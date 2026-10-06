# SPDX-License-Identifier: Apache-2.0
"""Streaming FLAC chunks with end-to-end verification.

A chunk is written to `<name>.flac.partial`. On close the file is flushed and
fsynced, decoded back, and checked three ways: the decoded audio, the MD5 the
writer computed from the device bytes, and the MD5 libFLAC stored in STREAMINFO
must all agree. Only then is it renamed to `<name>.flac`. A chunk that fails is
renamed `<name>.flac.corrupt` and reported, never silently kept.

FLAC's STREAMINFO MD5 is defined over little-endian samples at the stream's byte
width, which is exactly the device's int16/int24 bytes. So the header checksum
proves the file holds what the sound card delivered.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import soundfile as sf

from signal_archive_recorder.audio.format import AudioFormat, SampleFormat

PARTIAL_SUFFIX = ".partial"
CORRUPT_SUFFIX = ".corrupt"
# FLAC stores integers only; libsndfile writes up to 24 bits. Devices are opened at
# int24 by default, which also holds 16-bit audio losslessly (FLAC drops the unused
# low bits, so it costs almost nothing).
_SUBTYPES: dict[SampleFormat, str] = {"int16": "PCM_16", "int24": "PCM_24"}
_READ_FRAMES = 65_536
_HASH_BLOCK = 1 << 20
# The Vorbis comment fields libsndfile can write (stored as TITLE=, LICENSE=, ...).
TAG_NAMES = ("title", "copyright", "software", "artist", "comment", "date", "album", "license",
             "tracknumber", "genre")  # fmt: skip


class UnsupportedFormatError(ValueError):
    pass


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    frames: int
    decoded_md5: str
    header_md5: str
    error: str | None = None


@dataclass(frozen=True)
class FlacResult:
    path: Path
    frames: int
    pcm_md5: str
    sha256: str
    verified: bool
    recovered: bool = False
    error: str | None = None


def check_format(fmt: AudioFormat) -> str:
    try:
        return _SUBTYPES[fmt.sample_format]
    except KeyError:
        raise UnsupportedFormatError(
            f"FLAC can't store {fmt.sample_format} audio; open the device as int16 or int24"
        ) from None


def raw_to_array(data: bytes, fmt: AudioFormat) -> npt.NDArray[Any]:
    """Device bytes to the integer array libsndfile expects (24-bit is left-justified)."""
    if fmt.sample_format == "int16":
        return np.frombuffer(data, dtype="<i2").reshape(-1, fmt.channels)
    padded = np.zeros((len(data) // 3, 4), dtype=np.uint8)
    padded[:, 1:] = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
    return padded.view("<i4").reshape(-1, fmt.channels)


def array_to_raw(arr: npt.NDArray[Any], fmt: AudioFormat) -> bytes:
    if fmt.sample_format == "int16":
        return arr.astype("<i2", copy=False).tobytes()
    as_bytes = np.ascontiguousarray(arr, dtype="<i4").view(np.uint8).reshape(-1, 4)
    return as_bytes[:, 1:].tobytes()


def _read_dtype(fmt: AudioFormat) -> str:
    return "int16" if fmt.sample_format == "int16" else "int32"


def header_md5(path: Path) -> str:
    """The MD5 stored in STREAMINFO (all zeros if the encoder never finished)."""
    with path.open("rb") as f:
        head = f.read(42)
    if head[:4] != b"fLaC" or head[4] & 0x7F != 0:
        raise ValueError(f"{path.name} does not start with a FLAC STREAMINFO block")
    return head[26:42].hex()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def decode_raw(path: Path, fmt: AudioFormat) -> tuple[int, str]:
    """Decode a FLAC file to device bytes: (frames, MD5 of those bytes)."""
    md5 = hashlib.md5()
    frames = 0
    with sf.SoundFile(path) as f:
        if f.samplerate != fmt.sample_rate or f.channels != fmt.channels:
            raise ValueError(f"{path.name} is {f.samplerate} Hz/{f.channels} ch, expected {fmt}")
        while True:
            block = f.read(_READ_FRAMES, dtype=_read_dtype(fmt), always_2d=True)
            if not len(block):
                break
            md5.update(array_to_raw(block, fmt))
            frames += len(block)
    return frames, md5.hexdigest()


def verify_flac(path: Path, fmt: AudioFormat, expected_md5: str | None = None) -> VerifyResult:
    """Decode the whole file and check it against its header and, if given, our MD5."""
    try:
        stored = header_md5(path)
        frames, decoded = decode_raw(path, fmt)
    except Exception as exc:
        return VerifyResult(False, 0, "", "", f"decode failed: {exc}")
    if decoded != stored:
        return VerifyResult(False, frames, decoded, stored, "decoded audio != STREAMINFO MD5")
    if expected_md5 is not None and decoded != expected_md5:
        return VerifyResult(False, frames, decoded, stored, "decoded audio != captured audio")
    return VerifyResult(True, frames, decoded, stored)


class FlacWriter:
    """Write one chunk of device bytes to a verified FLAC file."""

    def __init__(
        self,
        path: Path,
        fmt: AudioFormat,
        *,
        compression_level: int = 8,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        subtype = check_format(fmt)
        if not 0 <= compression_level <= 8:
            raise ValueError("FLAC compression level is 0-8")
        self.path = path
        self.partial = path.with_name(path.name + PARTIAL_SUFFIX)
        self.format = fmt
        self._md5 = hashlib.md5()
        self._frames = 0
        self._file = sf.SoundFile(
            self.partial,
            "w",
            samplerate=fmt.sample_rate,
            channels=fmt.channels,
            subtype=subtype,
            format="FLAC",
            compression_level=compression_level / 8,
        )
        # Tags go in the header, so they're set before any audio. They don't change the
        # audio MD5, and are in place before the file's SHA-256 is computed.
        for name, value in (tags or {}).items():
            if name not in TAG_NAMES:
                raise ValueError(f"FLAC tag {name!r} isn't supported")
            setattr(self._file, name, value)

    @property
    def frames(self) -> int:
        return self._frames

    def write(self, data: bytes) -> None:
        if len(data) % self.format.frame_bytes:
            raise ValueError("data is not a whole number of frames")
        self._file.write(raw_to_array(data, self.format))
        self._md5.update(data)
        self._frames += len(data) // self.format.frame_bytes

    def close(self) -> FlacResult:
        self._file.close()
        _fsync(self.partial)
        captured = self._md5.hexdigest()
        check = verify_flac(self.partial, self.format, captured)
        if check.ok and check.frames == self._frames:
            os.replace(self.partial, self.path)
            _fsync_dir(self.path.parent)
            return FlacResult(self.path, self._frames, captured, sha256_file(self.path), True)
        error = check.error or f"decoded {check.frames} frames, wrote {self._frames}"
        bad = self.path.with_name(self.path.name + CORRUPT_SUFFIX)
        os.replace(self.partial, bad)
        return FlacResult(bad, self._frames, captured, sha256_file(bad), False, error=error)


def _fsync(path: Path) -> None:
    # Windows only flushes handles opened for writing.
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # Windows can't open directories; rename is journaled there
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
