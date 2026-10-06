# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The status window and tray icon (Qt, via PySide6 under the LGPL-3.0).

Small on purpose: a checklist, a level meter, pause/resume, "mark this" notes,
review & upload, diagnostics and shortcuts to the recordings and settings. Once
it's green, the operator can forget about it. Everything it shows comes from
RecorderController.snapshot(); it decides nothing itself.

`main()` is the whole desktop app: one instance per user, the setup wizard on
first start, then recording with the window and tray icon.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from signal_archive_recorder.paths import config_dir
from signal_archive_recorder.ui.autostart import Autostart
from signal_archive_recorder.ui.controller import SessionRow
from signal_archive_recorder.ui.health import CheckItem, HealthInputs, checklist, tray_state
from signal_archive_recorder.ui.review_window import ReviewWindow

log = logging.getLogger(__name__)

COLOURS = {
    "green": "#2e9d4a",
    "yellow": "#d9a400",
    "red": "#c8322b",
    "grey": "#8a8f98",
    "blue": "#2f6fd6",
}
LEVEL_COLOURS = {"ok": "green", "warn": "yellow", "bad": "red", "off": "grey"}
TRAY_TEXT = {
    "blue": "Ready: recording starts when WSJT-X runs",
    "green": "Recording; all good",
    "yellow": "Recording; something needs a look",
    "red": "Not recording",
    "grey": "Paused",
}
PRESET_NOTES = ("Strong QRM", "Rare DX", "Band opening", "Antenna change")
METER_FLOOR_DBFS = -90.0


class Controller(Protocol):
    """What the window needs from RecorderController (and from a fake in tests)."""

    paused: bool
    notes: list[str]

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self, reason: str = "stopped") -> None: ...

    def mark(self, text: str) -> bool: ...

    def snapshot(self) -> HealthInputs: ...

    def finished_sessions(self) -> list[SessionRow]: ...

    def describe(self, session_id: str) -> str: ...

    def upload(self, session_ids: list[str]) -> list[str]: ...


