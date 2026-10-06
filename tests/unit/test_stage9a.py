# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 9a: the real Linux sound-server rate, a busy WSJT-X port, disk-space warnings."""

import json
import socket
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from signal_archive_recorder.audio.device import DeviceInfo, open_input
from signal_archive_recorder.audio.sound_server import (
    is_server_device,
    native_rate,
    parse_pactl_info,
    parse_pw_metadata,
    server_rate,
)
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import CaptureWarning, Event
from signal_archive_recorder.session.disk import GB, DiskMonitor, Level, check
from tests.fakes.fake_audio import FakeBackend

# Real output from the development laptop (PipeWire 1.6.9).
PW_METADATA = """\
Found "settings" metadata 34
update: id:0 key:'log.level' value:'2' type:''
update: id:0 key:'clock.rate' value:'48000' type:''
update: id:0 key:'clock.allowed-rates' value:'[ 48000 ]' type:''
update: id:0 key:'clock.quantum' value:'1024' type:''
update: id:0 key:'clock.force-rate' value:'0' type:''
"""
PACTL_PIPEWIRE = """\
Server String: /run/user/1000/pulse/native
Server Name: PulseAudio (on PipeWire 1.6.9)
Default Sample Specification: float32le 2ch 48000Hz
"""
PACTL_PULSE = "Server Name: pulseaudio\nDefault Sample Specification: s16le 2ch 44100Hz\n"
PIPEWIRE = DeviceInfo(15, "pipewire", "ALSA", 128, 44_100)  # reports 44.1 kHz; really 48 kHz


def runner(outputs: dict[str, str | None]):  # type: ignore[no-untyped-def]
    def run(cmd: Sequence[str]) -> str | None:
        return outputs.get(cmd[0])

    return run


# -- the real sound-server rate (issue #7) --------------------------------------------


def test_parsers() -> None:
    assert parse_pw_metadata(PW_METADATA) == 48_000
    assert (
        parse_pw_metadata(PW_METADATA.replace("force-rate' value:'0'", "force-rate' value:'96000'"))
        == 96_000
    )
    assert parse_pactl_info(PACTL_PIPEWIRE) == (48_000, "pipewire")
    assert parse_pactl_info(PACTL_PULSE) == (44_100, "pulseaudio")


def test_server_rate_prefers_pipewire_then_falls_back() -> None:
    both = runner({"pw-metadata": PW_METADATA, "pactl": PACTL_PULSE})
    assert server_rate(both).rate == 48_000  # type: ignore[union-attr]
    only_pactl = runner({"pactl": PACTL_PULSE})
    assert (server_rate(only_pactl).rate, server_rate(only_pactl).server) == (44_100, "pulseaudio")  # type: ignore[union-attr]
    assert server_rate(runner({})) is None


def test_native_rate_only_changes_for_sound_server_devices() -> None:
    run = runner({"pw-metadata": PW_METADATA})
    assert native_rate(PIPEWIRE, run, "Linux") == 48_000
    raw = DeviceInfo(0, "USB Audio CODEC: - (hw:2,0)", "ALSA", 2, 44_100)
    assert native_rate(raw, run, "Linux") == 44_100  # a real device knows its own rate
    assert native_rate(PIPEWIRE, run, "Windows") == 44_100
    assert native_rate(PIPEWIRE, runner({}), "Linux") == 44_100  # no server answer: as reported
    assert is_server_device(DeviceInfo(0, "Pulse", "ALSA", 2, 44_100), "Linux")


def test_records_at_the_server_rate_without_a_false_warning() -> None:
    opened = open_input(FakeBackend([PIPEWIRE]), PIPEWIRE, lambda *a: None, native_rate=48_000)
    assert opened.delivered.sample_rate == 48_000
    assert opened.warnings == []


def test_warns_when_the_config_forces_another_rate() -> None:
    opened = open_input(
        FakeBackend([PIPEWIRE]), PIPEWIRE, lambda *a: None, sample_rate=44_100, native_rate=48_000
    )
    [warning] = opened.warnings
    assert warning.code == "os_resampling" and "48000 Hz" in warning.message


# -- a busy WSJT-X port -------------------------------------------------------------


def test_busy_wsjtx_port_doesnt_stop_recording(tmp_path: Path) -> None:
    from signal_archive_recorder.recorder import Recorder
    from tests.integration.test_e2e import DEVICE, FMT, S, fake_ntp, make_config, noise, utc_ns

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:  # e.g. GridTracker
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        clock = FakeClock(utc_ns("12:03:07"))
        backend = FakeBackend(
            [DEVICE], noise(FMT, 5), on_block=lambda n: clock.advance(n * S // 8000)
        )
        config = make_config(tmp_path, buffer_seconds=60)
        config = type(config)(**{**config.__dict__, "wsjtx": type(config.wsjtx)(port=port)})
        recorder = Recorder(config, backend=backend, clock=clock, ntp_probe=fake_ntp)
        session = recorder.start()  # doesn't raise
        assert recorder.listener is None
        assert backend.stream is not None
        backend.stream.pump()
        summary = recorder.stop()
    assert summary.capture.written_frames == 5 * 8000  # every sample recorded
    [chunk] = summary.chunks
    codes = [e.get("code") for e in chunk["events"] if e["type"] == "CaptureWarning"]
    assert "wsjtx_port_busy" in codes
    assert json.loads(session.session_json.read_text())["end_reason"] == "stopped"


# -- disk space ---------------------------------------------------------------------


def usage(free: int):  # type: ignore[no-untyped-def]
    return lambda path: SimpleNamespace(free=free)


def test_disk_levels() -> None:
    rate = 288_000  # 48 kHz stereo int24: about 1 GB an hour
    assert check(Path("."), rate, usage(50 * GB)).level is Level.OK
    assert check(Path("."), rate, usage(int(1.5 * GB))).level is Level.LOW  # under the 2 GB floor
    assert check(Path("."), rate * 4, usage(3 * GB)).level is Level.LOW  # under an hour at 4x
    status = check(Path("."), rate, usage(100_000_000))
    assert status.level is Level.CRITICAL and status.hours_left < 0.2


def test_disk_monitor_warns_once_per_change() -> None:
    bus = EventBus(FakeClock())
    events: list[Event] = []
    bus.subscribe(lambda s: events.append(s.event))
    free = {"bytes": 50 * GB}
    monitor = DiskMonitor(
        bus, Path("."), 288_000, usage=lambda p: SimpleNamespace(free=free["bytes"])
    )
    for value in (50 * GB, int(1.5 * GB), int(1.4 * GB), 100_000_000, 90_000_000, 50 * GB):
        free["bytes"] = value
        monitor.check_once()
    bus.close()
    codes = [e.code for e in events if isinstance(e, CaptureWarning)]
    assert codes == ["disk_low", "disk_critical", "disk_ok"]
