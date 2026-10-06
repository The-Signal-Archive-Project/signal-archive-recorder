# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 9c: tray state, "mark this" notes, pause/resume, and the status window."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.audio.levels import LiveLevel
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import Note, SourceUp
from signal_archive_recorder.station import Station
from signal_archive_recorder.ui.controller import RecorderController
from signal_archive_recorder.ui.health import HealthInputs, checklist, tray_state
from tests.fakes.fake_audio import FakeBackend, noise, sine
from tests.fakes.session_rig import Rig, frames, utc_ns
from tests.integration.test_e2e import DEVICE, FMT, S, fake_ntp, make_config

HEALTHY = HealthInputs(
    recording=True,
    peak_dbfs=-20.0,
    wsjtx="up",
    mode="FT8",
    dial_hz=14_074_000,
    clock="green",
    clock_offset_s=0.01,
)


# -- tray state (pure) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({}, "green"),
        ({"paused": True, "recording": False}, "grey"),
        ({"recording": False}, "red"),
        ({"disk": "critical"}, "red"),
        ({"disk": "low"}, "yellow"),
        ({"clipped": 12}, "yellow"),
        ({"peak_dbfs": -95.0}, "yellow"),  # silent input
        ({"frames_lost": 480}, "yellow"),
        ({"wsjtx": "down"}, "yellow"),
        ({"wsjtx": "never"}, "yellow"),
        ({"wsjtx": "disabled"}, "green"),
        ({"clock": "red", "clock_offset_s": 0.8}, "yellow"),  # recording still works
        ({"uploads": {"blocked": 1}}, "yellow"),
        ({"uploads": {"queued": 2, "pr_opened": 1}}, "green"),
    ],
)
def test_tray_state_mapping(changes: dict[str, Any], state: str) -> None:
    assert tray_state(replace(HEALTHY, **changes)) == state


def test_checklist_explains_itself() -> None:
    items = {i.name: i for i in checklist(replace(HEALTHY, uploads={"queued": 2, "failed": 1}))}
    assert items["WSJT-X"].detail == "FT8 14.074 MHz"
    assert items["Clock"].detail == "+0.010 s"
    assert items["Uploads"].detail == "2 to upload, 1 need attention"
    assert items["Uploads"].level == "warn"
    assert "kept back" in checklist(replace(HEALTHY, wsjtx="never"))[2].detail


# -- "mark this" notes --------------------------------------------------------------


def test_mark_note_event(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=150)
    rig.pump_to(30)
    rig.publish(Note(source="operator", text="strong QRM"))
    rig.pump_to(120)  # past 12:05:00, in the second chunk
    rig.publish(Note(source="operator", text=f"rare DX, notes in {Path.home()}/x"))
    first, second = rig.finish()
    [a] = [e for e in first["events"] if e["type"] == "Note"]
    assert (a["detail"], a["frame_offset"], a["t_ns"]) == (
        "strong QRM",
        frames(30),
        utc_ns("12:03:37"),
    )
    [b] = [e for e in second["events"] if e["type"] == "Note"]
    assert b["frame_offset"] == frames(120 - 113) and b["source"] == "operator"
    assert str(Path.home()) not in b["detail"]  # free text is scrubbed like everything else


# -- the controller ------------------------------------------------------------------


