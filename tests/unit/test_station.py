# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The station records only while WSJT-X runs, and stands by otherwise."""

import json
import socket
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder.config import ConfigError, parse_config
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.station import Station
from signal_archive_recorder.ui.controller import RecorderController
from signal_archive_recorder.ui.health import tray_state
from tests.fakes.fake_audio import FakeBackend, noise
from tests.fakes.session_rig import utc_ns
from tests.integration.test_e2e import DEVICE, FMT, OWN_CALL, S, fake_ntp, make_config


def status(dial: int, mode: str = "FT8") -> m.Status:
    return m.Status(
        "WSJT-X",
        dial,
        mode,
        None,
        "-15",
        mode,
        False,
        False,
        False,
        1500,
        1500,
        OWN_CALL,
        "EN52wa",
        None,
        False,
        None,
        False,
        0,
        None,
        None,
        "Default",
        None,
    )


class Rig:
    """A station with a fake sound card on a fake clock, and WSJT-X over real UDP."""

    def __init__(self, tmp_path: Path, start: str = "with_decoder", settle_s: float = 0.3) -> None:
        self.clock = FakeClock(utc_ns("12:03:07"))
        self.backend = FakeBackend(
            [DEVICE], noise(FMT, 900), on_block=lambda n: self.clock.advance(n * S // 8000)
        )
        self.opens = 0
        real_open = self.backend.open_shared_input

        def counting_open(*args: Any, **kwargs: Any) -> Any:
            self.opens += 1
            return real_open(*args, **kwargs)

        self.backend.open_shared_input = counting_open  # type: ignore[method-assign]
        self.root = tmp_path
        config = replace(make_config(tmp_path, buffer_seconds=60), start=start)
        self.station = Station(
            config,
            clock=self.clock,
            ntp_probe=fake_ntp,
            recorder_kwargs={"backend": self.backend},
            settle_s=settle_s,
        )
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, message: Any) -> None:
        assert self.station.listener is not None
        self.sock.sendto(m.encode(message), self.station.listener.address)

    def wait(self, condition: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + 10
        while not condition():
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            time.sleep(0.02)

    def pump(self, seconds: float) -> None:
        assert self.backend.stream is not None
        self.backend.stream.pump(int(seconds * FMT.sample_rate))

    def sessions(self) -> list[dict[str, Any]]:
        files = sorted(self.root.glob("sessions/*/session.json"))
        return [json.loads(p.read_text()) for p in files]

    def chunks(self, session_id: str) -> list[dict[str, Any]]:
        files = sorted(self.root.glob(f"sessions/{session_id}/recordings/*.meta.json"))
        return [json.loads(p.read_text()) for p in files]


def test_stands_by_until_wsjtx_runs_and_stops_when_it_closes(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.station.start()
    try:
        time.sleep(0.3)
        assert rig.station.state == "standby"
        assert rig.opens == 0 and rig.sessions() == []  # sound card closed, nothing written

        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        rig.send(status(14_074_000))  # may arrive before the session starts: still applied
        rig.wait(lambda: rig.station.state == "recording", "recording")
        rig.pump(20)
        rig.send(m.Close("WSJT-X"))
        rig.wait(lambda: rig.station.state == "standby", "standby after Close")
        assert rig.backend.stream is not None and rig.backend.stream.closed  # card released

        [session] = rig.sessions()
        assert session["end_reason"] == "decoder_closed" and session["ended_utc"]
        [chunk] = rig.chunks(session["session_id"])
        assert chunk["mode"]["mode_id"]["value"] == "ft8"
        assert chunk["radio"]["dial_hz"]["value"] == 14_074_000
        assert chunk["audio"]["flac"]["verified"]

        # WSJT-X again, on another band: a new session
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        rig.send(status(7_074_000))
        rig.wait(lambda: rig.station.state == "recording", "second session")
        rig.pump(5)
    finally:
        rig.station.stop("signal")
    _, second = rig.sessions()
    assert second["end_reason"] == "signal" and rig.opens == 2
    [chunk] = rig.chunks(second["session_id"])
    assert chunk["radio"]["dial_hz"]["value"] == 7_074_000


def test_silent_wsjtx_ends_the_session(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.station.start()
    try:
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        rig.wait(lambda: rig.station.state == "recording", "recording")
        rig.pump(10)
        rig.clock.advance(31 * S)  # no heartbeat for 31 s: WSJT-X crashed or hung
        rig.wait(lambda: rig.station.state == "standby", "standby after the timeout")
    finally:
        rig.station.stop()
    [session] = rig.sessions()
    assert session["end_reason"] == "decoder_closed"


def test_pause_holds_off_recording_until_resumed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.station.start()
    try:
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        rig.wait(lambda: rig.station.state == "recording", "recording")
        rig.pump(5)
        rig.station.pause()
        assert rig.station.state == "paused"
        assert rig.sessions()[0]["end_reason"] == "paused"  # saved straight away
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        time.sleep(1.2)
        assert rig.station.state == "paused" and len(rig.sessions()) == 1
        rig.station.resume()  # WSJT-X is still running
        assert rig.station.state == "recording"
        rig.pump(5)
    finally:
        rig.station.stop()
    assert [s["end_reason"] for s in rig.sessions()] == ["paused", "stopped"]


def test_always_records_without_wsjtx(tmp_path: Path) -> None:
    rig = Rig(tmp_path, start="always")
    rig.station.start()
    assert rig.station.state == "recording" and rig.opens == 1
    rig.pump(5)
    rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
    rig.send(m.Close("WSJT-X"))
    time.sleep(1.2)
    assert rig.station.state == "recording"  # WSJT-X closing doesn't matter here
    rig.station.stop("signal")
    [session] = rig.sessions()
    assert session["end_reason"] == "signal"


def test_missing_sound_card_is_retried(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.backend.devices = []  # unplugged
    rig.station.start()
    try:
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        rig.wait(lambda: rig.station.last_error is not None, "the error")
        assert "No input device matches" in rig.station.last_error
        assert rig.station.state == "standby" and rig.sessions() == []
        rig.backend.devices = [DEVICE]  # plugged back in
        rig.clock.advance(20 * S)  # less than the WSJT-X timeout, more than... not yet
        time.sleep(1.2)
        assert rig.station.state == "standby"
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))  # keep WSJT-X alive
        rig.clock.advance(11 * S)  # 31 s after the failure: try again
        rig.wait(lambda: rig.station.state == "recording", "the retry")
        assert rig.station.last_error is None
    finally:
        rig.station.stop()


def test_always_mode_raises_for_a_missing_sound_card(tmp_path: Path) -> None:
    from signal_archive_recorder.audio.device import DeviceUnavailableError

    rig = Rig(tmp_path, start="always")
    rig.backend.devices = []
    with pytest.raises(DeviceUnavailableError):
        rig.station.start()
    assert rig.station.state == "stopped" and rig.station.listener is None


def test_config_start_option() -> None:
    base = {"audio": {"device": "x"}}
    assert parse_config(base).start == "with_decoder"
    assert parse_config({**base, "recording": {"start": "always"}}).start == "always"
    with pytest.raises(ConfigError, match="with_decoder"):
        parse_config({**base, "recording": {"start": "sometimes"}})
    with pytest.raises(ConfigError, match="waits for WSJT-X"):
        parse_config({**base, "wsjtx": {"enabled": False}})
    assert (
        parse_config({**base, "wsjtx": {"enabled": False}, "recording": {"start": "always"}}).start
        == "always"
    )


def test_tray_is_blue_while_ready(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    controller = RecorderController(rig.station.config, lambda config: rig.station)
    controller.start()
    try:
        time.sleep(0.3)
        health = controller.snapshot()
        assert health.standby and not health.recording and tray_state(health) == "blue"
        assert not controller.mark("too early")  # nothing is being recorded
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        rig.send(status(14_074_000))
        rig.wait(lambda: controller.snapshot().recording, "recording")
        rig.pump(2)
        health = controller.snapshot()
        assert health.wsjtx == "up" and health.mode == "FT8" and health.session
        assert controller.mark("band opening")
        controller.pause()
        assert tray_state(controller.snapshot()) == "grey"
        controller.resume()
        rig.wait(lambda: controller.snapshot().recording, "recording again")
    finally:
        controller.stop()


@pytest.mark.parametrize(
    ("changes", "state", "detail"),
    [
        ({}, "blue", "ready: starts when WSJT-X is running"),
        ({"wsjtx": "busy"}, "red", None),  # can never tell when to record
        ({"problem": "Couldn't start recording: no device"}, "red", "Couldn't start"),
        ({"paused": True}, "grey", "paused"),
        ({"disk": "low"}, "yellow", None),
    ],
)
def test_ready_states(changes: dict[str, Any], state: str, detail: str | None) -> None:
    from signal_archive_recorder.ui.health import HealthInputs, checklist

    health = replace(HealthInputs(standby=True), **changes)
    assert tray_state(health) == state
    if detail:
        assert checklist(health)[0].detail.startswith(detail)


def test_waits_for_wsjtx_to_settle_before_recording(tmp_path: Path) -> None:
    """What WSJT-X 3.0.2 really sent while loading its settings (Rig None, FT4)."""
    rig = Rig(tmp_path, settle_s=1.0)
    rig.station.start()
    try:
        rig.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
        for dial in (0, 7_047_500, 145_000_000, 7_047_500):
            rig.send(status(dial, "FT4"))
            time.sleep(0.1)
        time.sleep(0.5)
        assert rig.station.state == "standby"  # still settling
        rig.wait(lambda: rig.station.state == "recording", "recording once settled")
        rig.pump(10)
    finally:
        rig.station.stop()
    [session] = rig.sessions()
    [chunk] = rig.chunks(session["session_id"])  # one chunk, not four slivers
    assert chunk["radio"]["dial_hz"]["value"] == 7_047_500
    assert chunk["mode"]["mode_id"]["value"] == "ft4"
