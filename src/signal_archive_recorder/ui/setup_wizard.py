# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The first-run setup wizard, as a window.

The same steps and checks as the terminal wizard (firstrun/wizard.py), using its
services: terms, Hugging Face login, audio input with a level test, station,
WSJT-X and the clock. Nothing is saved until Finish, so cancelling part-way leaves
no half-made configuration (or stored token) behind.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from signal_archive_recorder.audio.device import DeviceInfo, DeviceUnavailableError
from signal_archive_recorder.audio.sound_server import native_rate
from signal_archive_recorder.clockmon.monitor import status_for
from signal_archive_recorder.firstrun.config_writer import SetupChoices, write
from signal_archive_recorder.firstrun.devices import LINUX_TIP, Candidate, rank
from signal_archive_recorder.firstrun.wizard import LevelReport, Wizard
from signal_archive_recorder.metadata.settings import normalise_callsign, normalise_grid
from signal_archive_recorder.sources.wsjtx.listener import SETUP_HELP
from signal_archive_recorder.ui.autostart import Autostart
from signal_archive_recorder.ui.worker import in_background
from signal_archive_recorder.upload.consent import CONSENT_TEXT
from signal_archive_recorder.upload.hub import DEFAULT_REPO, Identity, check_token
from signal_archive_recorder.upload.token import Token

log = logging.getLogger(__name__)

TOKEN_HELP = f"""\
<p>To upload recordings, the recorder needs a Hugging Face access token.</p>
<ol>
<li>Sign in (or sign up, free) at <a href="https://huggingface.co">huggingface.co</a>.</li>
<li>Open <a href="https://huggingface.co/settings/tokens">your token settings</a> and choose
<b>Create new token</b>.</li>
<li>Pick <b>Write</b> (simplest), or <b>Fine-grained</b> with write access to
<code>{DEFAULT_REPO}</code>.</li>
<li>Copy the token (it starts with <code>hf_</code>), paste it below and press <b>Check</b>.</li>
</ol>
<p>The token is kept in your system's keyring, never in a file.</p>"""


def _label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setOpenExternalLinks(True)
    return label


class TermsPage(QWizardPage):
    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.setTitle("Contribution terms")
        self.setSubTitle("Recordings you upload become part of an open archive.")
        text = QTextBrowser()
        text.setPlainText(CONSENT_TEXT)
        self.agree = QCheckBox("I agree to these terms")
        self.agree.toggled.connect(self.completeChanged)
        current = wizard.env.consent.load()
        self.already = current is not None and current.is_current()
        if self.already:
            self.agree.setChecked(True)
            self.agree.setText("You've already agreed to these terms")
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.agree)

    def isComplete(self) -> bool:
        return self.agree.isChecked()


class LoginPage(QWizardPage):
    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.w = wizard
        self.setTitle("Hugging Face login")
        self.setSubTitle("Recordings are uploaded as pull requests to the project's dataset.")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("hf_...")
        self.token.textChanged.connect(self._edited)
        self.check = QPushButton("Check")
        self.check.clicked.connect(self.check_token)
        self.token.returnPressed.connect(self.check_token)
        self.status = _label()
        layout = QVBoxLayout(self)
        layout.addWidget(_label(TOKEN_HELP))
        layout.addWidget(self.token)
        layout.addWidget(self.check)
        layout.addWidget(self.status)
        self._checked_existing = False

    def initializePage(self) -> None:
        if self._checked_existing or self.w.identity is not None:
            return
        self._checked_existing = True
        existing = self.w.env.tokens.get()
        if existing is None:
            return
        self.status.setText("Checking the token already stored on this computer...")
        in_background(
            self,
            lambda: check_token(self.w.hub, existing, self.w.env.repo_id),
            self._existing_ok,
            lambda exc: self.status.setText(
                f"The stored token no longer works ({exc}). Please add a new one."
            ),
        )

    def _existing_ok(self, identity: Identity) -> None:
        self.w.identity, self.w.new_token = identity, None
        self.status.setText(f"Already logged in as <b>{identity.username}</b>.")
        self.completeChanged.emit()

    def _edited(self) -> None:
        if self.w.new_token is not None or self.w.identity is not None:
            self.w.identity = self.w.new_token = None
            self.status.setText("")
            self.completeChanged.emit()

    def check_token(self) -> None:
        raw = self.token.text().strip()
        if not raw:
            return
        try:
            token = Token(raw)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.check.setEnabled(False)
        self.status.setText("Checking...")

        def ok(identity: Identity) -> None:
            self.check.setEnabled(True)
            self.w.identity, self.w.new_token = identity, token
            self.status.setText(f"Logged in as <b>{identity.username}</b>; the token can upload.")
            self.completeChanged.emit()

        def bad(exc: BaseException) -> None:
            self.check.setEnabled(True)
            self.status.setText(f"{exc}<br>Please try another token.")

        in_background(self, lambda: check_token(self.w.hub, token, self.w.env.repo_id), ok, bad)

    def isComplete(self) -> bool:
        return self.w.identity is not None


