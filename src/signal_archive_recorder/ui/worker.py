# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Run slow work (network, audio tests, uploads) off the UI thread."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, Qt, Signal

log = logging.getLogger(__name__)


class Task(QObject):
    """One background call; its result comes back on the UI thread."""

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.running = False


_running: set[Task] = set()  # kept alive here until they report back


def in_background(
    owner: QObject,
    work: Callable[[], Any],
    done: Callable[[Any], None],
    error: Callable[[BaseException], None] | None = None,
) -> Task:
    """Run `work` on a thread; `done` or `error` runs on the UI thread, unless the
    owner (the window that asked) has been closed and deleted by then."""
    task = Task()
    _running.add(task)

    def deliver(callback: Callable[[Any], None] | None, value: Any) -> None:
        _finish(task)
        if callback is not None and shiboken6.isValid(owner):
            callback(value)

    queued = Qt.ConnectionType.QueuedConnection  # always delivered on the UI thread
    task.succeeded.connect(lambda value: deliver(done, value), queued)
    task.failed.connect(lambda exc: deliver(error, exc), queued)

    def run() -> None:
        try:
            result = work()
        except Exception as exc:
            log.warning("background task failed: %s: %s", type(exc).__name__, exc)
            task.failed.emit(exc)
            return
        task.succeeded.emit(result)

    task.running = True
    threading.Thread(target=run, name="ui-task", daemon=True).start()
    return task


def _finish(task: Task) -> None:
    task.running = False
    _running.discard(task)
    task.deleteLater()
