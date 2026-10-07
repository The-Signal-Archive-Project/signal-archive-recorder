# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Report a problem: save the diagnostics file, then go where reports are read."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from signal_archive_recorder import links
from signal_archive_recorder.updates import display_version


class ReportDialog(QDialog):
    def __init__(
        self, save_diagnostics: Callable[[], None] | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Report a problem")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)

        def text(html: str) -> QLabel:
            label = QLabel(html)
            label.setWordWrap(True)
            label.setOpenExternalLinks(True)
            layout.addWidget(label)
            return label

        text(f"Thank you! You're running <b>version {display_version()}</b>.")
        text(
            "<b>1. Save a diagnostics file.</b> It has no audio and no passwords, and your "
            "computer's name, user name, callsign and grid are blanked out. Attach it to "
            "your report."
        )
        self.save = QPushButton("Save diagnostics...")
        self.save.setEnabled(save_diagnostics is not None)
        if save_diagnostics is not None:
            self.save.clicked.connect(save_diagnostics)
        layout.addWidget(self.save)
        text("<b>2. Tell us what happened.</b>")
        self.opened: list[str] = []
        self.buttons: dict[str, QPushButton] = {}
        for key, label, url in (
            ("test", "Send a test report (GitHub form)", links.NEW_TEST_REPORT),
            ("bug", "Report a bug (GitHub form)", links.NEW_BUG_REPORT),
            ("group", "No GitHub account? Post to the group (groups.io)", links.GROUP),
            ("community", "Ask a quick question (Discord and more)", links.COMMUNITY),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, u=url: self.open_link(u))
            layout.addWidget(button)
            self.buttons[key] = button
        text(
            f"You can also email the group: <a href='mailto:{links.GROUP_EMAIL}'>"
            f"{links.GROUP_EMAIL}</a>"
        )
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    def open_link(self, url: str) -> None:
        self.opened.append(url)
        QDesktopServices.openUrl(QUrl(url))
