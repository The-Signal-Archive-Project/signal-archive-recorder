# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Hardening H1: the setup window, review & upload, diagnostics, log file, autostart."""

import json
import logging
import platform
import socket
import sys
import uuid
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder import applog, cli, diagnostics
from signal_archive_recorder.config import load_config
from signal_archive_recorder.ui import autostart
from signal_archive_recorder.ui.autostart import Autostart, desktop_entry, launch_command
from signal_archive_recorder.ui.controller import RecorderController, SessionRow
from signal_archive_recorder.upload.consent import ConsentStore
from signal_archive_recorder.upload.token import Token
from tests.unit import test_upload
from tests.unit.test_firstrun import SILENT, TOKEN, MemoryKeyring, Script, make_wizard
from tests.unit.test_upload import make_session


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The upload tests' environment: config dir, fake hub, keyring and clock."""
    return test_upload.env.__wrapped__(tmp_path, monkeypatch)  # type: ignore[attr-defined,no-any-return]


# -- log file -------------------------------------------------------------------------


def test_log_file_written_and_tokens_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIGNAL_ARCHIVE_LOG_DIR", str(tmp_path / "logs"))
    path = applog.setup_logging()
    assert path == tmp_path / "logs" / "recorder.log"
    logging.getLogger("signal_archive_recorder.test").warning("upload failed with %s", TOKEN)
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = path.read_text()
    assert "upload failed with hf_****" in text and TOKEN not in text
    applog.setup_logging()  # again (the CLI runs many times in one test process)
    assert sum(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers) == 1


def test_unwritable_log_folder_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    monkeypatch.setenv("SIGNAL_ARCHIVE_LOG_DIR", str(blocker / "logs"))
    assert applog.setup_logging() is None


# -- diagnostics ------------------------------------------------------------------------


def test_diagnostics_hold_no_secrets(env: dict[str, Any], tmp_path: Path) -> None:
    session = make_session(env["root"])
    config = tmp_path / "diag.toml"
    config.write_text(
        f'[storage]\nroot = {json.dumps(str(env["root"]))}\n[audio]\ndevice = "USB Audio CODEC"\n'
        '[station]\ncallsign = "W9XYZ"\ngrid = "EN52wa"\n'
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    secrets = [socket.gethostname(), str(Path.home()), TOKEN, "W9XYZ", "EN52wa"]
    (logs / "recorder.log").write_text(
        f"opened {Path.home()}/x on {socket.gethostname()} as W9XYZ in EN52wa, token {TOKEN}\n"
    )
    (logs / "recorder.log.1").write_text("older\n")
    out = diagnostics.build(
        tmp_path / "out" / "diag.zip",
        config_path=config,
        logs=logs,
        sessions=env["root"] / "sessions",
        devices=lambda: "USB Audio CODEC  [ALSA, 2 ch, 48000 Hz]",
    )
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert names == {
            "README.txt",
            "about.txt",
            "recorder.toml",
            "audio-inputs.txt",
            "sessions.json",
            "logs/recorder.log",
            "logs/recorder.log.1",
        }
        everything = "\n".join(z.read(n).decode() for n in names)
        overview = json.loads(z.read("sessions.json"))
        about = z.read("about.txt").decode()
    for secret in secrets:
        if len(secret) >= 3:
            assert secret not in everything, secret
    assert 'callsign = "<redacted>"' in everything
    assert "USB Audio CODEC" in everything  # device names are what's being debugged
    assert overview[0]["session"] == session.name and overview[0]["flac"] == 2
    assert overview[0]["end_reason"] and "state" not in overview[0]  # never uploaded
    assert "signal-archive-recorder" in about and "python" in about


def test_diagnostics_survive_a_broken_audio_system(tmp_path: Path) -> None:
    def broken() -> str:
        raise OSError("PortAudio library not found")

    out = diagnostics.build(
        tmp_path / "d.zip", config_path=None, logs=tmp_path / "none", sessions=None, devices=broken
    )
    with zipfile.ZipFile(out) as z:
        assert "PortAudio library not found" in z.read("audio-inputs.txt").decode()
        assert z.read("recorder.toml").decode().startswith("# no configuration")


def test_diagnostics_command(
    env: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "devices_text", lambda show_all=False: "no devices here")
    out = tmp_path / "report.zip"
    assert cli.main(["diagnostics", "--config", str(env["config"]), "--out", str(out)]) == 0
    with zipfile.ZipFile(out) as z:
        assert "no devices here" in z.read("audio-inputs.txt").decode()


# -- autostart --------------------------------------------------------------------------


def test_linux_autostart_entry(tmp_path: Path) -> None:
    control = Autostart(system="Linux", folder=tmp_path / "autostart")
    assert not control.enabled()
    control.enable(["/usr/bin/signal-archive-recorder", "tray"])
    assert control.enabled()
    text = control.entry.read_text()
    assert "Exec=/usr/bin/signal-archive-recorder tray" in text and "Terminal=false" in text
    control.disable()
    control.disable()  # twice is fine
    assert not control.enabled()


def test_desktop_entry_quotes_paths_with_spaces() -> None:
    assert "Exec='/opt/My Apps/sar' tray" in desktop_entry(["/opt/My Apps/sar", "tray"])


def test_launch_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launch_command() == [sys.executable]
    monkeypatch.setattr(sys, "frozen", False)
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    command = launch_command()
    assert command[-3:] == ["-m", "signal_archive_recorder.cli", "tray"]


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows registry")
def test_windows_autostart_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "VALUE_NAME", f"SignalArchiveRecorderTest{uuid.uuid4().hex}")
    control = Autostart(system="Windows")
    try:
        assert not control.enabled()
        control.enable([r"C:\Program Files\Signal Archive Recorder\sar.exe"])
        assert control.enabled()
        assert autostart._windows_get() == r'"C:\Program Files\Signal Archive Recorder\sar.exe"'
    finally:
        control.disable()
    assert not control.enabled()


