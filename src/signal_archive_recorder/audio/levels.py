# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Peak, RMS and clipping per channel, accumulated over a chunk.

Levels are in dBFS relative to digital full scale: a full-scale square wave reads
0 dBFS RMS, and a full-scale sine reads about -3.01 dBFS RMS. A sample counts as
clipped when it sits at the format's most positive or most negative value (for
float, at or beyond +/-1.0).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from signal_archive_recorder.audio.format import AudioFormat

SILENCE_DBFS = -math.inf


def to_float(data: bytes | memoryview, fmt: AudioFormat) -> npt.NDArray[np.float64]:
    """Decode interleaved samples to float64 in [-1, 1), shaped (frames, channels)."""
    if fmt.sample_format == "int24":
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = (ints ^ 0x800000) - 0x800000  # sign-extend 24 to 32 bits
        samples = ints.astype(np.float64) / 2**23
    elif fmt.sample_format == "float32":
        samples = np.frombuffer(data, dtype="<f4").astype(np.float64)
    else:
        dtype = "<i2" if fmt.sample_format == "int16" else "<i4"
        samples = np.frombuffer(data, dtype=dtype).astype(np.float64) / 2 ** (fmt.bits - 1)
    return samples.reshape(-1, fmt.channels)


def _clip_limits(fmt: AudioFormat) -> tuple[float, float]:
    if fmt.is_float:
        return -1.0, 1.0
    full = 2 ** (fmt.bits - 1)
    return -1.0, (full - 1) / full


@dataclass(frozen=True)
class Levels:
    frames: int
    peak_dbfs: tuple[float, ...]
    rms_dbfs: tuple[float, ...]
    clipped: tuple[int, ...]


def _db(x: float) -> float:
    return 20 * math.log10(x) if x > 0 else SILENCE_DBFS


class LevelMeter:
    def __init__(self, fmt: AudioFormat) -> None:
        self.format = fmt
        self._low, self._high = _clip_limits(fmt)
        self.reset()

    def reset(self) -> None:
        ch = self.format.channels
        self._frames = 0
        self._peak = np.zeros(ch)
        self._sum_sq = np.zeros(ch)
        self._clipped = np.zeros(ch, dtype=np.int64)

    def update(self, data: bytes | memoryview) -> None:
        x = to_float(data, self.format)
        if not len(x):
            return
        self._frames += len(x)
        self._peak = np.maximum(self._peak, np.abs(x).max(axis=0))
        self._sum_sq += np.square(x).sum(axis=0)
        self._clipped += ((x <= self._low) | (x >= self._high)).sum(axis=0)

    def snapshot(self) -> Levels:
        n = max(self._frames, 1)
        return Levels(
            frames=self._frames,
            peak_dbfs=tuple(_db(float(p)) for p in self._peak),
            rms_dbfs=tuple(_db(math.sqrt(float(s) / n)) for s in self._sum_sq),
            clipped=tuple(int(c) for c in self._clipped),
        )
