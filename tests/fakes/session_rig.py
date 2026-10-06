# SPDX-License-Identifier: Apache-2.0
"""A whole recorder on a fake clock: fake sound card, capture, bus and session manager."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from signal_archive_recorder.audio.capture import Capture
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.audio.timeline import StreamTimeline
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import Event
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.session.manager import SessionManager
from signal_archive_recorder.session.storage import SessionStorage
from tests.fakes.fake_audio import FakeAudioDevice, noise

S = 10**9
FMT = AudioFormat(8_000, 1, "int16")  # low rate keeps 12-minute sessions fast
REGISTRY = ModeRegistry.load_default()


def utc_ns(hhmmss: str, day: str = "2026-10-05") -> int:
    return int(datetime.fromisoformat(f"{day}T{hhmmss}").replace(tzinfo=UTC).timestamp()) * S


def frames(seconds: float) -> int:
    return round(seconds * FMT.sample_rate)


class Rig:
    """Fake sound card + capture + bus + session manager, all on one fake clock."""

    def __init__(self, root: Path, start: str = "12:03:07", seconds: float = 720, **kw: Any):
        self.clock = FakeClock(utc_ns(start))
        self.bus = EventBus(self.clock)
        self.timeline = StreamTimeline(FMT.sample_rate)
        self.manager = SessionManager(
            storage=SessionStorage(root),
            fmt=FMT,
            registry=REGISTRY,
            clock=self.clock,
            bus=self.bus,
            timeline=self.timeline,
            **kw,
        )
        self.session = self.manager.start()
        self.capture = Capture(
            FMT,
            bus=self.bus,
            clock=self.clock,
            sinks=[self.manager],
            timeline=self.timeline,
            buffer_seconds=seconds + 10,  # audio is pumped far faster than real time
        )
        self.capture.start()
        self.data = noise(FMT, seconds, seed=1)
        self.rate_hz = float(FMT.sample_rate)  # the sound card's true rate, for drift tests
        self.device = FakeAudioDevice(FMT, self.data, self._callback, block_frames=(480, 97, 640))

    def _callback(self, data: memoryview, n: int, overflow: bool) -> None:
        self.clock.advance(round(n * S / self.rate_hz))  # real time passes as audio arrives
        self.capture.on_audio(data, n, overflow)

    def pump_to(self, seconds: float) -> None:
        """Deliver audio up to `seconds` into the stream."""
        delivered = self.capture.stats().delivered_frames
        self.device.pump(frames(seconds) - delivered)

    def publish(self, event: Event) -> None:
        self.bus.publish(event)
        assert self.bus.wait_idle()

    def finish(self) -> list[dict[str, Any]]:
        self.device.pump()
        self.capture.stop(timeout=10)
        self.bus.wait_idle()
        chunks = self.manager.close()
        self.bus.close()
        return sorted(chunks, key=lambda c: c["index"])
