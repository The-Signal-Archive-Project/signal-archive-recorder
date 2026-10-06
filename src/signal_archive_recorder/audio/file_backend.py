# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Play a WAV file as if it were a sound card, for development without a radio.

The file's samples are delivered in blocks through the same callback as a real
device, paced in real time (or `speed` times faster).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import soundfile as sf

from signal_archive_recorder.audio.device import (
    DeviceInfo,
    DeviceOpenError,
    InputCallback,
    InputStream,
)
from signal_archive_recorder.audio.flac_writer import array_to_raw
from signal_archive_recorder.audio.format import AudioFormat, SampleFormat
from signal_archive_recorder.core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

_FORMATS: dict[str, SampleFormat] = {"PCM_16": "int16", "PCM_24": "int24"}


class FileBackend:
    def __init__(
        self, path: Path, *, speed: float = 1.0, loop: bool = False, clock: Clock | None = None
    ) -> None:
        info = sf.info(str(path))
        if info.subtype not in _FORMATS:
            raise DeviceOpenError(f"{path.name}: only 16- and 24-bit PCM WAV files are supported")
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.path = path
        self.format = AudioFormat(int(info.samplerate), int(info.channels), _FORMATS[info.subtype])
        self._speed = speed
        self._loop = loop
        self._clock = clock or SystemClock()

    def input_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(
                index=0,
                name=f"file:{self.path.name}",
                host_api="file",
                max_input_channels=self.format.channels,
                default_sample_rate=self.format.sample_rate,
            )
        ]

    def open_shared_input(
        self, device: DeviceInfo, fmt: AudioFormat, callback: InputCallback, blocksize: int
    ) -> InputStream:
        if fmt != self.format:
            raise DeviceOpenError(f"{self.path.name} is {self.format}, not {fmt}")
        dtype = "int16" if fmt.sample_format == "int16" else "int32"
        data, _ = sf.read(str(self.path), dtype=dtype, always_2d=True)
        return _FileStream(
            fmt, array_to_raw(data, fmt), callback, blocksize or 480, self._speed, self._loop,
            self._clock,
        )  # fmt: skip


class _FileStream:
    def __init__(
        self,
        fmt: AudioFormat,
        data: bytes,
        callback: InputCallback,
        block: int,
        speed: float,
        loop: bool,
        clock: Clock,
    ) -> None:
        self._format = fmt
        self._data = memoryview(data)
        self._callback = callback
        self._block = block
        self._speed = speed
        self._loop = loop
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="file-audio", daemon=True)
        self.finished = threading.Event()

    @property
    def format(self) -> AudioFormat:
        return self._format

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)

    def close(self) -> None:
        self.stop()

    def _run(self) -> None:
        fb = self._format.frame_bytes
        step = self._block * fb
        block_ns = self._format.frames_to_ns(self._block) / self._speed
        start = self._clock.monotonic_ns()
        sent = 0
        pos = 0
        while not self._stop.is_set():
            if pos >= len(self._data):
                if not self._loop:
                    self.finished.set()
                    return
                pos = 0
            chunk = self._data[pos : pos + step]
            due = start + sent * block_ns
            wait_s = (due - self._clock.monotonic_ns()) / 1e9
            if wait_s > 0 and self._stop.wait(wait_s):
                return
            self._callback(chunk, len(chunk) // fb, False)
            pos += len(chunk)
            sent += 1
