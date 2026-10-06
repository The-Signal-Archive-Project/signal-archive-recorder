# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The status window and tray icon (Qt, via PySide6 under the LGPL-3.0).

Small on purpose: a checklist, a level meter, pause/resume, "mark this" notes, an
"upload now" button and shortcuts to the recordings and settings. Once it's
green, the operator can forget about it. Everything it shows comes from
RecorderController.snapshot(); it decides nothing itself.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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

from signal_archive_recorder.ui.health import CheckItem, HealthInputs, checklist, tray_state

log = logging.getLogger(__name__)

COLOURS = {"green": "#2e9d4a", "yellow": "#d9a400", "red": "#c8322b", "grey": "#8a8f98"}
LEVEL_COLOURS = {"ok": "green", "warn": "yellow", "bad": "red", "off": "grey"}
TRAY_TEXT = {
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

    def upload_now(self) -> Any: ...


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


class _Done(QObject):
    finished = Signal(str)


class StatusWindow(QWidget):
    def __init__(self, controller: Controller, *, recordings: Path, settings: Path) -> None:
        super().__init__()
        self.controller = controller
        self._recordings = recordings
        self._settings = settings
        self.state = "red"
        self.setWindowTitle("Signal Archive Recorder")
        self.setMinimumWidth(460)

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
        self.upload_button = QPushButton("Upload now")
        self.upload_button.clicked.connect(self.upload_now)
        open_button = QPushButton("Open recordings")
        open_button.clicked.connect(lambda: self._open(self._recordings))
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(lambda: self._open(self._settings))
        buttons = QHBoxLayout()
        for b in (self.pause_button, self.upload_button, open_button, settings_button):
            buttons.addWidget(b)
        self.message = QLabel()
        self.message.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(grid)
        layout.addWidget(QLabel("Audio level"))
        layout.addWidget(self.meter)
        layout.addLayout(notes_row)
        layout.addLayout(presets)
        layout.addWidget(self.notes)
        layout.addLayout(buttons)
        layout.addWidget(self.message)

        self._done = _Done()
        self._done.finished.connect(self._upload_finished)
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

    # -- actions ------------------------------------------------------------------

    def toggle_pause(self) -> None:
        try:
            if self.controller.paused:
                self.controller.resume()
                self.message.setText("Recording again (a new session).")
            else:
                self.controller.pause()
                self.message.setText("Paused: the session was saved.")
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

    def upload_now(self) -> None:
        self.upload_button.setEnabled(False)
        self.message.setText("Uploading finished sessions...")

        def work() -> None:
            try:
                results = self.controller.upload_now()
                text = f"Upload done ({len(results)} sessions)."
            except Exception as exc:
                text = f"Upload didn't run: {exc}"
            self._done.finished.emit(text)

        threading.Thread(target=work, name="upload-now", daemon=True).start()

    def _upload_finished(self, text: str) -> None:
        self.upload_button.setEnabled(True)
        self.message.setText(text)
        self.refresh()

    def _open(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class Tray:
    """The tray icon: its colour follows the window's state."""

    def __init__(self, window: StatusWindow, quit_app: Any) -> None:
        self.window = window
        self.icon = QSystemTrayIcon(QIcon(dot(window.state, 32)))
        menu = QMenu()
        show = QAction("Show status", menu)
        show.triggered.connect(self._show)
        self.pause = QAction("Pause", menu)
        self.pause.triggered.connect(window.toggle_pause)
        note = QAction("Mark this...", menu)
        note.triggered.connect(self._mark)
        quit_action = QAction("Quit (saves the session)", menu)
        quit_action.triggered.connect(quit_app)
        for action in (show, self.pause, note, quit_action):
            menu.addAction(action)
        self._menu = menu
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(lambda reason: self._show())
        self.icon.show()
        window.timer.timeout.connect(self.refresh)

    def refresh(self) -> None:
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


def run(controller: Any, *, recordings: Path, settings: Path) -> int:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    app.setApplicationName("Signal Archive Recorder")
    try:
        controller.start()
    except Exception as exc:
        QMessageBox.critical(None, "Signal Archive Recorder", f"Couldn't start recording:\n{exc}")
        return 3
    window = _Window(controller, recordings=recordings, settings=settings)

    def quit_app() -> None:
        controller.stop(reason="quit")
        app.quit()

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        window.has_tray = True
        tray = Tray(window, quit_app)
        app.setQuitOnLastWindowClosed(False)
    else:
        app.aboutToQuit.connect(lambda: controller.stop(reason="quit"))
    window.show()
    result = app.exec()
    del tray
    return int(result)
