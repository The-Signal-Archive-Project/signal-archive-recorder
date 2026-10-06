# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The station: waits for WSJT-X, and records only while it runs.

With `[recording] start = "with_decoder"` (the default), the station stands by with
the sound card closed and only the receive-only WSJT-X listener running. When
WSJT-X appears (its first heartbeat or status), a session starts. When it goes away
(a Close message, or 30 s without a heartbeat), the session is finished and saved,
the sound card is released, and the station stands by again. With `"always"`, one
session runs from start to stop.

Long-lived here: the event bus, the WSJT-X listener and the upload service (so
background uploads carry on between sessions). Each session is a `Recorder`, which
borrows the bus and listener. Starting and stopping sessions happens on the
station's own thread, never on the bus thread, because stopping waits for the bus.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any, Literal

from signal_archive_recorder.clockmon.monitor import NtpProbe, ntplib_probe
from signal_archive_recorder.config import RecorderConfig
from signal_archive_recorder.core.bus import EventBus, Subscriber
from signal_archive_recorder.core.clock import Clock, SystemClock
from signal_archive_recorder.core.events import CaptureWarning, SourceDown, SourceUp, Stamped
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.recorder import (
    Recorder,
    RunSummary,
    default_uploader,
    needs_upload_service,
)
from signal_archive_recorder.session.storage import SessionDir
from signal_archive_recorder.sources.wsjtx.listener import SETUP_HELP, WsjtxListener
from signal_archive_recorder.upload.queue import Uploader
from signal_archive_recorder.upload.service import UploadService, Window

log = logging.getLogger(__name__)

State = Literal["stopped", "standby", "recording", "paused"]
DECODERS = ("wsjtx",)  # sources whose presence means "the station is on the air"
RETRY_S = 30.0  # after a failed session start (sound card unplugged...), try again