class AudioPage(QWizardPage):
    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.w = wizard
        self.setTitle("Audio input")
        self.setSubTitle("Pick the input that carries your radio's receive audio.")
        self.devices = QListWidget()
        self.devices.currentItemChanged.connect(self._picked)
        self.test = QPushButton("Test level (3 seconds)")
        self.test.clicked.connect(self.test_level)
        self.result = _label()
        self.tip = _label()
        layout = QVBoxLayout(self)
        layout.addWidget(self.devices)
        layout.addWidget(self.test)
        layout.addWidget(self.result)
        layout.addWidget(self.tip)
        self.candidates: list[Candidate] = []

    def initializePage(self) -> None:
        if self.candidates:
            return
        try:
            self.w.backend = self.w.env.backend_factory()
            found = self.w.backend.input_devices()
        except (DeviceUnavailableError, OSError) as exc:
            self.result.setText(f"Can't use the audio system: {exc}")
            return
        self.candidates = rank(found, self.w.env.system)
        if not self.candidates:
            self.result.setText("No audio inputs found. Connect the radio's audio and try again.")
            return
        recommended = [c for c in self.candidates if c.recommended]
        others = [c for c in self.candidates if not c.recommended]
        for heading, group in (("Recommended", recommended), ("Other inputs", others)):
            if not group:
                continue
            item = QListWidgetItem(heading)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            self.devices.addItem(item)
            for c in group:
                d = c.device
                text = f"{d.name}   [{d.host_api}, {native_rate(d)} Hz]"
                if c.reasons:
                    text += f"\n      {'; '.join(c.reasons)}"
                entry = QListWidgetItem(text)
                entry.setData(Qt.ItemDataRole.UserRole, d)
                self.devices.addItem(entry)
        self.devices.setCurrentRow(1)  # the top recommendation
        if self.w.env.system == "Linux":
            self.tip.setText(LINUX_TIP)

    def device(self) -> DeviceInfo | None:
        item = self.devices.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return data if isinstance(data, DeviceInfo) else None

    def _picked(self) -> None:
        self.w.level = None
        self.result.setText("Press Test level with the radio on and receiving.")
        self.completeChanged.emit()

    def test_level(self) -> None:
        device, backend = self.device(), self.w.backend
        if device is None or backend is None:
            return
        self.test.setEnabled(False)
        self.result.setText(f"Listening to {device.name}...")

        def done(report: LevelReport) -> None:
            self.test.setEnabled(True)
            self.w.level = report
            peak = "silence" if math.isinf(report.peak_dbfs) else f"{report.peak_dbfs:.1f} dBFS"
            advice = "" if report.verdict == "good" else " You can still use it, or pick another."
            self.result.setText(f"Peak level {peak}: <b>{report.verdict}</b>.{advice}")

        def failed(exc: BaseException) -> None:
            self.test.setEnabled(True)
            self.result.setText(f"Couldn't open it: {exc}")

        in_background(self, lambda: self.w.env.level_probe(backend, device), done, failed)

    def isComplete(self) -> bool:
        return self.device() is not None


class StationPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Your station (optional)")
        self.setSubTitle("Both are optional, and you choose what is shared.")
        self.callsign = QLineEdit()
        self.share = QCheckBox("Share my callsign with my recordings (credits you; it's public)")
        self.grid = QLineEdit()
        self.grid.setPlaceholderText("e.g. EN52 or EN52wa")
        self.precision = QComboBox()
        for label, value in (
            ("None (withheld)", 0),
            ("4 characters (about 100 km)", 4),
            ("6 characters (about 5 km)", 6),
            ("8 characters", 8),
        ):
            self.precision.addItem(label, value)
        self.precision.setCurrentIndex(1)
        self.error = _label()
        self.error.setStyleSheet("color: #c8322b;")
        form = QFormLayout()
        form.addRow("Callsign", self.callsign)
        form.addRow("", self.share)
        form.addRow("Grid locator", self.grid)
        form.addRow("Grid shared", self.precision)
        layout = QVBoxLayout(self)
        layout.addWidget(
            _label(
                "Not sharing your callsign keeps it out of everything uploaded, "
                "including decoded messages."
            )
        )
        layout.addLayout(form)
        layout.addWidget(self.error)

    def values(self) -> tuple[str | None, bool, str | None, int]:
        """Normalised callsign, sharing, grid and precision; raises ValueError if invalid."""
        call = normalise_callsign(self.callsign.text()) if self.callsign.text().strip() else None
        grid = normalise_grid(self.grid.text()) if self.grid.text().strip() else None
        return call, bool(call) and self.share.isChecked(), grid, int(self.precision.currentData())

    def validatePage(self) -> bool:
        try:
            self.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            return False
        self.error.setText("")
        return True