def dot(colour: str, size: int = 16) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(COLOURS[colour]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return pixmap


class StatusWindow(QWidget):
    def __init__(
        self,
        controller: Controller,
        *,
        recordings: Path,
        settings: Path,
        diagnostics: Callable[[Path], Path] | None = None,
        autostart: Autostart | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self._recordings = recordings
        self._settings = settings
        self._diagnostics = diagnostics
        self._autostart = autostart
        self.review: ReviewWindow | None = None
        self.state = "red"
        self.setWindowTitle("Signal Archive Recorder")
        self.setMinimumWidth(460)

        self.update_banner = QLabel()
        self.update_banner.setOpenExternalLinks(True)
        self.update_banner.setWordWrap(True)
        self.update_banner.setStyleSheet(
            "background: #2f6fd6; color: white; padding: 6px; border-radius: 4px;"
        )
        self.update_banner.hide()
        self.header_dot = QLabel()
        self.header = QLabel()
        self.header.setStyleSheet("font-size: 15px; font-weight: 600;")
        top = QHBoxLayout()
        top.addWidget(self.header_dot)
        top.addWidget(self.header, 1)

        self.rows: dict[str, tuple[QLabel, QLabel]] = {}
        grid = QGridLayout()
        for row, name in enumerate(("Recording", "Audio", "WSJT-X", "Clock", "Disk", "Uploads")):
            mark, detail = QLabel(), QLabel()
            detail.setWordWrap(True)
            grid.addWidget(mark, row, 0)
            grid.addWidget(QLabel(f"<b>{name}</b>"), row, 1)
            grid.addWidget(detail, row, 2)
            self.rows[name] = (mark, detail)
        grid.setColumnStretch(2, 1)

        self.meter = QProgressBar()
        self.meter.setRange(int(METER_FLOOR_DBFS), 0)
        self.meter.setFormat("%v dBFS")

        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("Mark this moment, e.g. strong QRM")
        self.note_input.returnPressed.connect(self._mark_typed)
        mark_button = QPushButton("Mark")
        mark_button.clicked.connect(self._mark_typed)
        notes_row = QHBoxLayout()
        notes_row.addWidget(self.note_input, 1)
        notes_row.addWidget(mark_button)
        presets = QHBoxLayout()
        for text in PRESET_NOTES:
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, t=text: self.mark(t))
            presets.addWidget(button)
        self.notes = QListWidget()
        self.notes.setMaximumHeight(80)

        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.upload_button = QPushButton("Review && upload...")
        self.upload_button.clicked.connect(self.open_review)
        open_button = QPushButton("Open recordings")
        open_button.clicked.connect(lambda: self._open(self._recordings))
        buttons = QHBoxLayout()
        for b in (self.pause_button, self.upload_button, open_button):
            buttons.addWidget(b)
        settings_button = QPushButton("Settings file")
        settings_button.clicked.connect(lambda: self._open(self._settings))
        self.diagnostics_button = QPushButton("Save diagnostics...")
        self.diagnostics_button.clicked.connect(self.save_diagnostics)
        self.diagnostics_button.setEnabled(diagnostics is not None)
        self.autostart_box = QCheckBox("Start when I log in")
        if autostart is not None:
            self.autostart_box.setChecked(autostart.enabled())
            self.autostart_box.toggled.connect(self.set_autostart)
        else:
            self.autostart_box.setEnabled(False)
        more = QHBoxLayout()
        for w in (settings_button, self.diagnostics_button, self.autostart_box):
            more.addWidget(w)
        self.message = QLabel()
        self.message.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.update_banner)
        layout.addLayout(top)
        layout.addLayout(grid)
        layout.addWidget(QLabel("Audio level"))
        layout.addWidget(self.meter)
        layout.addLayout(notes_row)
        layout.addLayout(presets)
        layout.addWidget(self.notes)
        layout.addLayout(buttons)
        layout.addLayout(more)
        layout.addWidget(self.message)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # -- display ------------------------------------------------------------------

    def refresh(self) -> None:
        health = self.controller.snapshot()
        self.state = tray_state(health)
        self.header_dot.setPixmap(dot(self.state, 18))
        self.header.setText(TRAY_TEXT[self.state])
        items: list[CheckItem] = checklist(health)
        for item in items:
            mark, detail = self.rows[item.name]
            mark.setPixmap(dot(LEVEL_COLOURS[item.level], 12))
            detail.setText(item.detail)
        peak = health.peak_dbfs if health.peak_dbfs is not None else METER_FLOOR_DBFS
        self.meter.setValue(int(max(METER_FLOOR_DBFS, min(0.0, peak))))
        self.pause_button.setText("Resume" if self.controller.paused else "Pause")
        release = getattr(self.controller, "update", None)
        if release is not None and self.update_banner.isHidden():
            self.update_banner.setText(
                f"Version {release.label} is available. "
                f'<a style="color: white;" href="{release.url}">Download it here</a>.'
            )
            self.update_banner.show()

    # -- actions ------------------------------------------------------------------

    def toggle_pause(self) -> None:
        try:
            if self.controller.paused:
                self.controller.resume()
                self.message.setText("Resumed: recording while WSJT-X runs (a new session).")
            else:
                self.controller.pause()
                self.message.setText(
                    "Paused: the session was saved, and nothing is recorded until you resume."
                )
        except Exception as exc:
            self.message.setText(f"Couldn't do that: {exc}")
        self.refresh()

    def mark(self, text: str) -> None:
        if self.controller.mark(text):
            self.notes.insertItem(0, text)
            self.message.setText(f'Marked: "{text}"')
        else:
            self.message.setText("Not recording, so there's nothing to mark.")

    def _mark_typed(self) -> None:
        self.mark(self.note_input.text())
        self.note_input.clear()

    def open_review(self) -> None:
        if self.review is None:
            self.review = ReviewWindow(self.controller, self)
        self.review.show()
        self.review.raise_()
        self.review.activateWindow()

    def save_diagnostics(self) -> None:
        if self._diagnostics is None:
            return
        default = str(Path.home() / "signal-archive-diagnostics.zip")
        target, _ = QFileDialog.getSaveFileName(self, "Save diagnostics", default, "Zip (*.zip)")
        if target:
            self.write_diagnostics(Path(target))

    def write_diagnostics(self, target: Path) -> None:
        assert self._diagnostics is not None
        try:
            saved = self._diagnostics(target)
        except Exception as exc:
            self.message.setText(f"Couldn't save diagnostics: {exc}")
            return
        self.message.setText(
            f"Diagnostics saved to {saved}. Attach it to your report; it "
            "holds no audio or token, and your names are redacted."
        )

    def set_autostart(self, on: bool) -> None:
        assert self._autostart is not None
        try:
            self._autostart.enable() if on else self._autostart.disable()
        except OSError as exc:
            self.message.setText(f"Couldn't change start at login: {exc}")

    def _open(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class Tray:
    """The tray icon: its colour follows the window's state."""

    def __init__(self, window: StatusWindow, quit_app: Any) -> None:
        self.window = window
        self._notified: Any = None
        self.icon = QSystemTrayIcon(QIcon(dot(window.state, 32)))
        menu = QMenu()
        show = QAction("Show status", menu)
        show.triggered.connect(self._show)
        self.pause = QAction("Pause", menu)
        self.pause.triggered.connect(window.toggle_pause)
        note = QAction("Mark this...", menu)
        note.triggered.connect(self._mark)
        review = QAction("Review && upload...", menu)
        review.triggered.connect(window.open_review)
        quit_action = QAction("Quit (saves the session)", menu)
        quit_action.triggered.connect(quit_app)
        for action in (show, self.pause, note, review, quit_action):
            menu.addAction(action)
        self._menu = menu
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(lambda reason: self._show())
        self.icon.show()
        window.timer.timeout.connect(self.refresh)

    def refresh(self) -> None:
        release = getattr(self.window.controller, "update", None)
        if release is not None and release != self._notified:
            self._notified = release
            self.icon.showMessage(
                "Signal Archive Recorder",
                f"Version {release.label} is available. Open the status window to download it.",
            )
        self.icon.setIcon(QIcon(dot(self.window.state, 32)))
        self.icon.setToolTip(f"Signal Archive Recorder: {TRAY_TEXT[self.window.state]}")
        self.pause.setText("Resume" if self.window.controller.paused else "Pause")

    def _show(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _mark(self) -> None:
        text, ok = QInputDialog.getText(None, "Mark this moment", "Note:")
        if ok and text:
            self.window.mark(text)


class _Window(StatusWindow):
    """With a tray, closing the window hides it; recording carries on."""

    has_tray = False

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.has_tray:
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)


class QuitOnSignals:
    """Ctrl-C, SIGTERM (logout, `kill`, systemd) and Ctrl-Break end the app cleanly.

    Python only runs signal handlers between bytecodes, which never happens while
    Qt's event loop is waiting, so a timer hands control back to Python regularly.
    """

    def __init__(self, app: QApplication) -> None:
        self.reason: str | None = None
        self._app = app
        self._timer = QTimer(app)
        self._timer.timeout.connect(lambda: None)
        self._timer.start(250)
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                with contextlib.suppress(ValueError, OSError):  # not on the main thread
                    signal.signal(getattr(signal, name), self._handle)

    def _handle(self, signum: int, frame: Any) -> None:
        log.info("signal %s: stopping", signum)
        self.reason = "signal"
        QTimer.singleShot(0, self._quit)

    def _quit(self) -> None:
        for widget in self._app.topLevelWidgets():  # a dialog (setup) runs its own loop
            if isinstance(widget, QDialog) and widget.isVisible():
                widget.reject()
        self._app.quit()


class SingleInstance:
    """One recorder per user: a second start just shows the first one's window."""

    def __init__(self, name: str | None = None) -> None:
        # Per user and settings folder, so tests (and a second profile) don't collide.
        who = f"{getpass.getuser()}|{config_dir()}"
        self.name = (
            name or f"signal-archive-recorder-{hashlib.sha256(who.encode()).hexdigest()[:12]}"
        )
        self.server = QLocalServer()
        self.on_show: Callable[[], None] = lambda: None

    def acquire(self) -> bool:
        """True if this is the only instance; otherwise asks the running one to show itself."""
        probe = QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(500):
            probe.write(b"show\n")
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            return False
        QLocalServer.removeServer(self.name)  # left over from a crash
        if not self.server.listen(self.name):
            log.warning("single-instance guard unavailable: %s", self.server.errorString())
            return True
        self.server.newConnection.connect(self._connection)
        return True

    def _connection(self) -> None:
        while (conn := self.server.nextPendingConnection()) is not None:
            conn.readyRead.connect(conn.readAll)
            conn.disconnected.connect(conn.deleteLater)
            self.on_show()

    def release(self) -> None:
        self.server.close()


def run(
    controller: Any,
    *,
    recordings: Path,
    settings: Path,
    diagnostics: Callable[[Path], Path] | None = None,
    autostart: Autostart | None = None,
    instance: SingleInstance | None = None,
) -> int:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    app.setApplicationName("Signal Archive Recorder")
    quit_signals = QuitOnSignals(app)
    try:
        controller.start()
    except Exception as exc:
        log.exception("couldn't start recording")
        QMessageBox.critical(None, "Signal Archive Recorder", f"Couldn't start recording:\n{exc}")
        return 3
    window = _Window(
        controller,
        recordings=recordings,
        settings=settings,
        diagnostics=diagnostics,
        autostart=autostart,
    )

    def show() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    if instance is not None:
        instance.on_show = show

    def quit_app() -> None:
        controller.stop(reason="quit")
        app.quit()

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        window.has_tray = True
        tray = Tray(window, quit_app)
        app.setQuitOnLastWindowClosed(False)
    window.show()
    result = app.exec()
    # However the loop ended (Quit, the last window closed, a signal, logging out of
    # the desktop), the session is finished and saved. Stopping twice is harmless.
    controller.stop(reason=quit_signals.reason or "quit")
    del tray
    return int(result)


def main(
    config_path: Path,
    *,
    setup_env: Callable[[], Any],
    load: Callable[[Path], Any],
    make_controller: Callable[[Any], Any],
    diagnostics: Callable[[Path], Path] | None = None,
) -> int:
    """The desktop app: set up if needed, then record in the tray."""
    from signal_archive_recorder.ui.setup_wizard import SetupWizard

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    app.setApplicationName("Signal Archive Recorder")
    app.setWindowIcon(QIcon(dot("green", 64)))
    QuitOnSignals(app)  # also during setup
    instance = SingleInstance()
    if not instance.acquire():
        log.info("already running; asked it to show its window")
        return 0
    try:
        if not config_path.exists():
            wizard = SetupWizard(setup_env(), config_path)
            if wizard.exec() != QDialog.DialogCode.Accepted:
                return 0
        try:
            config = load(config_path)
        except Exception as exc:
            QMessageBox.critical(
                None,
                "Signal Archive Recorder",
                f"There's a problem with the settings in {config_path}:\n{exc}",
            )
            return 2
        return run(
            make_controller(config),
            recordings=config.storage_root / "sessions",
            settings=config_path,
            diagnostics=diagnostics,
            autostart=Autostart(),
            instance=instance,
        )
    finally:
        instance.release()
