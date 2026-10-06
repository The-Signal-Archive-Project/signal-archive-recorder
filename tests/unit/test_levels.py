# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import math
import struct

import numpy as np
import pytest

from signal_archive_recorder.audio.format import AudioFormat, SampleFormat
from signal_archive_recorder.audio.levels import LevelMeter, to_float
from tests.fakes.fake_audio import encode, noise, sine

HALF_SCALE_DB = 20 * math.log10(0.5)  # -6.02
SINE_RMS_DB = 20 * math.log10(math.sqrt(0.5))  # -3.01


@pytest.mark.parametrize("sample_format", ["int16", "int24", "int32", "float32"])
def test_levels(sample_format: SampleFormat) -> None:
    fmt = AudioFormat(48_000, 2, sample_format)
    meter = LevelMeter(fmt)
    data = sine(fmt, 1.0, freq_hz=1000, amplitude=0.5)
    meter.update(data[: len(data) // 2])  # accumulates across blocks
    meter.update(data[len(data) // 2 :])
    levels = meter.snapshot()
    assert levels.frames == 48_000
    for ch in range(2):
        assert levels.peak_dbfs[ch] == pytest.approx(HALF_SCALE_DB, abs=0.01)
        assert levels.rms_dbfs[ch] == pytest.approx(HALF_SCALE_DB + SINE_RMS_DB, abs=0.01)
        assert levels.clipped[ch] == 0


def test_clipped_count_int16() -> None:
    fmt = AudioFormat(48_000, 2, "int16")
    # (left, right) frames: left clips 3 times (both rails), right once.
    pairs = [(32767, 0), (-32768, 5), (32766, 32767), (100, -100), (32767, -32767)]
    data = struct.pack(f"<{2 * len(pairs)}h", *[v for p in pairs for v in p])
    meter = LevelMeter(fmt)
    meter.update(data)
    assert meter.snapshot().clipped == (3, 1)


def test_clipped_count_int24() -> None:
    fmt = AudioFormat(48_000, 1, "int24")
    values = np.array([1.0, -1.0, 0.25, 0.9999999]).reshape(-1, 1)  # encode clamps 1.0 to max
    meter = LevelMeter(fmt)
    meter.update(encode(values, "int24"))
    assert meter.snapshot().clipped == (3,)


def test_clipped_count_float() -> None:
    fmt = AudioFormat(48_000, 1, "float32")
    data = struct.pack("<5f", 1.0, -1.0, 1.5, 0.999, -0.5)
    meter = LevelMeter(fmt)
    meter.update(data)
    levels = meter.snapshot()
    assert levels.clipped == (3,)
    assert levels.peak_dbfs[0] == pytest.approx(20 * math.log10(1.5))


def test_full_scale_square_is_zero_dbfs() -> None:
    fmt = AudioFormat(48_000, 1, "float32")
    meter = LevelMeter(fmt)
    meter.update(struct.pack("<4f", 1.0, -1.0, 1.0, -1.0))
    levels = meter.snapshot()
    assert levels.peak_dbfs[0] == pytest.approx(0.0)
    assert levels.rms_dbfs[0] == pytest.approx(0.0)


def test_silence_and_reset() -> None:
    fmt = AudioFormat(48_000, 1, "int16")
    meter = LevelMeter(fmt)
    meter.update(sine(fmt, 0.1))
    meter.reset()
    meter.update(b"\x00\x00" * 100)
    levels = meter.snapshot()
    assert levels.peak_dbfs == (-math.inf,)
    assert levels.rms_dbfs == (-math.inf,)
    assert levels.frames == 100


@pytest.mark.parametrize("sample_format", ["int16", "int24", "int32"])
def test_int_decode_matches_encode(sample_format: SampleFormat) -> None:
    fmt = AudioFormat(48_000, 1, sample_format)
    data = noise(fmt, 0.01, seed=7)
    assert encode(to_float(data, fmt), sample_format) == data
