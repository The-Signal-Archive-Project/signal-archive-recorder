# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Warn before the disk fills up, while recording.

Free space is checked every minute. The operator is warned once when it drops
below "low" (an hour of recording at the current rate, and at least 2 GB), again
below "critical" (200 MB), and told when it recovers. Recording never stops here:
audio always comes first, and freeing space (deleting confirmed uploads) is the
storage manager's job.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.events import CaptureWarning

log = logging.getLogger(__name__)

GB = 1_000_000_000
MIN_LOW_BYTES = 2 * GB
CRITICAL_BYTES = 200_000_000


class Level(IntEnum):
    OK = 0
    LOW = 1
    CRITICAL = 2


@dataclass(frozen=True)
class DiskStatus:
    free_bytes: int
    level: Level
    hours_left: float


Usage = Callable[[Path], Any]  # like shutil.disk_usage: has .free


def check(path: Path, bytes_per_second: float, usage: Usage = shutil.disk_usage) -> DiskStatus:
    free = int(usage(path).free)
    low = max(MIN_LOW_BYTES, int(bytes_per_second * 3600))
    level = Level.CRITICAL if free < CRITICAL_BYTES else Level.LOW if free < low else Level.OK
    return DiskStatus(free, level, free / bytes_per_second / 3600 if bytes_per_second else 0.0)


class DiskMonitor:
    def __init__(
        self,
        bus: EventBus,
        path: Path,
        bytes_per_second: float,
        *,
        interval_s: float = 60.0,
        usage: Usage = shutil.disk_usage,
    ) -> None:
        self._bus = bus
        self._path = path
        self._rate = bytes_per_second
        self._interval = interval_s
        self._usage = usage
        self.level = Level.OK
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="disk-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except OSError:
                log.exception("couldn't check free disk space")
            if self._stop.wait(self._interval):
                return

    def check_once(self) -> DiskStatus:
        status = check(self._path, self._rate, self._usage)
        if status.level != self.level:
            self._announce(status)
            self.level = status.level
        return status

    def _announce(self, status: DiskStatus) -> None:
        gb = status.free_bytes / GB
        if status.level is Level.OK:
            code, message = "disk_ok", f"Disk space is fine again ({gb:.1f} GB free)."
        elif status.level is Level.LOW:
            code = "disk_low"
            message = (
                f"Disk space is getting low: {gb:.1f} GB free, about {status.hours_left:.1f} "
                "hours of recording. Upload and clear old sessions, or free some space."
            )
        else:
            code = "disk_critical"
            message = (
                f"Disk almost full: {gb * 1000:.0f} MB free. Recording will fail soon; free "
                "space now."
            )
        (log.info if status.level is Level.OK else log.warning)("%s", message)
        self._bus.publish(CaptureWarning(source="disk", code=code, message=message))