class ChecksPage(QWizardPage):
    """WSJT-X (receive-only listen) and the clock, run together."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.w = wizard
        self.setTitle("WSJT-X and clock")
        self.setSubTitle("Start WSJT-X now if you use it. These checks take up to 15 seconds.")
        self.wsjtx = _label()
        self.clock = _label()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>WSJT-X</b>"))
        layout.addWidget(self.wsjtx)
        layout.addWidget(QLabel("<b>Clock</b>"))
        layout.addWidget(self.clock)
        layout.addStretch()
        self.listening = False

    def initializePage(self) -> None:
        if self.listening:
            return
        self.listening = True
        self.wsjtx.setText("Listening for WSJT-X (receive only)...")
        in_background(
            self, self.w.env.wsjtx_probe, self._wsjtx_done, lambda exc: self._wsjtx_done(None)
        )
        if self.w.env.ntp_probe is None:
            self.clock.setText("Skipped.")
            return
        self.clock.setText("Asking a time server...")
        probe = self.w.env.ntp_probe
        in_background(
            self,
            lambda: probe("pool.ntp.org"),
            self._clock_done,
            lambda exc: self.clock.setText(
                f"Couldn't reach a time server ({exc}); the recorder keeps trying."
            ),
        )

    def _wsjtx_done(self, found: str | None) -> None:
        self.listening = False  # the port is free again, so recording can use it
        if found == "busy":
            self.wsjtx.setText(
                "Another program is already using UDP port 2237 (GridTracker or "
                f"JTAlert?).<br>{SETUP_HELP}"
            )
        elif found:
            self.wsjtx.setText(f"Found {found}.")
        else:
            self.wsjtx.setText(
                f"WSJT-X wasn't heard. That's fine: start it whenever you like.<br>{SETUP_HELP}"
            )
        self.w.wsjtx_checked = True
        self.completeChanged.emit()

    def _clock_done(self, answer: Any) -> None:
        status = status_for(answer.offset_s)
        direction = "behind" if answer.offset_s > 0 else "ahead of"
        text = f"Your clock is {abs(answer.offset_s):.3f} s {direction} true time ({status})."
        if status != "green":
            text += (
                " Recording still works (the offset is recorded for correction), but "
                "fixing your computer's time sync makes the recordings more useful."
            )
        self.clock.setText(text)

    def isComplete(self) -> bool:
        return self.w.wsjtx_checked  # don't record while the check still holds the port


class FinishPage(QWizardPage):
    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.w = wizard
        self.setTitle("Ready to record")
        self.summary = _label()
        self.autostart = QCheckBox("Start Signal Archive Recorder when I log in")
        self.autostart.setChecked(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.autostart)
        layout.addWidget(
            _label(
                "After Finish, recording starts and the recorder lives in the "
                "system tray. Click its icon for the status window."
            )
        )

    def initializePage(self) -> None:
        call, share, grid, precision = self.w.station.values()
        device = self.w.audio.device()
        lines = [
            f"Audio input: <b>{device.name if device else '?'}</b>",
            f"Hugging Face user: <b>{self.w.identity.username if self.w.identity else '?'}</b>",
            f"Callsign: {call or 'not set'}" + (" (shared)" if share else " (not shared)"),
            f"Grid: {grid or 'not set'}" + (f" ({precision} characters shared)" if grid else ""),
            f"Recordings are saved in {Path.home() / 'SignalArchive'}",
        ]
        self.summary.setText("<br>".join(lines))


class SetupWizard(QWizard):
    def __init__(
        self,
        env: Wizard,
        config_path: Path,
        *,
        autostart: Autostart | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.env = env
        self.config_path = config_path
        self.autostart_control = autostart or Autostart()
        self.hub = env.hub_factory()
        self.identity: Identity | None = None
        self.new_token: Token | None = None
        self.backend: Any = None
        self.level: LevelReport | None = None
        self.wsjtx_checked = False
        self.choices: SetupChoices | None = None
        self.setWindowTitle("Signal Archive Recorder setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(640, 520)

        welcome = QWizardPage()
        welcome.setTitle("Welcome to Signal Archive Recorder")
        layout = QVBoxLayout(welcome)
        layout.addWidget(
            _label(
                "It records the raw receive audio from your station for the <b>Signal Archive "
                "Project</b>: an open library of real radio signals for research and for building "
                "better decoders and detectors.<br><br>Setup takes a few minutes. Nothing is saved "
                "until you press Finish."
            )
        )
        self.terms = TermsPage(self)
        self.login = LoginPage(self)
        self.audio = AudioPage(self)
        self.station = StationPage()
        self.checks = ChecksPage(self)
        self.finish = FinishPage(self)
        for page in (
            welcome,
            self.terms,
            self.login,
            self.audio,
            self.station,
            self.checks,
            self.finish,
        ):
            self.addPage(page)

    def accept(self) -> None:
        """Finish: only now is anything saved."""
        try:
            self.save()
        except Exception as exc:
            log.exception("saving setup failed")
            QMessageBox.critical(self, "Setup", f"Couldn't save the settings:\n{exc}")
            return
        super().accept()

    def save(self) -> SetupChoices:
        device = self.audio.device()
        assert device is not None and self.identity is not None
        call, share, grid, precision = self.station.values()
        if not self.terms.already:
            self.env.consent.accept(self.env.now_ns())
        if self.new_token is not None:
            self.env.tokens.set(self.new_token)
        choices = SetupChoices(
            device=device.name,
            callsign=call,
            share_callsign=share,
            grid=grid,
            grid_precision=precision,
        )
        write(self.config_path, choices)
        try:
            if self.finish.autostart.isChecked():
                self.autostart_control.enable()
            else:
                self.autostart_control.disable()
        except OSError as exc:  # never a reason to lose the rest of setup
            log.warning("couldn't change start-at-login: %s", exc)
        self.choices = choices
        return choices
