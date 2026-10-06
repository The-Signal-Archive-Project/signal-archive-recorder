# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""NTP clock monitoring, and the timing chain it enables."""

import json
import time
from collections.abc import Sequence
from pathlib import Path

import jsonschema
import pytest

from signal_archive_recorder.clockmon.monitor import ClockMonitor, NtpAnswer, status_for
from signal_archive_recorder.clockmon.os_sync import (
    OsSync,
    parse_chronyc,
    parse_macos,
    parse_timedatectl,
    parse_w32tm,
    read_os_sync,
)
from signal_archive_recorder.config import ConfigError, parse_config
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import CaptureWarning, ClockChecked, Event
from signal_archive_recorder.metadata.builder import _schemas
from tests.fakes.session_rig import FMT, Rig, S, utc_ns

SYNCED = OsSync(True, "timedatectl")


def probe_returning(offset: float):  # type: ignore[no-untyped-def]
    def probe(server: str) -> NtpAnswer:
        return NtpAnswer(offset, 0.02, 2)

    return probe


def collect(bus: EventBus) -> list[Event]:
    events: list[Event] = []
    bus.subscribe(lambda s: events.append(s.event))
    return events


@pytest.mark.parametrize(
    ("offset", "status"), [(0.05, "green"), (-0.3, "yellow"), (0.8, "red"), (None, "unknown")]
)
def test_clock_thresholds(offset: float | None, status: str) -> None:
    assert status_for(offset) == status


def test_red_offset_warns_operator() -> None:
    bus = EventBus(FakeClock(utc_ns("12:00:00")))
    events = collect(bus)
    monitor = ClockMonitor(bus, FakeClock(utc_ns("12:00:00")), probe=probe_returning(0.8),
                           os_sync=lambda: SYNCED)  # fmt: skip
    check = monitor.check_once()
    bus.close()
    assert (check.status, check.offset_s, check.server) == ("red", 0.8, "pool.ntp.org")
    assert check.measured_ns == utc_ns("12:00:00")
    assert [type(e) for e in events] == [ClockChecked, CaptureWarning]


def test_ntp_unreachable_falls_back_then_gives_up() -> None:
    asked: list[str] = []

    def flaky(server: str) -> NtpAnswer:
        asked.append(server)
        if server != "time.google.com":
            raise OSError("no route")
        return NtpAnswer(-0.004, 0.03, 1)

    bus = EventBus(FakeClock())
    monitor = ClockMonitor(bus, FakeClock(), probe=flaky, os_sync=lambda: SYNCED)
    check = monitor.check_once()
    assert check.server == "time.google.com" and check.offset_s == -0.004
    assert asked == ["pool.ntp.org", "time.cloudflare.com", "time.google.com"]

    def down(server: str) -> NtpAnswer:
        raise OSError("offline")

    offline = ClockMonitor(bus, FakeClock(), probe=down, os_sync=lambda: OsSync(None, None))
    check = offline.check_once()
    bus.close()
    assert (check.offset_s, check.status, check.server) == (None, "unknown", None)


def test_monitor_checks_at_start_and_on_interval() -> None:
    bus = EventBus(FakeClock())
    events = collect(bus)
    monitor = ClockMonitor(bus, FakeClock(), probe=probe_returning(0.01), interval_s=0.05,
                           os_sync=lambda: SYNCED)  # fmt: skip
    monitor.start()
    time.sleep(0.3)
    started = time.monotonic()
    monitor.stop()
    assert time.monotonic() - started < 1
    bus.close()
    assert sum(isinstance(e, ClockChecked) for e in events) >= 3


def test_monitor_survives_a_broken_os_reader() -> None:
    def broken() -> OsSync:
        raise RuntimeError("boom")

    bus = EventBus(FakeClock())
    monitor = ClockMonitor(bus, FakeClock(), probe=probe_returning(0.0), os_sync=broken,
                           interval_s=0.05)  # fmt: skip
    monitor.start()
    time.sleep(0.15)
    monitor.stop()
    bus.close()
    assert not monitor._thread.is_alive()


# -- OS sync parsers (from real tool output) ---------------------------------------

TIMEDATECTL = "NTPSynchronized=yes\n"
CHRONYC = """Reference ID    : A9FEA97B (time.home.lan)
Stratum         : 3
Ref time (UTC)  : Tue Oct 06 15:30:01 2026
System time     : 0.000012345 seconds fast of NTP time
Last offset     : -0.000004321 seconds
RMS offset      : 0.000021000 seconds
Leap status     : Normal
"""
W32TM_SYNCED = """Leap Indicator: 0(no warning)
Stratum: 4 (secondary reference - syncd by (S)NTP)
Last Successful Sync Time: 10/6/2026 3:30:01 PM
Source: time.windows.com,0x9
Poll Interval: 10 (1024s)
"""
W32TM_LOCAL = W32TM_SYNCED.replace("time.windows.com,0x9", "Local CMOS Clock")
MACOS = "Network Time: On\n"


