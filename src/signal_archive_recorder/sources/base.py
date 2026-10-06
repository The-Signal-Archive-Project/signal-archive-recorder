# SPDX-License-Identifier: Apache-2.0
"""Running a source so that its failures can never reach audio capture.

A source's main loop runs on its own thread. If it raises, the supervisor
publishes SourceDown(reason="crashed: ...") and restarts it after a backoff that
doubles up to a limit. The rest of the recorder only ever sees events.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.events import SourceDown

log = logging.getLogger(__name__)

# The source's loop: runs until the stop event is set, publishing events as it goes.
SourceLoop = Callable[[threading.Event], None]


class SupervisedSource:
    def __init__(
        self,
        name: str,
        loop: SourceLoop,
        bus: EventBus,
        *,
        backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
    ) -> None:
        self.name = name
        self.crashes = 0
        self._loop = loop
        self._bus = bus
        self._backoff = backoff_s
        self._max_backoff = max_backoff_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"source-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        delay = self._backoff
        while not self._stop.is_set():
            try:
                self._loop(self._stop)
                delay = self._backoff  # a clean return resets the backoff
                if self._stop.wait(delay):  # never spin on a loop that returns at once
                    return
            except Exception as exc:
                self.crashes += 1
                log.exception("source %s crashed (restart in %.1f s)", self.name, delay)
                self._bus.publish(SourceDown(source=self.name, reason=f"crashed: {exc!r}"))
                if self._stop.wait(delay):
                    return
                delay = min(delay * 2, self._max_backoff)
