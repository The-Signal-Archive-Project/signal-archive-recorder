# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Check the computer clock against NTP at start-up and every 10 minutes.

The offset is recorded even when the OS says it's synchronised: "synchronised"
isn't a measurement. Thresholds: green under 0.1 s, yellow 0.1-0.5 s (recorded and
flagged), red over 0.5 s (the operator is warned). Recording continues regardless.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from signal_archive_recorder.clockmon.os_sync import OsSync, read_os_sync
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.core.events import CaptureWarning, ClockChecked

log = logging.getLogger(__name__)

SOURCE = "clock"
DEFAULT_SERVERS = ("pool.ntp.org", "time.cloudflare.com", "time.google.com")
YELLOW_S = 0.1
RED_S = 0.5


@dataclass(frozen=True)
class NtpAnswer:
    offset_s: float
    delay_s: float
    stratum: int


# Ask one server; raise on failure.
NtpProbe = Callable[[str], NtpAnswer]


def ntplib_probe(server: str) -> NtpAnswer:
    import ntplib

    r = ntplib.NTPClient().request(server, version=4, timeout=2)
    return NtpAnswer(float(r.offset), float(r.delay), int(r.stratum))


def status_for(offset_s: float | None) -> str:
    if offset_s is None:
        return "unknown"
    size = abs(offset_s)
    return "green" if size < YELLOW_S else "yellow" if size <= RED_S else "red"


class ClockMonitor:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        servers: Sequence[str] = DEFAULT_SERVERS,
        interval_s: float = 600.0,
        probe: NtpProbe = ntplib_probe,
        os_sync: Callable[[], OsSync] = read_os_sync,
    ) -> None:
        if not servers:
            raise ValueError("at least one NTP server is needed")
        self._bus = bus
        self._clock = clock
        self._servers = tuple(servers)
        self._interval = interval_s
        self._probe = probe
        self._os_sync = os_sync
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="clock-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:  # never let a clock problem reach audio
                log.exception("clock check failed")
            if self._stop.wait(self._interval):
                return

    def check_once(self) -> ClockChecked:
        os_state = self._os_sync()
        answer, server = None, None
        for candidate in self._servers:
            try:
                answer = self._probe(candidate)
                server = candidate
                break
            except Exception as exc:
                log.info("NTP server %s didn't answer: %s", candidate, exc)
        event = ClockChecked(
            source=SOURCE,
            measured_ns=self._clock.now_ns(),
            offset_s=answer.offset_s if answer else None,
            delay_s=answer.delay_s if answer else None,
            stratum=answer.stratum if answer else None,
            server=server,
            status=status_for(answer.offset_s if answer else None),
            os_synchronized=os_state.synchronized,
            os_sync_tool=os_state.tool,
        )
        self._bus.publish(event)
        if event.status == "red":
            self._bus.publish(
                CaptureWarning(
                    source=SOURCE,
                    code="clock_offset_red",
                    message=(
                        f"The computer clock is {event.offset_s:+.2f} s off true time. "
                        "Recording continues, but please fix the clock's time sync."
                    ),
                )
            )
            log.warning("clock offset %+.3f s", event.offset_s)
        return event
