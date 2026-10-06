# SPDX-License-Identifier: Apache-2.0
"""Mapping between stream frame positions and UTC time.

The audio stream is the session's clock. Frame f of the stream is at
anchor_t + (f - anchor_frame) / sample_rate, using the latest anchor at or before
f. The first audio callback sets the first anchor; a driver overflow (which loses
an unknown amount of audio) sets a new one. Nothing here corrects for the sound
card's clock drifting: instead, wall-clock sync points are recorded about once a
second so the pipeline can estimate drift from the data.
"""

from __future__ import annotations

import bisect
import threading

NS_PER_S = 1_000_000_000


class StreamTimeline:
    def __init__(self, sample_rate: int, *, sync_every_s: float = 1.0) -> None:
        self.sample_rate = sample_rate
        self._anchor_frames: list[int] = []
        self._anchor_ns: list[int] = []
        self._sync: list[tuple[int, int]] = []
        self._sync_every = max(1, round(sync_every_s * sample_rate))
        self._lock = threading.Lock()

    @property
    def started(self) -> bool:
        with self._lock:
            return bool(self._anchor_frames)

    def anchor(self, frame: int, t_ns: int) -> None:
        with self._lock:
            if self._anchor_frames and frame <= self._anchor_frames[-1]:
                raise ValueError("anchors must move forward")
            self._anchor_frames.append(frame)
            self._anchor_ns.append(t_ns)

    def maybe_sync(self, frame: int, t_ns: int) -> None:
        """Record (frame, wall-clock time) if a sync point is due."""
        with self._lock:
            if not self._sync or frame - self._sync[-1][0] >= self._sync_every:
                self._sync.append((frame, t_ns))

    def sync_points(self, start_frame: int, end_frame: int) -> list[tuple[int, int]]:
        with self._lock:
            return [(f, t) for f, t in self._sync if start_frame <= f < end_frame]

    def frame_to_ns(self, frame: int) -> int:
        with self._lock:
            if not self._anchor_frames:
                raise RuntimeError("timeline has no anchor yet")
            i = max(0, bisect.bisect_right(self._anchor_frames, frame) - 1)
            base_frame, base_ns = self._anchor_frames[i], self._anchor_ns[i]
        return base_ns + (frame - base_frame) * NS_PER_S // self.sample_rate

    def ns_to_frame(self, t_ns: int) -> int:
        """The nearest frame to a UTC time (never negative)."""
        with self._lock:
            if not self._anchor_frames:
                return 0
            i = max(0, bisect.bisect_right(self._anchor_ns, t_ns) - 1)
            base_frame, base_ns = self._anchor_frames[i], self._anchor_ns[i]
        # Integer maths: floats lose precision over long sessions.
        frame = base_frame + ((t_ns - base_ns) * self.sample_rate + NS_PER_S // 2) // NS_PER_S
        if i + 1 < len(self._anchor_frames):
            frame = min(frame, self._anchor_frames[i + 1])
        return max(0, frame)