# -- review & upload (controller, no Qt) -----------------------------------------------


def test_controller_review_and_upload(env: dict[str, Any]) -> None:
    from tests.unit.test_upload import uploader

    session = make_session(env["root"])
    up = uploader(env)  # consent and login, as the real app would have
    controller = RecorderController(load_config(env["config"]))
    controller.config = load_config(env["config"])
    [row] = controller.finished_sessions()
    assert row == SessionRow(session.name, "queued", 2, row.seconds, True)
    text = controller.describe(session.name)
    assert "Shared about you" in text and "Radio-audio checks" in text
    assert "Upload state: queued" in text
    lines = controller.upload([session.name])
    assert any("pull request opened" in line for line in lines)
    assert controller.finished_sessions()[0].state == "pr_opened"
    assert up.sessions()[0][1].pr_num == 1


# -- Qt: the setup window ----------------------------------------------------------------


def test_setup_window_happy_path(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.setup_wizard import SetupWizard

    keyring = MemoryKeyring()
    env_ = make_wizard(tmp_path, Script([]), keyring=keyring)
    config = tmp_path / "config" / "recorder.toml"
    control = Autostart(system="Linux", folder=tmp_path / "autostart")
    wizard = SetupWizard(env_, config, autostart=control)
    qtbot.addWidget(wizard)
    wizard.show()

    wizard.next()  # welcome -> terms
    assert not wizard.terms.isComplete()
    wizard.terms.agree.setChecked(True)
    wizard.next()  # -> login
    assert not wizard.login.isComplete()
    wizard.login.token.setText("hf_readonly000000000000")
    wizard.login.check_token()
    qtbot.waitUntil(lambda: "read-only" in wizard.login.status.text())
    assert not wizard.login.isComplete()
    wizard.login.token.setText(TOKEN)
    wizard.login.check_token()
    qtbot.waitUntil(wizard.login.isComplete)
    assert "volunteer" in wizard.login.status.text()
    assert keyring.store == {}  # nothing saved before Finish
    wizard.next()  # -> audio
    assert wizard.audio.device().name == "Microphone (USB Audio CODEC )"  # recommended
    wizard.audio.test_level()
    qtbot.waitUntil(lambda: "good" in wizard.audio.result.text())
    wizard.next()  # -> station
    wizard.station.callsign.setText("w9xyz!")
    wizard.next()
    assert wizard.currentPage() is wizard.station and "callsign" in wizard.station.error.text()
    wizard.station.callsign.setText("w9xyz")
    wizard.station.share.setChecked(True)
    wizard.station.grid.setText("en52wa")
    wizard.station.precision.setCurrentIndex(2)  # 6 characters
    wizard.next()  # -> checks
    qtbot.waitUntil(wizard.checks.isComplete)
    assert "Found WSJT-X 3.0.2" in wizard.checks.wsjtx.text()
    qtbot.waitUntil(lambda: "behind true time" in wizard.checks.clock.text())
    wizard.next()  # -> finish
    assert "Microphone (USB Audio CODEC )" in wizard.finish.summary.text()
    assert not config.exists()
    wizard.accept()

    loaded = load_config(config)
    assert loaded.audio.device == "Microphone (USB Audio CODEC )"
    assert (loaded.station.callsign, loaded.station.share_callsign) == ("W9XYZ", True)
    assert (loaded.station.grid, loaded.station.grid_precision) == ("EN52wa", 6)
    assert env_.tokens.get() == Token(TOKEN)
    assert env_.consent.require().consent_version == "1"
    assert control.enabled()


def test_setup_window_cancel_saves_nothing(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.setup_wizard import SetupWizard

    keyring = MemoryKeyring()
    env_ = make_wizard(tmp_path, Script([]), keyring=keyring)
    config = tmp_path / "config" / "recorder.toml"
    wizard = SetupWizard(env_, config, autostart=Autostart("Linux", tmp_path / "autostart"))
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.next()
    wizard.terms.agree.setChecked(True)
    wizard.next()
    wizard.login.token.setText(TOKEN)
    wizard.login.check_token()
    qtbot.waitUntil(wizard.login.isComplete)
    wizard.reject()
    assert not config.exists() and keyring.store == {}
    assert ConsentStore(tmp_path / "config" / "consent.json").load() is None
    assert not (tmp_path / "autostart").exists()


def test_setup_window_returning_user(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.setup_wizard import SetupWizard

    keyring = MemoryKeyring()
    first = make_wizard(tmp_path, Script(["I agree", "1", "", ""], [TOKEN]), keyring=keyring)
    first.run(tmp_path / "a.toml")
    env_ = make_wizard(tmp_path, Script([]), keyring=keyring, levels=[SILENT])
    wizard = SetupWizard(env_, tmp_path / "b.toml", autostart=Autostart("Linux", tmp_path / "x"))
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.next()
    assert wizard.terms.isComplete() and wizard.terms.already
    wizard.next()
    qtbot.waitUntil(wizard.login.isComplete)
    assert "Already logged in" in wizard.login.status.text()
    wizard.next()
    wizard.audio.test_level()
    qtbot.waitUntil(lambda: "silent" in wizard.audio.result.text())
    assert wizard.audio.isComplete()  # a quiet input can still be chosen


# -- Qt: review window, status window, single instance -----------------------------------


class FakeReviewer:
    def __init__(self) -> None:
        self.rows = [
            SessionRow("20261005T120307Z", "queued", 2, 150.0, True),
            SessionRow("20261004T090000Z", "pr_opened", 1, 60.0, True),
        ]
        self.uploaded: list[list[str]] = []

    def finished_sessions(self) -> list[SessionRow]:
        return self.rows

    def describe(self, session_id: str) -> str:
        return f"Session {session_id}\n  Shared about you: ..."

    def upload(self, session_ids: list[str]) -> list[str]:
        self.uploaded.append(session_ids)
        self.rows[0] = SessionRow("20261005T120307Z", "pr_opened", 2, 150.0, True)
        return [f"{s}: pull request opened: https://example/pr/1" for s in session_ids]


def test_review_window(qtbot: Any) -> None:
    from signal_archive_recorder.ui.review_window import ReviewWindow

    fake = FakeReviewer()
    window = ReviewWindow(fake)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.sessions.count() == 2)
    assert "waiting to upload" in window.sessions.item(0).text()
    assert window.selected() == "20261005T120307Z"  # the newest is shown first
    assert window.upload_all.isEnabled() and window.upload_one.isEnabled()
    window.sessions.setCurrentRow(1)  # already uploaded: can't upload again
    qtbot.waitUntil(lambda: "20261004T090000Z" in window.details.toPlainText())
    assert not window.upload_one.isEnabled()
    window.sessions.setCurrentRow(0)
    qtbot.waitUntil(lambda: "Shared about you" in window.details.toPlainText())
    assert window.upload_one.isEnabled()
    window.upload_selected()
    qtbot.waitUntil(lambda: "pull request opened" in window.message.text())
    assert fake.uploaded == [["20261005T120307Z"]]
    qtbot.waitUntil(lambda: "in review" in window.sessions.item(0).text())
    assert not window.upload_all.isEnabled()


def test_status_window_diagnostics_and_autostart(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.app import StatusWindow
    from tests.unit.test_stage9c import FakeController

    saved: list[Path] = []
    control = Autostart("Linux", tmp_path / "autostart")
    window = StatusWindow(
        FakeController(),
        recordings=tmp_path,
        settings=tmp_path / "r.toml",
        diagnostics=lambda p: saved.append(p) or p,
        autostart=control,
    )
    qtbot.addWidget(window)
    window.write_diagnostics(tmp_path / "d.zip")
    assert saved == [tmp_path / "d.zip"] and "Diagnostics saved" in window.message.text()
    assert not window.autostart_box.isChecked()
    window.autostart_box.setChecked(True)
    assert control.enabled()
    window.autostart_box.setChecked(False)
    assert not control.enabled()


def test_single_instance(qtbot: Any) -> None:
    from signal_archive_recorder.ui.app import SingleInstance

    name = f"sar-test-{uuid.uuid4().hex[:8]}"
    first, second = SingleInstance(name), SingleInstance(name)
    shown: list[bool] = []
    first.on_show = lambda: shown.append(True)
    try:
        assert first.acquire()
        assert not second.acquire()  # a second start only asks the first to show itself
        qtbot.waitUntil(lambda: shown == [True])
    finally:
        first.release()
    third = SingleInstance(name)
    assert third.acquire()  # free again once the first has gone
    third.release()


def test_gui_entry_point_runs_tray(monkeypatch: pytest.MonkeyPatch) -> None:
    from signal_archive_recorder import gui

    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["signal-archive-recorder-gui", "--config", "x.toml"])
    assert gui.main() == 0
    assert seen == [["tray", "--config", "x.toml"]]


def test_desktop_app_saves_the_session_on_sigterm(tmp_path: Path) -> None:
    """Logging out of the desktop (SIGTERM; Ctrl-Break on Windows) finishes the session."""
    import os
    import signal
    import subprocess
    import time

    import numpy as np
    import soundfile as sf

    from tests.fakes.fake_audio import noise
    from tests.integration.test_e2e import FMT

    wav = tmp_path / "reference.wav"
    pcm = np.frombuffer(noise(FMT, 60, seed=3), dtype="<i2").reshape(-1, 1)
    sf.write(wav, pcm, 8000, subtype="PCM_16")
    root = tmp_path / "archive"
    config = tmp_path / "recorder.toml"
    config.write_text(
        f"[storage]\nroot = {json.dumps(str(root))}\n"
        f'[audio]\nfile = {json.dumps(str(wav))}\nsample_format = "int16"\n'
        '[wsjtx]\nport = 0\n[clock]\nenabled = false\n[recording]\nstart = "always"\n'
    )
    env_vars = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "SIGNAL_ARCHIVE_CONFIG_DIR": str(tmp_path / "config"),
        "SIGNAL_ARCHIVE_LOG_DIR": str(tmp_path / "logs"),
    }
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        [sys.executable, "-m", "signal_archive_recorder.gui", "--config", str(config)],
        env=env_vars,
        creationflags=flags,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not list(root.glob("sessions/*/session.json")):
            assert proc.poll() is None, proc.communicate()[0]
            assert time.monotonic() < deadline, "the app never started recording"
            time.sleep(0.2)
        time.sleep(2.5)
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)  # type: ignore[attr-defined]
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    [session_json] = root.glob("sessions/*/session.json")
    session = json.loads(session_json.read_text())
    assert session["end_reason"] == "signal" and session["ended_utc"]
    assert list(session_json.parent.glob("recordings/*.flac"))
    assert "recorder.log" in os.listdir(tmp_path / "logs")
