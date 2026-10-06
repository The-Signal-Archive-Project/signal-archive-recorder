# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Synthetic radio-like audio: FT8-shaped transmissions in receiver noise.

Not real FT8 (no encoding, random symbols): just the same shape on the spectrum,
8 continuous-phase tones 6.25 Hz apart, 79 symbols of 0.16 s, which is what the
radio-audio screening looks for.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FT8_SYMBOL_S = 0.16
FT8_TONE_SPACING_HZ = 6.25
FT8_SYMBOLS = 79


def ft8_like(sr: int, df_hz: float, *, amplitude: float, seed: int = 0) -> npt.NDArray[np.float64]:
    rng = np.random.default_rng(seed)
    per_symbol = int(FT8_SYMBOL_S * sr)
    freqs = np.repeat(df_hz + FT8_TONE_SPACING_HZ * rng.integers(0, 8, FT8_SYMBOLS), per_symbol)
    phase = 2 * np.pi * np.cumsum(freqs) / sr
    return amplitude * np.sin(phase)


def receiver_noise(
    sr: int, seconds: float, *, level: float = 0.05, seed: int = 1
) -> npt.NDArray[np.float64]:
    return level * np.random.default_rng(seed).normal(size=int(seconds * sr))


def add_at(
    audio: npt.NDArray[np.float64], signal: npt.NDArray[np.float64], sr: int, at_s: float
) -> None:
    start = int(at_s * sr)
    audio[start : start + len(signal)] += signal[: len(audio) - start]


def to_int16(audio: npt.NDArray[np.float64]) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
