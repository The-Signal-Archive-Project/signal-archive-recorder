# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Logging to the console (when there is one) and to a rotating log file.

The log file is what testers attach to bug reports (through "Save diagnostics"),
so it's always on. Anything that looks like a Hugging Face token is masked before
it's written, as a second line of defence: tokens should never reach a log at all.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from signal_archive_recorder.paths import log_dir

LOG_FILE = "recorder.log"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_TOKEN = re.compile(r"hf_[A-Za-z0-9]{8,}")
_installed: list[logging.Handler] = []


class TokenMask(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if _TOKEN.search(message):
            record.msg, record.args = _TOKEN.sub("hf_****", message), None
        return True


def setup_logging(verbose: bool = False, *, to_file: bool = True) -> Path | None:
    """Configure logging once per process run; returns the log file, if any."""
    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
        handler.close()
    _installed.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    mask = TokenMask()

    if sys.stderr is not None:  # a windowed app on Windows has no console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(FORMAT))
        console.addFilter(mask)
        _installed.append(console)
    path = None
    if to_file:
        try:
            folder = log_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / LOG_FILE
            file = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=4, encoding="utf-8")
            file.setFormatter(logging.Formatter(FORMAT))
            file.addFilter(mask)
            _installed.append(file)
        except OSError:
            path = None  # an unwritable log folder never stops recording
    for handler in _installed:
        root.addHandler(handler)
    return path


def stop_file_logging() -> None:
    """Close the log file (before deleting the log folder, which Windows refuses while open)."""
    root = logging.getLogger()
    for handler in [h for h in _installed if isinstance(h, RotatingFileHandler)]:
        root.removeHandler(handler)
        handler.close()
        _installed.remove(handler)
