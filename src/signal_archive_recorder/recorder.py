# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The whole recorder, wired together in the order that keeps audio safe.

Start: recover crashed chunks, open the session, start sources, start the capture
writer, then open the device. Stop runs the other way: stop the device, drain the
audio, stop the sources (so their logs are complete), then close the session,
which finalises the last chunk and writes session.json.

A Recorder is one session. On its own (the e2e tests) it owns its event bus, WSJT-X
listener and upload service. Run by a `Station`, it borrows the station's
long-lived bus and listener, which outlive sessions so that the station can wait
for WSJT-X between them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from signal_archive_recorder.audio.capture import Capture, CaptureStats
from signal_archive_recorder.audio.channels import ChannelPicker, Keep, kept_format
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
from signal_archive_recorder.audio.levels import LiveLevel
from signal_archive_recorder.audio.sound_server import native_rate
from signal_archive_recorder.audio.timeline import StreamTimeline
from signal_archive_recorder.clockmon.monitor import ClockMonitor, NtpProbe, ntplib_probe
from signal_archive_recorder.config import RecorderConfig
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock, SystemClock
from signal_archive_recorder.core.events import CaptureWarning
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.paths import config_dir
from signal_archive_recorder.session.disk import DiskMonitor
from signal_archive_recorder.session.manager import SessionManager
from signal_archive_recorder.session.recovery import recover_storage
from signal_archive_recorder.session.storage import SessionDir, SessionStorage
from signal_archive_recorder.sources.wsjtx.listener import SETUP_HELP, WsjtxListener
from signal_archive_recorder.upload.consent import ConsentStore
from signal_archive_recorder.upload.hub import HfHub
from signal_archive_recorder.upload.queue import Uploader
from signal_archive_recorder.upload.service import UploadService, Window
from signal_archive_recorder.upload.token import TokenStore

log = logging.getLogger(__name__)


def select_device(backend: AudioBackend, wanted: str) -> DeviceInfo:
    devices = backend.input_devices()
    matches = [d for d in devices if wanted.lower() in d.name.lower()]
    if not matches:
        names = ", ".join(d.name for d in devices) or "none"
        raise DeviceUnavailableError(f'No input device matches "{wanted}". Found: {names}')
    exact = [d for d in matches if d.name.lower() == wanted.lower()]
    return (exact or matches)[0]


def default_uploader(cfg: RecorderConfig, clock: Clock) -> Uploader:
    return Uploader(
        SessionStorage(cfg.storage_root),
        HfHub(),
        TokenStore(),
        ConsentStore(config_dir() / "consent.json"),
        clock,
        repo_id=cfg.upload.repo,
        require_decoder=cfg.upload.require_decoder,
        max_bytes_per_s=cfg.upload.max_bytes_per_s,
    )


