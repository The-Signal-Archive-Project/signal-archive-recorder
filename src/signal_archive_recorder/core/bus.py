# SPDX-License-Identifier: Apache-2.0
"""The session event bus.

Any thread may publish. Each event is stamped with the clock and a sequence number
under one lock, then delivered in that order by a single dispatcher thread, so every
subscriber sees the same total order. A subscriber that raises is logged and skipped;
it never stops delivery to the others.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.core.events import Event, Stamped

log = logging.getLogger(__name__)

Subscriber = Callable[[Stamped], None]
_STOP = object()


class BusClosedError(RuntimeError):
    pass


class EventBus:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._queue: queue.SimpleQueue[Stamped | object] = queue.SimpleQueue()
        self._subscribers: list[Subscriber] = []
        self._subs_lock = threading.Lock()
        self._dispatcher = threading.Thread(target=self._run, name="event-bus", daemon=True)
        self._dispatcher.start()

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """Register a subscriber; returns a function that unsubscribes it."""
        with self._subs_lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._subs_lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def publish(self, event: Event) -> Stamped:
        with self._lock:
            if self._closed:
                raise BusClosedError("bus is closed")
            stamped = Stamped(t_ns=self._clock.now_ns(), seq=self._seq, event=event)
            self._seq += 1
            self._queue.put(stamped)
        return stamped

    def close(self, timeout: float | None = None) -> None:
        """Stop accepting events, deliver everything already published, then stop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(_STOP)
        self._dispatcher.join(timeout)

    def _run(self) -> None:
        while (item := self._queue.get()) is not _STOP:
            assert isinstance(item, Stamped)
            with self._subs_lock:
                subscribers = list(self._subscribers)
            for subscriber in subscribers:
                try:
                    subscriber(item)
                except Exception:
                    log.exception("event subscriber %r failed on %r", subscriber, item)
