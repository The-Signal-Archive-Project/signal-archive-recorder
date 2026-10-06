# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The only place the recorder reads the system clock.

Everything else takes a Clock, so tests can drive time with FakeClock.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now_ns(self) -> int:
        """Wall-clock UTC time, nanoseconds since the Unix epoch."""
        ...

    def monotonic_ns(self) -> int:
        """Monotonic time for measuring intervals and timeouts; arbitrary origin."""
        ...


class SystemClock:
    def now_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class FakeClock:
    """A thread-safe clock that only moves when told to.

    With auto_advance_ns set, every now_ns() call also moves time forward by that much,
    which gives distinct, increasing timestamps without manual stepping.
    """

    def __init__(self, start_ns: int = 0, *, auto_advance_ns: int = 0) -> None:
        self._now = start_ns
        self._mono = 0
        self._auto = auto_advance_ns
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        with self._lock:
            now = self._now
            self._now += self._auto
            self._mono += self._auto
            return now

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._mono

    def advance(self, ns: int) -> None:
        if ns < 0:
            raise ValueError("use set_wall_ns() to step the wall clock backwards")
        with self._lock:
            self._now += ns
            self._mono += ns

    def set_wall_ns(self, now_ns: int) -> None:
        """Step the wall clock (like an NTP correction); monotonic time is unaffected."""
        with self._lock:
            self._now = now_ns
