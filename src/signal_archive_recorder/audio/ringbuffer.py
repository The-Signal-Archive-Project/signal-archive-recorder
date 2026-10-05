# SPDX-License-Identifier: Apache-2.0
"""A single-producer, single-consumer byte ring buffer measured in whole frames.

The producer is the audio callback, which must never block: write() takes as many
frames as fit and reports how many. The lock only guards the indices; copies happen
outside it because the producer and consumer never touch the same region.
"""

from __future__ import annotations

import threading

BytesLike = bytes | bytearray | memoryview


class RingBuffer:
    def __init__(self, capacity_frames: int, frame_bytes: int) -> None:
        if capacity_frames <= 0 or frame_bytes <= 0:
            raise ValueError("capacity and frame size must be positive")
        self.capacity_frames = capacity_frames
        self.frame_bytes = frame_bytes
        self._buf = bytearray(capacity_frames * frame_bytes)
        self._read = 0  # total frames ever read
        self._written = 0  # total frames ever written
        self._lock = threading.Lock()
        self._readable = threading.Condition(self._lock)

    @property
    def used_frames(self) -> int:
        with self._lock:
            return self._written - self._read

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._written

    def write(self, data: BytesLike) -> int:
        """Copy in as many whole frames as fit; return how many were taken."""
        view = memoryview(data).cast("B")
        if len(view) % self.frame_bytes:
            raise ValueError("data is not a whole number of frames")
        with self._lock:
            free = self.capacity_frames - (self._written - self._read)
            start = self._written % self.capacity_frames
        frames = min(len(view) // self.frame_bytes, free)
        if frames == 0:
            return 0
        self._copy_in(start, view[: frames * self.frame_bytes])
        with self._lock:
            self._written += frames
            self._readable.notify()
        return frames

    def read(self, max_frames: int, timeout: float | None = None) -> bytes:
        """Copy out up to max_frames, waiting up to timeout for data. Empty on timeout."""
        with self._lock:
            if self._written == self._read:
                self._readable.wait(timeout)
            available = self._written - self._read
            start = self._read % self.capacity_frames
        frames = min(available, max_frames)
        if frames <= 0:
            return b""
        out = self._copy_out(start, frames)
        with self._lock:
            self._read += frames
        return out

    def _copy_in(self, start_frame: int, view: memoryview) -> None:
        fb = self.frame_bytes
        first = min(len(view), (self.capacity_frames - start_frame) * fb)
        self._buf[start_frame * fb : start_frame * fb + first] = view[:first]
        if first < len(view):
            self._buf[: len(view) - first] = view[first:]

    def _copy_out(self, start_frame: int, frames: int) -> bytes:
        fb = self.frame_bytes
        begin = start_frame * fb
        end = begin + frames * fb
        if end <= len(self._buf):
            return bytes(self._buf[begin:end])
        return bytes(self._buf[begin:]) + bytes(self._buf[: end - len(self._buf)])

    def wake(self) -> None:
        """Wake a reader blocked in read(), e.g. when shutting down."""
        with self._lock:
            self._readable.notify_all()