def controller(tmp_path: Path) -> tuple[RecorderController, FakeClock]:
    clock = FakeClock(utc_ns("12:03:07"))

    def factory(config: Any) -> Station:
        backend = FakeBackend(
            [DEVICE], noise(FMT, 5), on_block=lambda n: clock.advance(n * S // 8000)
        )
        return Station(
            config, clock=clock, ntp_probe=fake_ntp, recorder_kwargs={"backend": backend}
        )

    # "always": these tests are about pause, resume and marks (standby: test_station.py)
    config = replace(make_config(tmp_path, buffer_seconds=60), start="always")
    return RecorderController(config, factory), clock


def test_pause_saves_the_session_and_resume_starts_a_new_one(tmp_path: Path) -> None:
    ctl, _ = controller(tmp_path)
    ctl.start()
    first = ctl.snapshot().session
    assert ctl.snapshot().recording and tray_state(ctl.snapshot()) == "yellow"  # no WSJT-X yet
    ctl.pause()
    assert ctl.paused and tray_state(ctl.snapshot()) == "grey"
    meta = json.loads((tmp_path / "sessions" / first / "session.json").read_text())  # type: ignore[operator]
    assert meta["end_reason"] == "paused" and meta["ended_utc"]
    ctl.resume()
    second = ctl.snapshot().session
    assert second != first and ctl.snapshot().recording
    ctl.stop()
    assert not ctl.snapshot().recording


def test_controller_follows_events_and_marks(tmp_path: Path) -> None:
    ctl, _ = controller(tmp_path)
    assert not ctl.mark("too early")  # nothing is recording yet
    ctl.start()
    assert ctl.recorder is not None and ctl.recorder.bus is not None
    ctl.recorder.bus.publish(SourceUp(source="wsjtx", detail="WSJT-X 3.0.2"))
    assert ctl.recorder.bus.wait_idle()
    assert ctl.snapshot().wsjtx == "up"
    assert ctl.mark("strong QRM") and ctl.notes == ["strong QRM"]
    assert not ctl.mark("   ")
    ctl.stop()


def test_live_level() -> None:
    fmt = AudioFormat(48_000, 2, "int24")
    level = LiveLevel(fmt)
    assert level.latest() is None
    level.write(sine(fmt, 0.1, amplitude=0.5), 0)
    latest = level.latest()
    assert latest is not None and max(latest.peak_dbfs) == pytest.approx(-6.0, abs=0.1)


# -- the window ------------------------------------------------------------------------


class FakeController:
    def __init__(self) -> None:
        self.paused = False
        self.notes: list[str] = []
        self.health = replace(HEALTHY)
        self.calls: list[str] = []

    def pause(self) -> None:
        self.calls.append("pause")
        self.paused = True
        self.health = replace(self.health, paused=True, recording=False)

    def resume(self) -> None:
        self.calls.append("resume")
        self.paused = False
        self.health = replace(self.health, paused=False, recording=True)

    def stop(self, reason: str = "stopped") -> None:
        self.calls.append("stop")

    def mark(self, text: str) -> bool:
        self.notes.append(text)
        return bool(text.strip())

    def snapshot(self) -> HealthInputs:
        return self.health

    def upload_now(self) -> list[Any]:
        return []


def test_window_shows_status_and_controls(qtbot: Any, tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from signal_archive_recorder.ui.app import StatusWindow

    fake = FakeController()
    window = StatusWindow(fake, recordings=tmp_path, settings=tmp_path / "recorder.toml")
    qtbot.addWidget(window)
    assert window.state == "green" and window.header.text() == "Recording; all good"
    assert window.rows["WSJT-X"][1].text() == "FT8 14.074 MHz"
    assert window.meter.value() == -20

    fake.health = replace(fake.health, clipped=5)
    window.refresh()
    assert window.state == "yellow" and "clipping" in window.rows["Audio"][1].text()

    qtbot.mouseClick(window.pause_button, Qt.MouseButton.LeftButton)
    assert fake.calls == ["pause"] and window.state == "grey"
    assert window.pause_button.text() == "Resume"
    qtbot.mouseClick(window.pause_button, Qt.MouseButton.LeftButton)
    assert fake.calls == ["pause", "resume"] and window.pause_button.text() == "Pause"

    window.note_input.setText("band opening to JA")
    qtbot.keyClick(window.note_input, Qt.Key.Key_Return)
    assert fake.notes == ["band opening to JA"]
    assert window.notes.item(0).text() == "band opening to JA"


def test_tray_command_explains_the_gui_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins

    from signal_archive_recorder import cli

    monkeypatch.setenv("SIGNAL_ARCHIVE_CONFIG_DIR", str(tmp_path))
    cli.main(["init", "--device", "X"])
    real_import = builtins.__import__

    def no_qt(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("signal_archive_recorder.ui.app"):
            raise ImportError("No module named 'PySide6'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_qt)
    assert cli.main(["tray"]) == cli.EXIT_CONFIG
    assert "signal-archive-recorder[gui]" in capsys.readouterr().err
