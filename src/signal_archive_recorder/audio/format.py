# SPDX-License-Identifier: Apache-2.0
"""Sample formats as they arrive from the device: interleaved, native little-endian."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SampleFormat = Literal["int16", "int24", "int32", "float32"]

_BYTES: dict[SampleFormat, int] = {"int16": 2, "int24": 3, "int32": 4, "float32": 4}


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    sample_format: SampleFormat

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.channels <= 0:
            raise ValueError("sample rate and channel count must be positive")
        if self.sample_format not in _BYTES:
            raise ValueError(f"unsupported sample format {self.sample_format!r}")

    @property
    def bytes_per_sample(self) -> int:
        return _BYTES[self.sample_format]

    @property
    def frame_bytes(self) -> int:
        return self.bytes_per_sample * self.channels

    @property
    def bits(self) -> int:
        return self.bytes_per_sample * 8

    @property
    def is_float(self) -> bool:
        return self.sample_format == "float32"

    def frames_to_ns(self, frames: int) -> int:
        return frames * 1_000_000_000 // self.sample_rate