def test_os_sync_parsers() -> None:
    assert parse_timedatectl(TIMEDATECTL) is True
    assert parse_timedatectl("NTPSynchronized=no\n") is False
    assert parse_chronyc(CHRONYC) is True
    assert parse_chronyc(CHRONYC.replace("Normal", "Not synchronised")) is False
    assert parse_w32tm(W32TM_SYNCED) is True
    assert parse_w32tm(W32TM_LOCAL) is False
    assert parse_macos(MACOS) is True
    assert parse_timedatectl("garbage") is None


def test_read_os_sync_falls_back_and_never_keeps_output() -> None:
    def runner(cmd: Sequence[str]) -> str | None:
        return None if cmd[0] == "timedatectl" else CHRONYC

    result = read_os_sync(runner, system="Linux")
    assert result == OsSync(True, "chronyc")  # just yes/no and the tool: no server names
    assert read_os_sync(lambda cmd: None, system="Linux") == OsSync(None, None)


# -- metadata ---------------------------------------------------------------------


def validator(name: str) -> jsonschema.Draft202012Validator:
    schemas, registry = _schemas()
    return jsonschema.Draft202012Validator(schemas[name], registry=registry)


def test_clock_checks_in_metadata(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=150)
    check = ClockChecked(source="clock", measured_ns=utc_ns("12:03:07"), offset_s=0.0123456789,
                         delay_s=0.02, stratum=2, server="pool.ntp.org", status="green",
                         os_synchronized=True, os_sync_tool="timedatectl")  # fmt: skip
    rig.publish(check)
    rig.pump_to(60)
    private = ClockChecked(source="clock", measured_ns=utc_ns("12:04:07"), offset_s=0.2,
                           server="ntp.home.lan", status="yellow")  # fmt: skip
    rig.publish(private)
    chunks = rig.finish()

    session = json.loads(rig.session.session_json.read_text())
    validator("session").validate(session)
    checks = session["clock"]["checks"]
    assert session["clock"]["time_source"]["value"] == "ntp"
    assert [c["offset_s"] for c in checks] == [0.012346, 0.2]
    assert [c["server"] for c in checks] == ["pool.ntp.org", "custom"]  # LAN names stay home
    assert checks[1]["stream_frame"] == 60 * FMT.sample_rate
    assert "home.lan" not in json.dumps(session) + json.dumps(chunks)
    first, second = chunks
    assert first["clock"]["previous_check"]["offset_s"] == 0.012346
    assert second["clock"]["previous_check"]["status"] == "yellow"
    events = [e for e in first["events"] if e["type"] == "ClockChecked"]
    assert [(e["frame_offset"], e["server"]) for e in events] == [
        (0, "pool.ntp.org"),
        (60 * FMT.sample_rate, "custom"),
    ]


def test_drift_chain_allows_correction(tmp_path: Path) -> None:
    """Sound card 100 ppm fast, computer clock 0.25 s behind: both are recoverable."""
    rig = Rig(tmp_path, start="12:00:00", seconds=650)
    rig.rate_hz = FMT.sample_rate * (1 + 100e-6)
    for minute in range(0, 11, 5):  # NTP checks every 5 minutes of stream time
        rig.pump_to(minute * 60)
        rig.publish(ClockChecked(source="clock", measured_ns=rig.clock.now_ns(),
                                 offset_s=0.25, status="yellow"))  # fmt: skip
    chunks = rig.finish()

    # What a pipeline would do, from the published metadata alone:
    session = json.loads(rig.session.session_json.read_text())
    offsets = [(c["measured_ns"], c["offset_s"]) for c in session["clock"]["checks"]]

    def true_utc_s(computer_ns: int) -> float:
        _, offset = min(offsets, key=lambda o: abs(o[0] - computer_ns))
        return computer_ns / S + offset

    sync = [p for c in chunks for p in c["audio"]["sync_points"]]
    (f0, t0), (f1, t1) = sync[0], sync[-1]
    true_rate = (f1 - f0) / (true_utc_s(t1) - true_utc_s(t0))
    assert true_rate == pytest.approx(FMT.sample_rate * (1 + 100e-6), abs=0.02)
    # ...and the first sample's true UTC is 0.25 s later than the computer thought.
    computer_first = chunks[0]["time"]["first_sample_ns"]
    assert true_utc_s(computer_first) - computer_first / S == pytest.approx(0.25)


def test_clock_config() -> None:
    config = parse_config({"audio": {"device": "x"}, "clock": {"interval_s": 300}})
    assert config.clock.enabled and config.clock.interval_s == 300
    with pytest.raises(ConfigError, match="at least 60"):
        parse_config({"audio": {"device": "x"}, "clock": {"interval_s": 5}})
