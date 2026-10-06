# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Which channels of a stereo input carry anything worth keeping.

Most radio interfaces show up as stereo, but usually only one channel carries the
receiver: the other is a copy of it, or silent. A copy adds nothing (no SNR, no
information) and doubles the storage, so setup recommends keeping one channel.
Channels that really differ (a second receiver on the right channel, or I/Q from
an SDR or a rig's I/Q output) are both kept.

The device is still opened in stereo and the kept channel is picked out of the
stream byte for byte, so it stays bit-exact. (Asking the OS for mono can make it
average the two channels, which isn't exact and costs 6 dB when one is silent.)
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from signal_archive_recorder.audio.capture import Sink
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.audio.levels import to_float

log = logging.getLogger(__name__)

Keep = Literal["both", "left", "right"]
KEEP_CHOICES: tuple[Keep, ...] = ("both", "left", "right")
SILENT_DBFS = -70.0  # below this, a channel carries nothing from the receiver
COPY_CORRELATION = 0.99  # with levels within COPY_LEVEL_DB: the same audio
COPY_LEVEL_DB = 1.0
QUIETER_DB = 30.0  # a channel this far below the other is just its noise floor
GUARD_EVERY_S = 10.0  # how often a dropped channel is re-checked while recording
GUARD_STRIKES = 3  # consecutive "it carries different audio" checks before warning


@dataclass(frozen=True)
class ChannelAnalysis:
    channels: int
    peak_dbfs: tuple[float, ...]
    identical: bool  # the two channels are byte-for-byte the same
    correlation: float | None  # left vs right; None if either is constant


@dataclass(frozen=True)
class Recommendation:
    keep: Keep
    reason: str  # copy, nearly_copy, silent, quieter, different, no_audio, mono, multichannel
    explanation: str


def _db(x: float) -> float:
    return 20 * math.log10(x) if x > 0 else -math.inf


def analyse(data: bytes, fmt: AudioFormat) -> ChannelAnalysis:
    x = to_float(data, fmt)
    peaks = tuple(
        _db(float(np.abs(x[:, c]).max())) if len(x) else -math.inf for c in range(fmt.channels)
    )
    if fmt.channels != 2 or not len(x):
        return ChannelAnalysis(fmt.channels, peaks, False, None)
    raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 2, fmt.bytes_per_sample)
    identical = bool(np.array_equal(raw[:, 0, :], raw[:, 1, :]))
    left, right = x[:, 0], x[:, 1]
    correlation = None
    if left.std() > 0 and right.std() > 0:
        correlation = float(np.corrcoef(left, right)[0, 1])
    return ChannelAnalysis(2, peaks, identical, correlation)


def _same_audio(a: ChannelAnalysis) -> bool:
    if a.identical:
        return True
    left, right = a.peak_dbfs
    close = math.isfinite(left) and math.isfinite(right) and abs(left - right) < COPY_LEVEL_DB
    return close and a.correlation is not None and a.correlation > COPY_CORRELATION


def recommend(a: ChannelAnalysis) -> Recommendation:
    if a.channels == 1:
        return Recommendation("both", "mono", "The input is mono.")
    if a.channels > 2:
        return Recommendation("both", "multichannel", f"The input has {a.channels} channels.")
    left, right = a.peak_dbfs
    if left < SILENT_DBFS and right < SILENT_DBFS:
        return Recommendation(
            "both",
            "no_audio",
            "There was no audio on either channel, so "
            "both are kept. Test again with the radio receiving to save space.",
        )
    if a.identical:
        return Recommendation(
            "left",
            "copy",
            "The right channel is an exact copy of the left, "
            "so only the left is recorded (nothing is lost).",
        )
    if right < SILENT_DBFS or left < SILENT_DBFS:
        side: Keep = "left" if right < SILENT_DBFS else "right"
        other = "right" if side == "left" else "left"
        return Recommendation(
            side, "silent", f"The {other} channel is silent, so only the {side} is recorded."
        )
    if _same_audio(a):
        return Recommendation(
            "left",
            "nearly_copy",
            "Both channels carry the same audio, so only the left is recorded (nothing is lost).",
        )
    if abs(left - right) > QUIETER_DB:
        side = "left" if left > right else "right"
        other = "right" if side == "left" else "left"
        return Recommendation(
            side,
            "quieter",
            f"The {other} channel only has a faint noise floor, so only the {side} is recorded.",
        )
    return Recommendation(
        "both",
        "different",
        "The channels carry different audio (a second receiver, or I/Q?), so both are recorded.",
    )


def kept_format(fmt: AudioFormat, keep: Keep) -> AudioFormat:
    if keep == "both" or fmt.channels < 2:
        return fmt
    return AudioFormat(fmt.sample_rate, 1, fmt.sample_format)


class ChannelPicker:
    """A capture sink that keeps one channel of a stereo stream and passes it on.

    Runs on the capture writer thread, never in the audio callback. It also keeps an
    eye on the dropped channel: if it starts carrying different audio (say, a second
    receiver was switched on), `on_distinct` is called once with an explanation.
    """

    def __init__(
        self,
        fmt: AudioFormat,
        keep: Keep,
        sinks: Sequence[Sink],
        on_distinct: Callable[[str], None] | None = None,
    ) -> None:
        if fmt.channels != 2 or keep == "both":
            raise ValueError("ChannelPicker keeps one channel of a stereo stream")
        self.format = fmt
        self.keep = keep
        self._index = 0 if keep == "left" else 1
        self._sinks = list(sinks)
        self._on_distinct = on_distinct
        self._guard_frames = int(GUARD_EVERY_S * fmt.sample_rate)
        self._since_check = self._guard_frames  # check the first block
        self._strikes = 0
        self.warned = False

    def write(self, data: bytes, stream_frame: int) -> None:
        bps = self.format.bytes_per_sample
        frames = np.frombuffer(data, dtype=np.uint8).reshape(-1, 2, bps)
        kept = frames[:, self._index, :].tobytes()
        for sink in self._sinks:
            sink.write(kept, stream_frame)
        self._since_check += len(frames)
        if self._since_check >= self._guard_frames and not self.warned:
            self._since_check = 0
            self._guard(data)

    def _guard(self, data: bytes) -> None:
        a = analyse(data, self.format)
        kept_db, dropped_db = a.peak_dbfs[self._index], a.peak_dbfs[1 - self._index]
        distinct = (
            dropped_db >= SILENT_DBFS
            and not _same_audio(a)
            and (not math.isfinite(kept_db) or kept_db - dropped_db <= QUIETER_DB)
        )
        self._strikes = self._strikes + 1 if distinct else 0
        if self._strikes >= GUARD_STRIKES and self._on_distinct is not None:
            self.warned = True
            dropped = "right" if self.keep == "left" else "left"
            self._on_distinct(
                f"The {dropped} channel now carries different audio (a second receiver?) "
                f"and isn't being recorded. To keep it, run setup again or set "
                f'[audio] keep_channel = "both".'
            )