class Station:
    def __init__(
        self,
        config: RecorderConfig,
        *,
        clock: Clock | None = None,
        registry: ModeRegistry | None = None,
        recorder_factory: Callable[..., Recorder] = Recorder,
        recorder_kwargs: dict[str, Any] | None = None,
        ntp_probe: NtpProbe = ntplib_probe,
        uploader_factory: Callable[[], Uploader] | None = None,
        on_session: Callable[[SessionDir | None, RunSummary | None], None] | None = None,
    ) -> None:
        """`on_session(started, None)` when a session starts, `(None, summary)` when one ends
        (called on the station's thread)."""
        self.config = config
        self.clock = clock or SystemClock()
        self.registry = registry or ModeRegistry.load_default()
        self._recorder_factory = recorder_factory
        self._recorder_kwargs = {"ntp_probe": ntp_probe, **(recorder_kwargs or {})}
        self._uploader_factory = uploader_factory
        self._on_session = on_session or (lambda started, ended: None)
        self.bus: EventBus | None = None
        self.listener: WsjtxListener | None = None
        self.upload_service: UploadService | None = None
        self.recorder: Recorder | None = None
        self.summaries: list[RunSummary] = []
        self.last_error: str | None = None
        self._decoders_up: set[tuple[str, str]] = set()
        self._paused = False
        self._running = False
        self._stop_reason = "stopped"
        self._wake = threading.Condition()
        self._transition = threading.RLock()  # one session start/stop at a time
        self._thread: threading.Thread | None = None
        self._next_try_mono_ns = 0

    # -- state ----------------------------------------------------------------------

    @property
    def always(self) -> bool:
        return self.config.start == "always"

    @property
    def decoder_up(self) -> bool:
        with self._wake:
            return bool(self._decoders_up)

    @property
    def state(self) -> State:
        if not self._running:
            return "stopped"
        if self.recorder is not None:
            return "recording"
        return "paused" if self._paused else "standby"

    @property
    def session(self) -> SessionDir | None:
        recorder = self.recorder
        return recorder.manager.session if recorder and recorder.manager else None

    def _wanted(self) -> bool:
        return self._running and not self._paused and (self.always or bool(self._decoders_up))

    # -- control ----------------------------------------------------------------------

    def start(self, subscribers: Iterable[Subscriber] = ()) -> None:
        """Start listening (and recording at once if `always`). With `always`, a sound
        card problem raises here, as `record` always did; otherwise it's retried.
        `subscribers` see every event from the very start."""
        cfg = self.config
        self.bus = bus = EventBus(self.clock)
        bus.subscribe(self._on_event)
        for subscriber in subscribers:
            bus.subscribe(subscriber)
        if cfg.wsjtx.enabled:
            listener = WsjtxListener(
                bus,
                self.registry,
                self.clock,
                port=cfg.wsjtx.port,
                bind=cfg.wsjtx.bind,
                group=cfg.wsjtx.group,
            )
            try:
                listener.start()
                self.listener = listener
            except OSError as exc:  # usually: another program already has the port
                self.last_error = (
                    f"Couldn't listen for WSJT-X on UDP port {cfg.wsjtx.port} ({exc}), so the "
                    f"recorder can't tell when to record. {SETUP_HELP}"
                )
                log.warning("%s", self.last_error)
                bus.publish(
                    CaptureWarning(source="wsjtx", code="wsjtx_port_busy", message=self.last_error)
                )
        if needs_upload_service(cfg):
            uploader = (self._uploader_factory or (lambda: default_uploader(cfg, self.clock)))()
            self.upload_service = UploadService(
                uploader,
                bus,
                self.clock,
                schedule=cfg.upload.schedule,
                window=Window.parse(cfg.upload.overnight_window),
                max_bytes=int(cfg.max_gb * 1e9) or None,
                delete_after_days=cfg.delete_after_days or None,
            )
            self.upload_service.start()
        self._running = True
        if self.always:
            try:
                self._start_session(raise_errors=True)
            except BaseException:
                self.stop()
                raise
        else:
            log.info("standing by: recording starts when WSJT-X is running")
        self._thread = threading.Thread(target=self._run, name="station", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "stopped") -> list[RunSummary]:
        with self._wake:
            self._running = False
            self._stop_reason = reason
            self._wake.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(60)
        self._end_session(reason)
        if self.upload_service is not None:
            self.upload_service.stop()
            self.upload_service = None
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        if self.bus is not None:
            self.bus.close()
        return self.summaries

    def pause(self) -> None:
        """Don't record, even with WSJT-X running (the current session is saved)."""
        with self._wake:
            self._paused = True
            self._wake.notify_all()
        self._sync()

    def resume(self) -> None:
        with self._wake:
            self._paused = False
            self._next_try_mono_ns = 0
            self._wake.notify_all()
        self._sync()

    def _sync(self) -> None:
        """Bring the session in line with what's wanted now, then let the thread carry on."""
        if self._wanted():
            self._start_session()
        else:
            self._end_session("paused")

    # -- the station thread -------------------------------------------------------------

    def _on_event(self, stamped: Stamped) -> None:
        e = stamped.event
        if e.source not in DECODERS or not isinstance(e, (SourceUp, SourceDown)):
            return
        key = (e.source, str(e.raw.get("client_id", "")))
        with self._wake:
            if isinstance(e, SourceUp):
                self._decoders_up.add(key)
            else:
                self._decoders_up.discard(key)
            self._wake.notify_all()

    def _run(self) -> None:
        while True:
            with self._wake:
                if not self._running:
                    return
                self._wake.wait(1.0)
                if not self._running:
                    return
                wanted = self._wanted()
            try:
                if wanted and self.recorder is None:
                    if self.clock.monotonic_ns() >= self._next_try_mono_ns:
                        self._start_session()
                elif not wanted and self.recorder is not None:
                    self._end_session("paused" if self._paused else "decoder_closed")
            except Exception:  # the station thread must survive anything
                log.exception("station: session change failed")

    def _start_session(self, raise_errors: bool = False) -> None:
        with self._transition:
            self._start_session_locked(raise_errors)

    def _start_session_locked(self, raise_errors: bool) -> None:
        if self.recorder is not None or not self._wanted():
            return
        assert self.bus is not None
        recorder = self._recorder_factory(
            self.config,
            clock=self.clock,
            registry=self.registry,
            bus=self.bus,
            listener=self.listener,
            manage_uploads=False,
            **self._recorder_kwargs,
        )
        try:
            session = recorder.start()
        except Exception as exc:
            self.last_error = f"Couldn't start recording: {exc}"
            self._next_try_mono_ns = self.clock.monotonic_ns() + int(RETRY_S * 1e9)
            log.warning("%s (trying again in %.0f s)", self.last_error, RETRY_S)
            self.bus.publish(
                CaptureWarning(
                    source="recorder", code="session_start_failed", message=self.last_error
                )
            )
            if raise_errors:
                raise
            return
        self.last_error = None
        self.recorder = recorder
        log.info("recording started: %s", session.path)
        self._on_session(session, None)

    def _end_session(self, reason: str) -> None:
        with self._transition:
            self._end_session_locked(reason)

    def _end_session_locked(self, reason: str) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        if not self._running:
            reason = self._stop_reason
        try:
            summary = recorder.stop(reason=reason)
        finally:
            self.recorder = None  # only now: "recording" lasts until the session is saved
        self.summaries.append(summary)
        log.info("recording stopped (%s): %s", reason, summary.session.path)
        self._on_session(None, summary)
