# SPDX-License-Identifier: Apache-2.0
"""A fake sound card that replays known audio through the real callback interface.

Tests pump blocks synchronously, so the test thread plays the audio thread. Block
sizes can vary (jitter), and chosen blocks can carry a driver-overflow flag.
"""

from __future__ import annotations

import itertools
import wave
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import numpy.typing as npt

from signal_archive_recorder.audio.device import (
    DeviceInfo,
    DeviceOpenError,
    InputCallback,
    InputStream,
)
from signal_archive_recorder.audio.format import AudioFormat, SampleFormat


def encode(samples: npt.NDArray[np.float64], sample_format: SampleFormat) -> bytes:
    """Interleaved float samples in [-1, 1] (frames x channels) to device bytes."""
    flat = samples.reshape(-1)
    if sample_format == "float32":
        return flat.astype("<f4").tobytes()
    bits = {"int16": 16, "int24": 24, "int32": 32}[sample_format]
    full = 2 ** (bits - 1)
    ints = np.clip(np.round(flat * full), -full, full - 1).astype(np.int64)
    if sample_format == "int24":
        u = (ints & 0xFFFFFF).astype(np.uint32)
        return (
            np.stack([u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF], axis=1)
            .astype(np.uint8)
            .tobytes()
        )
    return ints.astype("<i2" if bits == 16 else "<i4").tobytes()


def sine(
    fmt: AudioFormat, seconds: float, *, freq_hz: float = 1000.0, amplitude: float = 0.5
) -> bytes:
    n = round(seconds * fmt.sample_rate)
    t = np.arange(n) / fmt.sample_rate
    tone = amplitude * np.sin(2 * np.pi * freq_hz * t)
    return encode(np.repeat(tone[:, None], fmt.channels, axis=1), fmt.sample_format)


def noise(fmt: AudioFormat, seconds: float, *, seed: int = 0) -> bytes:
    """Random bytes in whole frames: exercises every bit pattern the format allows."""
    n = round(seconds * fmt.sample_rate) * fmt.frame_bytes
    return np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8).tobytes()


def read_wav(path: Path) -> tuple[AudioFormat, bytes]:
    """Load a PCM WAV fixture as raw device bytes."""
    with wave.open(str(path), "rb") as w:
        sample_format: SampleFormat = {2: "int16", 3: "int24", 4: "int32"}[w.getsampwidth()]
        fmt = AudioFormat(w.getframerate(), w.getnchannels(), sample_format)
        return fmt, w.readframes(w.getnframes())


class FakeAudioDevice:
    """Implements InputStream; deliver audio with pump()."""

    def __init__(
        self,
        fmt: AudioFormat,
        data: bytes,
        callback: InputCallback,
        *,
        block_frames: Iterable[int] = (480,),
        overflow_blocks: Iterable[int] = (),
    ) -> None:
        if len(data) % fmt.frame_bytes:
            raise ValueError("data is not a whole number of frames")
        self._format = fmt
        self._data = memoryview(data)
        self._callback = callback
        self._sizes = itertools.cycle(block_frames)
        self._overflow = set(overflow_blocks)
        self._pos = 0  # bytes delivered
        self._block = 0
        self.started = False
        self.closed = False

    @property
    def format(self) -> AudioFormat:
        return self._format

    @property
    def remaining_frames(self) -> int:
        return (len(self._data) - self._pos) // self._format.frame_bytes

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def pump(self, frames: int | None = None) -> int:
        """Deliver blocks until `frames` frames (or everything) have gone; return count."""
        target = self.remaining_frames if frames is None else min(frames, self.remaining_frames)
        sent = 0
        while sent < target:
            n = min(next(self._sizes), target - sent)
            end = self._pos + n * self._format.frame_bytes
            self._callback(self._data[self._pos : end], n, self._block in self._overflow)
            self._pos = end
            self._block += 1
            sent += n
        return sent


class FakeBackend:
    """An AudioBackend whose devices are FakeAudioDevices.

    delivered_rate simulates a stream running at a different rate from the one
    requested; refuse_shared simulates a device held exclusively by another program.
    """

    def __init__(
        self,
        devices: list[DeviceInfo],
        data: bytes = b"",
        *,
        delivered_rate: int | None = None,
        refuse_shared: bool = False,
    ) -> None:
        self.devices = devices
        self.data = data
        self.delivered_rate = delivered_rate
        self.refuse_shared = refuse_shared
        self.open_calls: list[tuple[DeviceInfo, AudioFormat]] = []
        self.stream: FakeAudioDevice | None = None

    def input_devices(self) -> list[DeviceInfo]:
        return self.devices

    def open_shared_input(
        self, device: DeviceInfo, fmt: AudioFormat, callback: InputCallback, blocksize: int
    ) -> InputStream:
        self.open_calls.append((device, fmt))
        if self.refuse_shared:
            raise DeviceOpenError("Device unavailable", device_unavailable=True)
        delivered = AudioFormat(
            self.delivered_rate or fmt.sample_rate, fmt.channels, fmt.sample_format
        )
        self.stream = FakeAudioDevice(delivered, self.data, callback)
        return self.stream