def needs_upload_service(cfg: RecorderConfig) -> bool:
    return cfg.upload.schedule != "manual" or bool(cfg.max_gb or cfg.delete_after_days)


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
        uploader_factory: Callable[[], Uploader] | None = None,
        bus: EventBus | None = None,
        listener: WsjtxListener | None = None,
        manage_uploads: bool = True,
    ) -> None:
        """`bus` and `listener` given: borrowed (a Station's); they're not started,
        stopped or closed here. `manage_uploads=False`: the owner runs uploads."""
        self.config = config
        self._shared_bus = bus
        self._shared_listener = listener
        self._manage_uploads = manage_uploads
        self.clock = clock or SystemClock()
        self.registry = registry or ModeRegistry.load_default()
        self._backend = backend
        self.bus: EventBus | None = None
        self.manager: SessionManager | None = None
        self.capture: Capture | None = None
        self.stream: InputStream | None = None
        self.listener: WsjtxListener | None = None
        self.clock_monitor: ClockMonitor | None = None
        self.disk_monitor: DiskMonitor | None = None
        self.upload_service: UploadService | None = None
        self.live_level: LiveLevel | None = None
        self._uploader_factory = uploader_factory or self._default_uploader
        self._ntp_probe = ntp_probe
        self.recovered: list[Recovery] = []

    def _make_backend(self) -> AudioBackend:
        if self._backend is not None:
            return self._backend
        audio = self.config.audio
        if audio.file is not None:
            return FileBackend(audio.file, speed=audio.file_speed, loop=audio.file_loop)
        return SoundDeviceBackend()

    def _default_uploader(self) -> Uploader:
        return default_uploader(self.config, self.clock)

    def _native_rate(self, device: DeviceInfo) -> int:
        """The device's true rate (asking the sound server on Linux)."""
        if self._backend is not None:  # injected backends (files, tests) know their own rate
            return device.default_sample_rate
        return native_rate(device)

    def start(self) -> SessionDir:
        cfg = self.config
        backend = self._make_backend()
        device = select_device(backend, cfg.audio.device or "")
        builder = MetadataBuilder(self.registry, cfg.station, extra_secrets=(device.name,))
        storage = SessionStorage(cfg.storage_root)
        self.recovered = recover_storage(storage, builder)
        for r in self.recovered:
            log.warning("recovery: %s: %s", r.partial.name, r.reason)

        self.bus = bus = self._shared_bus or EventBus(self.clock)
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
            native_rate=self._native_rate(device),
        )
        device_fmt = opened.delivered
        keep: Keep = cfg.audio.keep_channel
        if keep != "both" and device_fmt.channels != 2:
            log.info(
                "keep_channel = %s ignored: the input has %d channel(s)", keep, device_fmt.channels
            )
            keep = "both"
        fmt = kept_format(device_fmt, keep)
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
            channel_selection={"device_channels": 2, "kept": keep} if keep != "both" else None,
        )
        session = self.manager.start()
        for warning in opened.warnings:
            log.warning("%s", warning.message)
            bus.publish(warning)

        self.disk_monitor = DiskMonitor(bus, cfg.storage_root, fmt.sample_rate * fmt.frame_bytes)
        self.disk_monitor.start()

        if cfg.clock.enabled:
            self.clock_monitor = ClockMonitor(
                bus,
                self.clock,
                servers=cfg.clock.servers,
                interval_s=cfg.clock.interval_s,
                probe=self._ntp_probe,
            )
            self.clock_monitor.start()

        if self._shared_listener is not None:
            self.listener = self._shared_listener
            self.listener.attach(session.decode_log("wsjtx"))
            self.listener.announce()  # what WSJT-X reported before this session began
        elif cfg.wsjtx.enabled and self._shared_bus is None:  # a Station brings its own
            self.listener = WsjtxListener(
                bus,
                self.registry,
                self.clock,
                port=cfg.wsjtx.port,
                bind=cfg.wsjtx.bind,
                group=cfg.wsjtx.group,
                decode_log=session.decode_log("wsjtx"),
            )
            try:
                self.listener.start()
            except OSError as exc:  # usually: another program already has the port
                self.listener = None
                message = (
                    f"Couldn't listen for WSJT-X on UDP port {cfg.wsjtx.port} ({exc}). Recording "
                    f"continues without WSJT-X context. {SETUP_HELP}"
                )
                log.warning("%s", message)
                bus.publish(CaptureWarning(source="wsjtx", code="wsjtx_port_busy", message=message))

        self.live_level = LiveLevel(fmt)
        sinks: list[Any] = [self.manager, self.live_level]
        if keep != "both":

            def distinct(message: str) -> None:
                log.warning("%s", message)
                bus.publish(
                    CaptureWarning(source="audio", code="dropped_channel_active", message=message)
                )

            sinks = [ChannelPicker(device_fmt, keep, sinks, on_distinct=distinct)]
        self.capture = holder["capture"] = Capture(
            device_fmt,
            bus=bus,
            clock=self.clock,
            sinks=sinks,
            buffer_seconds=cfg.audio.buffer_seconds,
            timeline=timeline,
        )
        if self._manage_uploads and needs_upload_service(cfg):
            self.upload_service = UploadService(
                self._uploader_factory(),
                bus,
                self.clock,
                schedule=cfg.upload.schedule,
                window=Window.parse(cfg.upload.overnight_window),
                max_bytes=int(cfg.max_gb * 1e9) or None,
                delete_after_days=cfg.delete_after_days or None,
            )
            self.upload_service.start()

        self.capture.start()
        self.stream = opened.stream
        self.stream.start()
        log.info("recording %s to %s", fmt, session.path)
        return session

    def stop(self, reason: str = "stopped") -> RunSummary:
        assert self.manager is not None and self.capture is not None and self.bus is not None
        assert self.manager.session is not None
        if self.upload_service is not None:
            self.upload_service.stop()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
        stats = self.capture.stop(timeout=30)
        if self.listener is not None and self.listener is self._shared_listener:
            self.bus.wait_idle()
            self.listener.attach(None)  # this session's decode log is complete
        elif self.listener is not None:
            self.listener.stop()
        if self.clock_monitor is not None:
            self.clock_monitor.stop()
        if self.disk_monitor is not None:
            self.disk_monitor.stop()
        self.bus.wait_idle()
        chunks = self.manager.close(end_reason=reason)
        if self._shared_bus is None:
            self.bus.close()
        log.info(
            "stopped (%s): %d chunks, %d frames, %d lost",
            reason,
            len(chunks),
            stats.written_frames,
            stats.lost_frames,
        )
        return RunSummary(self.manager.session, stats, chunks, self.recovered)
