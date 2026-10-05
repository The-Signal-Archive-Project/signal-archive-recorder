# SPDX-License-Identifier: Apache-2.0
"""The capture pipeline: device callback -> ring buffer -> writer thread -> sinks.

The audio callback only copies into the ring buffer. If the buffer is full, the
frames that don't fit are dropped, counted exactly, and reported as one AudioGap per
continuous run of lost frames. The writer thread hands each sink the data along with
the stream frame index of its first frame, so a gap shows up as a jump in that index.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.audio.ringbuffer import RingBuffer
from signal_archive_recorder.audio.timeline import StreamTimeline
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.core.events import AudioGap, CaptureWarning

log = logging.getLogger(__name__)

SOURCE = "audio"


class Sink(Protocol):
    def write(self, data: bytes, stream_frame: int) -> None:
        """Receive contiguous frames starting at stream_frame (device frame index)."""
        ...


@dataclass(frozen=True)
class CaptureStats:
    delivered_frames: int
    written_frames: int
    lost_frames: int
    driver_overflows: int


@dataclass
class _Gap:
    at_written: int  # ring-buffer write position where the gap sits
    stream_frame: int
    lost: int


class Capture:
    def __init__(
        self,
        fmt: AudioFormat,
        *,
        bus: EventBus,
        clock: Clock,
        sinks: list[Sink],
        buffer_seconds: float = 10.0,
        read_seconds: float = 0.1,
        timeline: StreamTimeline | None = None,
    ) -> None:
        self.format = fmt
        self.timeline = timeline or StreamTimeline(fmt.sample_rate)
        self._bus = bus
        self._clock = clock
        self._sinks = sinks
        self._ring = RingBuffer(max(1, round(buffer_seconds * fmt.sample_rate)), fmt.frame_bytes)
        self._read_frames = max(1, round(read_seconds * fmt.sample_rate))

        # Audio-thread state.
        self._delivered = 0
        self._lost = 0
        self._overflows = 0
        self._open_gap: _Gap | None = None
        self.first_callback_ns: int | None = None

        # Overrun gaps in ring-buffer coordinates, so the writer can translate positions
        # into stream frame indices.
        self._gaps: deque[_Gap] = deque()
        self._written = 0
        self._stream_offset = 0  # stream_frame - ring position, grows by each gap

        self._stopping = threading.Event()
        self._writer = threading.Thread(target=self._run_writer, name="audio-writer", daemon=True)

    # -- audio thread ------------------------------------------------------------

    def on_audio(self, data: memoryview, frames: int, overflow: bool) -> None:
        """The device callback. Never blocks and never raises."""
        try:
            now = self._clock.now_ns()
            # The callback runs as a block finishes, so its first frame is one block older.
            block_start_ns = now - self.format.frames_to_ns(frames)
            if self.first_callback_ns is None:
                self.first_callback_ns = now
                self.timeline.anchor(0, block_start_ns)
            elif overflow:
                # The driver lost an unknown amount of audio: re-anchor the timeline.
                self.timeline.anchor(self._delivered, block_start_ns)
            if overflow:
                self._overflows += 1
                self._publish_gap(self._delivered, None, "driver_overflow")
            self.timeline.maybe_sync(self._delivered + frames, now)
            taken = self._ring.write(data)
            dropped = frames - taken
            if taken and self._open_gap is not None:
                self._close_gap()
            if dropped:
                if self._open_gap is None:
                    # Registered with the writer now, before any later frames reach the
                    # ring; its count is final by the time data beyond it exists.
                    self._open_gap = _Gap(
                        at_written=self._ring.total_written,
                        stream_frame=self._delivered + taken,
                        lost=0,
                    )
                    self._gaps.append(self._open_gap)
                self._open_gap.lost += dropped
                self._lost += dropped
            self._delivered += frames
        except Exception:  # pragma: no cover - last-resort guard for the audio thread
            log.exception("audio callback failed")

    def _close_gap(self) -> None:
        gap = self._open_gap
        assert gap is not None
        self._open_gap = None
        self._publish_gap(gap.stream_frame, gap.lost, "buffer_overrun")

    def _publish_gap(self, stream_frame: int, lost: int | None, reason: str) -> None:
        self._bus.publish(
            AudioGap(
                source=SOURCE,
                stream_frame=stream_frame,
                lost_frames=lost,
                est_t_ns=self.timeline.frame_to_ns(stream_frame),
                reason=reason,
            )
        )

    # -- control -----------------------------------------------------------------

    def start(self) -> None:
        self._writer.start()

    def stop(self, timeout: float | None = None) -> CaptureStats:
        """Call after the device stream has stopped: drains the buffer, then returns."""
        if self._open_gap is not None:
            self._close_gap()
        self._stopping.set()
        self._ring.wake()
        self._writer.join(timeout)
        return self.stats()

    def stats(self) -> CaptureStats:
        return CaptureStats(
            delivered_frames=self._delivered,
            written_frames=self._written,
            lost_frames=self._lost,
            driver_overflows=self._overflows,
        )

    # -- writer thread -----------------------------------------------------------

    def _run_writer(self) -> None:
        while True:
            stopping = self._stopping.is_set()
            limit = self._read_frames
            if self._gaps and self._gaps[0].at_written > self._written:
                limit = min(limit, self._gaps[0].at_written - self._written)
            data = self._ring.read(limit, timeout=0.05)
            if data:
                # Data beyond a gap exists, so that gap's lost count is final.
                while self._gaps and self._gaps[0].at_written == self._written:
                    self._stream_offset += self._gaps.popleft().lost
                self._deliver(data, self._written + self._stream_offset)
                self._written += len(data) // self.format.frame_bytes
            elif stopping and self._ring.used_frames == 0:
                return

    def _deliver(self, data: bytes, stream_frame: int) -> None:
        for sink in self._sinks:
            try:
                sink.write(data, stream_frame)
            except Exception as exc:
                log.exception("audio sink %r failed", sink)
                self._bus.publish(
                    CaptureWarning(source=SOURCE, code="sink_failed", message=f"{sink!r}: {exc}")
                )
