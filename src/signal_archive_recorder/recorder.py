# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The whole recorder, wired together in the order that keeps audio safe.

Start: recover crashed chunks, open the session, start sources, start the capture
writer, then open the device. Stop runs the other way: stop the device, drain the
audio, stop the sources (so their logs are complete), then close the session,
which finalises the last chunk and writes session.json.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from signal_archive_recorder.audio.capture import Capture, CaptureStats
from signal_archive_recorder.audio.device import (
    AudioBackend,
    DeviceInfo,
    DeviceUnavailableError,
    InputStream,
    SoundDeviceBackend,
    open_input,
)
from signal_archive_recorder.audio.file_backend import FileBackend
from signal_archive_recorder.audio.flac_recovery import Recovery
from signal_archive_recorder.audio.timeline import StreamTimeline
from signal_archive_recorder.clockmon.monitor import ClockMonitor, NtpProbe, ntplib_probe
from signal_archive_recorder.config import RecorderConfig
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock, SystemClock
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.session.manager import SessionManager
from signal_archive_recorder.session.recovery import recover_storage
from signal_archive_recorder.session.storage import SessionDir, SessionStorage
from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener

log = logging.getLogger(__name__)


def select_device(backend: AudioBackend, wanted: str) -> DeviceInfo:
    devices = backend.input_devices()
    matches = [d for d in devices if wanted.lower() in d.name.lower()]
    if not matches:
        names = ", ".join(d.name for d in devices) or "none"
        raise DeviceUnavailableError(f'No input device matches "{wanted}". Found: {names}')
    exact = [d for d in matches if d.name.lower() == wanted.lower()]
    return (exact or matches)[0]


@dataclass
class RunSummary:
    session: SessionDir
    capture: CaptureStats
    chunks: list[dict[str, Any]]
    recovered: list[Recovery] = field(default_factory=list)


class Recorder:
    def __init__(
        self,
        config: RecorderConfig,
        *,
        backend: AudioBackend | None = None,
        clock: Clock | None = None,
        registry: ModeRegistry | None = None,
        ntp_probe: NtpProbe = ntplib_probe,
    ) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.registry = registry or ModeRegistry.load_default()
        self._backend = backend
        self.bus: EventBus | None = None
        self.manager: SessionManager | None = None
        self.capture: Capture | None = None
        self.stream: InputStream | None = None
        self.listener: WsjtxListener | None = None
        self.clock_monitor: ClockMonitor | None = None
        self._ntp_probe = ntp_probe
        self.recovered: list[Recovery] = []

    def _make_backend(self) -> AudioBackend:
        if self._backend is not None:
            return self._backend
        audio = self.config.audio
        if audio.file is not None:
            return FileBackend(audio.file, speed=audio.file_speed, loop=audio.file_loop)
        return SoundDeviceBackend()

    def start(self) -> SessionDir:
        cfg = self.config
        backend = self._make_backend()
        device = select_device(backend, cfg.audio.device or "")
        builder = MetadataBuilder(self.registry, cfg.station, extra_secrets=(device.name,))
        storage = SessionStorage(cfg.storage_root)
        self.recovered = recover_storage(storage, builder)
        for r in self.recovered:
            log.warning("recovery: %s: %s", r.partial.name, r.reason)

        self.bus = bus = EventBus(self.clock)
        # Open the device before creating the session, so a missing device fails early
        # and leaves no empty session behind. The stream isn't started until the end.
        holder: dict[str, Capture] = {}
        opened = open_input(
            backend,
            device,
            lambda data, frames, overflow: holder["capture"].on_audio(data, frames, overflow),
            sample_format=cfg.audio.sample_format,
            channels=cfg.audio.channels,
            sample_rate=cfg.audio.sample_rate,
        )
        fmt = opened.delivered
        timeline = StreamTimeline(fmt.sample_rate)
        self.manager = SessionManager(
            storage=storage,
            fmt=fmt,
            registry=self.registry,
            clock=self.clock,
            bus=bus,
            timeline=timeline,
            target_chunk_s=cfg.target_chunk_s,
            builder=builder,
        )
        session = self.manager.start()
        for warning in opened.warnings:
            log.warning("%s", warning.message)
            bus.publish(warning)

        if cfg.clock.enabled:
            self.clock_monitor = ClockMonitor(
                bus,
                self.clock,
                servers=cfg.clock.servers,
                interval_s=cfg.clock.interval_s,
                probe=self._ntp_probe,
            )
            self.clock_monitor.start()

        if cfg.wsjtx.enabled:
            self.listener = WsjtxListener(
                bus,
                self.registry,
                self.clock,
                port=cfg.wsjtx.port,
                bind=cfg.wsjtx.bind,
                group=cfg.wsjtx.group,
                decode_log=session.decode_log("wsjtx"),
            )
            self.listener.start()

        self.capture = holder["capture"] = Capture(
            fmt,
            bus=bus,
            clock=self.clock,
            sinks=[self.manager],
            buffer_seconds=cfg.audio.buffer_seconds,
            timeline=timeline,
        )
        self.capture.start()
        self.stream = opened.stream
        self.stream.start()
        log.info("recording %s to %s", fmt, session.path)
        return session

    def stop(self, reason: str = "stopped") -> RunSummary:
        assert self.manager is not None and self.capture is not None and self.bus is not None
        assert self.manager.session is not None
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
        stats = self.capture.stop(timeout=30)
        if self.listener is not None:
            self.listener.stop()
        if self.clock_monitor is not None:
            self.clock_monitor.stop()
        self.bus.wait_idle()
        chunks = self.manager.close(end_reason=reason)
        self.bus.close()
        log.info(
            "stopped (%s): %d chunks, %d frames, %d lost",
            reason,
            len(chunks),
            stats.written_frames,
            stats.lost_frames,
        )
        return RunSummary(self.manager.session, stats, chunks, self.recovered)
