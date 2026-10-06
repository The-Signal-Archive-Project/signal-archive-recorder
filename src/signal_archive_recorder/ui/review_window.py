# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Review & upload: see exactly what a session would share, then upload it.

Shows the same review and radio-audio checks as `signal-archive-recorder review`,
and uploads through the same Uploader as `upload`. All the work happens in
RecorderController, off the UI thread.
"""

from __future__ import annotations

from typing import Any, Protocol

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from signal_archive_recorder.ui.controller import SessionRow
from signal_archive_recorder.ui.worker import in_background

STATE_TEXT = {
    "queued": "waiting to upload",
    "uploading": "uploading",
    "pr_opened": "uploaded, in review",
    "validated": "uploaded and confirmed",
    "failed": "upload failed",
    "blocked": "kept back",
    "pr_missing": "pull request deleted",
}


class Reviewer(Protocol):
    def finished_sessions(self) -> list[SessionRow]: ...

    def describe(self, session_id: str) -> str: ...

    def upload(self, session_ids: list[str]) -> list[str]: ...


class ReviewWindow(QWidget):
    def __init__(self, controller: Reviewer, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.controller = controller
        self.setWindowTitle("Review & upload: Signal Archive Recorder")
        self.resize(900, 560)
        self.sessions = QListWidget()
        self.sessions.currentItemChanged.connect(self._selected)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFont("monospace"))
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        split = QSplitter()
        split.addWidget(self.sessions)
        split.addWidget(self.details)
        split.setSizes([280, 620])

        self.upload_one = QPushButton("Upload this session")
        self.upload_one.clicked.connect(self.upload_selected)
        self.upload_all = QPushButton("Upload all waiting")
        self.upload_all.clicked.connect(self.upload_waiting)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        buttons = QHBoxLayout()
        for b in (self.upload_one, self.upload_all, refresh):
            buttons.addWidget(b)
        buttons.addStretch()
        self.message = QLabel(
            "Select a session to see exactly what would be shared. Chunks that don't look "
            "like radio receive audio stay on this computer."
        )
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.message)
        self.rows: list[SessionRow] = []
        self.busy = False

    # -- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        in_background(self, self.controller.finished_sessions, self._show_rows, self._error)

    def _show_rows(self, rows: list[SessionRow]) -> None:
        keep = self.selected()
        self.rows = rows
        self.sessions.clear()
        for row in rows:
            state = STATE_TEXT.get(row.state, row.state)
            if not row.finished:
                state = "unfinished"
            item = QListWidgetItem(
                f"{row.session_id}\n  {row.chunks} chunks, {row.seconds / 60:.0f} min, {state}"
            )
            item.setData(Qt.ItemDataRole.UserRole, row.session_id)
            self.sessions.addItem(item)
            if row.session_id == keep:
                self.sessions.setCurrentItem(item)
        if not rows:
            self.details.setPlainText("No finished sessions yet.")
        self._update_buttons()

    def selected(self) -> str | None:
        item = self.sessions.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, str) else None

    def _selected(self) -> None:
        self._update_buttons()
        session_id = self.selected()
        if session_id is None:
            return
        self.details.setPlainText(f"Checking {session_id}...")

        def show(text: str) -> None:
            if self.selected() == session_id:  # still the one being looked at
                self.details.setPlainText(text)

        in_background(self, lambda: self.controller.describe(session_id), show, self._error)

    # -- uploading ----------------------------------------------------------------

    def _waiting(self) -> list[str]:
        return [r.session_id for r in self.rows if r.state == "queued" and r.finished]

    def _update_buttons(self) -> None:
        row = next((r for r in self.rows if r.session_id == self.selected()), None)
        ready = row is not None and row.finished and row.state == "queued"
        self.upload_one.setEnabled(not self.busy and ready)
        self.upload_all.setEnabled(not self.busy and bool(self._waiting()))

    def upload_selected(self) -> None:
        if session_id := self.selected():
            self._upload([session_id])

    def upload_waiting(self) -> None:
        self._upload(self._waiting())

    def _upload(self, session_ids: list[str]) -> None:
        if not session_ids:
            return
        self.busy = True
        self._update_buttons()
        self.message.setText(f"Uploading {len(session_ids)} session(s)... this can take a while.")
        in_background(
            self, lambda: self.controller.upload(session_ids), self._uploaded, self._error
        )

    def _uploaded(self, lines: list[str]) -> None:
        self.busy = False
        self.message.setText("\n".join(lines) or "Nothing to upload.")
        self.refresh()

    def _error(self, exc: Any) -> None:
        self.busy = False
        self.message.setText(f"Couldn't do that: {exc}")
        self._update_buttons()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.refresh()
