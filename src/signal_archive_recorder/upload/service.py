# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Background uploading while `record` runs, on the operator's schedule.

Schedules:
- "manual": nothing happens in the background; use `signal-archive-recorder upload`.
- "while_recording": every few minutes, upload finished sessions (never the one
  being recorded) and follow up open pull requests.
- "overnight": the same, but only inside a local-time window such as 01:00-06:00.

Each round also applies the archive's disk limits (confirmed uploads only). A round
that can't upload (no consent yet, not logged in, offline) is logged and retried
next time; it never affects recording.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, time, tzinfo

from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.core.events import CaptureWarning
from signal_archive_recorder.session.cleanup import cleanup
from signal_archive_recorder.upload.queue import Uploader

log = logging.getLogger(__name__)

SCHEDULES = ("manual", "while_recording", "overnight")
_WINDOW = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> Window:
        m = _WINDOW.match(text.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(3)) > 23:
            raise ValueError(f"{text!r} isn't a time window like 01:00-06:00")
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        return cls(time(h1, m1), time(h2, m2))

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # wraps past midnight, e.g. 22:00-06:00


def allowed_now(schedule: str, window: Window, now_ns: int, tz: tzinfo | None = None) -> bool:
    if schedule == "while_recording":
        return True
    if schedule == "overnight":
        local = datetime.fromtimestamp(now_ns / 1e9, tz).astimezone(tz)
        return window.contains(local.time())
    return False


class UploadService:
    def __init__(
        self,
        uploader: Uploader,
        bus: EventBus,
        clock: Clock,
        *,
        schedule: str,
        window: Window,
        max_bytes: int | None = None,
        delete_after_days: float | None = None,
        interval_s: float = 300.0,
        tz: tzinfo | None = None,
    ) -> None:
        if schedule not in SCHEDULES:
            raise ValueError(f"schedule must be one of {SCHEDULES}")
        self.uploader = uploader
        self._bus = bus
        self._clock = clock
        self.schedule = schedule
        self.window = window
        self._max_bytes = max_bytes
        self._delete_after_days = delete_after_days
        self._interval = interval_s
        self._tz = tz
        self.rounds = 0
        self._last_problem: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="upload-service", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = 10.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self._interval):
                return

    def run_once(self) -> None:
        """One round: upload and follow up if the schedule allows, then tidy the disk."""
        self.rounds += 1
        try:
            if allowed_now(self.schedule, self.window, self._clock.now_ns(), self._tz):
                self.uploader.upload_all()
                self.uploader.poll_all()
            self._last_problem = None
        except Exception as exc:  # no consent, not logged in, offline...
            problem = f"{type(exc).__name__}: {exc}"
            if problem != self._last_problem:  # say it once, not every round
                log.warning("background upload paused: %s", problem)
                self._last_problem = problem
        self._tidy()

    def _tidy(self) -> None:
        if not (self._max_bytes or self._delete_after_days):
            return
        try:
            report = cleanup(
                self.uploader.storage,
                now_ns=self._clock.now_ns(),
                max_bytes=self._max_bytes,
                delete_after_days=self._delete_after_days,
            )
        except OSError:
            log.exception("disk cleanup failed")
            return
        if report.warning:
            self._bus.publish(CaptureWarning(source="storage", code="disk_limit",
                                             message=report.warning))  # fmt: skip
