# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Recover `.flac.partial` files left behind when the recorder stopped mid-chunk.

A crashed writer leaves complete FLAC frames followed by at most one cut-off frame,
under a STREAMINFO that still says "0 samples, MD5 unknown", which libsndfile won't
read. Recovery walks the frames, keeps every complete one, writes a patched copy,
decodes it, and re-encodes the audio as a normal verified chunk. The recovered
audio is a bit-exact prefix of what was captured.

The frame layout and checksums follow the FLAC format specification, RFC 9639.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from signal_archive_recorder.audio.flac_writer import (
    PARTIAL_SUFFIX,
    TAG_NAMES,
    FlacResult,
    FlacWriter,
    array_to_raw,
)
from signal_archive_recorder.audio.format import AudioFormat, SampleFormat

log = logging.getLogger(__name__)

UNRECOVERABLE_SUFFIX = ".unrecoverable"
CRASHED_SUFFIX = ".crashed"
_FORMATS_BY_BITS: dict[int, SampleFormat] = {16: "int16", 24: "int24"}
_STREAMINFO_END = 42  # "fLaC" + 4-byte block header + 34-byte STREAMINFO
_BLOCK_SIZES = {1: 192, **{c: 576 << (c - 2) for c in range(2, 6)}}
_BLOCK_SIZES.update({c: 256 << (c - 8) for c in range(8, 16)})


def _crc8_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return table


def _crc16_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC8 = _crc8_table()
_CRC16 = _crc16_table()


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8[crc ^ b]
    return crc


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16[(crc >> 8) ^ b]
    return crc


@dataclass(frozen=True)
class StreamInfo:
    sample_rate: int
    channels: int
    bits: int
    audio_offset: int  # where the first frame starts


@dataclass(frozen=True)
class FrameHeader:
    number: int
    block_size: int
    channels: int


def parse_stream_info(buf: bytes) -> StreamInfo:
    if buf[:4] != b"fLaC" or buf[4] & 0x7F != 0:
        raise ValueError("not a FLAC stream with STREAMINFO first")
    packed = int.from_bytes(buf[18:26], "big")
    sample_rate = packed >> 44
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    pos = 4
    while True:  # skip the metadata blocks
        if pos + 4 > len(buf):
            raise ValueError("metadata blocks are cut off")
        last = buf[pos] & 0x80
        pos += 4 + int.from_bytes(buf[pos + 1 : pos + 4], "big")
        if last:
            return StreamInfo(sample_rate, channels, bits, pos)


def parse_tags(buf: bytes) -> dict[str, str]:
    """The VORBIS_COMMENT block's fields that FlacWriter can write back, if present."""
    tags: dict[str, str] = {}
    pos = 4
    while pos + 4 <= len(buf):
        header, size = buf[pos], int.from_bytes(buf[pos + 1 : pos + 4], "big")
        block = buf[pos + 4 : pos + 4 + size]
        if header & 0x7F == 4 and len(block) == size:  # little-endian lengths inside
            vendor = int.from_bytes(block[:4], "little")
            i = 4 + vendor
            count = int.from_bytes(block[i : i + 4], "little")
            i += 4
            for _ in range(count):
                n = int.from_bytes(block[i : i + 4], "little")
                key, _, value = block[i + 4 : i + 4 + n].decode("utf-8", "replace").partition("=")
                i += 4 + n
                if key.lower() in TAG_NAMES:
                    tags[key.lower()] = value
        pos += 4 + size
        if header & 0x80:
            break
    # libsndfile appends its own name to "software"; don't let it pile up on re-encode.
    if "software" in tags:
        tags["software"] = re.sub(r" \(libsndfile-[^)]*\)$", "", tags["software"])
    return tags


def parse_frame_header(buf: bytes, pos: int) -> tuple[FrameHeader, int] | None:
    """The frame header at pos and its length, or None if it isn't a valid header."""
    end = len(buf)
    if pos + 6 > end or buf[pos] != 0xFF or buf[pos + 1] & 0xFE != 0xF8:
        return None
    bs_code, sr_code = buf[pos + 2] >> 4, buf[pos + 2] & 0xF
    ch_code, reserved = buf[pos + 3] >> 4, buf[pos + 3] & 1
    if bs_code == 0 or sr_code == 15 or ch_code > 10 or reserved:
        return None
    i = pos + 4
    first = buf[i]  # frame number, UTF-8-style coded
    extra = next((n for n in range(7) if not first & (0x80 >> n)), 7)
    if extra == 1 or extra > 7:
        return None
    count = max(extra - 1, 0)
    number = first & (0x7F >> extra) if extra else first
    if i + 1 + count > end:
        return None
    for b in buf[i + 1 : i + 1 + count]:
        if b & 0xC0 != 0x80:
            return None
        number = (number << 6) | (b & 0x3F)
    i += 1 + count
    if bs_code in (6, 7):
        size = 1 if bs_code == 6 else 2
        block_size = int.from_bytes(buf[i : i + size], "big") + 1
        i += size
    else:
        block_size = _BLOCK_SIZES[bs_code]
    i += {12: 1, 13: 2, 14: 2}.get(sr_code, 0)
    if i >= end or crc8(buf[pos:i]) != buf[i]:
        return None
    channels = ch_code + 1 if ch_code < 8 else 2
    return FrameHeader(number, block_size, channels), i + 1 - pos


def complete_frames(buf: bytes, info: StreamInfo) -> tuple[int, int]:
    """Walk the frames: (end offset of the last complete frame, samples per channel)."""
    pos, expected, samples = info.audio_offset, 0, 0
    while True:
        parsed = parse_frame_header(buf, pos)
        if parsed is None or parsed[0].number != expected or parsed[0].channels != info.channels:
            return pos, samples
        header, length = parsed
        nxt = _next_frame(buf, pos + length, expected + 1, info.channels)
        if nxt is None:  # last frame: keep it only if its CRC-16 checks out
            end = len(buf)
            if end - pos > length + 2 and crc16(buf[pos : end - 2]) == int.from_bytes(
                buf[end - 2 : end], "big"
            ):
                return end, samples + header.block_size
            return pos, samples
        pos, expected, samples = nxt, expected + 1, samples + header.block_size


def _next_frame(buf: bytes, start: int, number: int, channels: int) -> int | None:
    pos = start
    while (pos := buf.find(b"\xff", pos)) != -1:
        parsed = parse_frame_header(buf, pos)
        if parsed and parsed[0].number == number and parsed[0].channels == channels:
            return pos
        pos += 1
    return None


@dataclass(frozen=True)
class Recovery:
    partial: Path
    result: FlacResult | None  # None when nothing could be recovered
    reason: str


def recover_partial(partial: Path) -> Recovery:
    """Turn one .flac.partial into a verified .flac, or mark it .unrecoverable.

    The crashed file is first renamed to .flac.crashed, so the new chunk's own
    .partial never overwrites it, and an interrupted recovery can simply run again.
    """
    final = partial.with_name(
        partial.name.removesuffix(PARTIAL_SUFFIX).removesuffix(CRASHED_SUFFIX)
    )
    source = final.with_name(final.name + CRASHED_SUFFIX)
    if partial != source:
        os.replace(partial, source)
    buf = source.read_bytes()
    try:
        info = parse_stream_info(buf)
        end, samples = complete_frames(buf, info)
        sample_format = _FORMATS_BY_BITS[info.bits]
    except (ValueError, KeyError, IndexError) as exc:
        return _give_up(source, final, f"unreadable header: {exc}")
    if samples == 0:
        return _give_up(source, final, "no complete audio frames")

    fmt = AudioFormat(info.sample_rate, info.channels, sample_format)
    patched = bytearray(buf[:end])
    packed = int.from_bytes(patched[18:26], "big")
    packed = (packed & ~((1 << 36) - 1)) | samples
    patched[18:26] = packed.to_bytes(8, "big")
    scratch = final.with_name(final.name + ".scan.flac")
    scratch.write_bytes(bytes(patched))
    try:
        writer = FlacWriter(final, fmt, tags=parse_tags(buf))  # keep the original credit
        with sf.SoundFile(scratch) as f:
            while len(block := f.read(65_536, dtype=_dtype(fmt), always_2d=True)):
                writer.write(array_to_raw(block, fmt))
        result = writer.close()
    except Exception as exc:
        log.exception("recovering %s failed", partial)
        return _give_up(source, final, f"decode failed: {exc}")
    finally:
        scratch.unlink(missing_ok=True)
    if not result.verified:
        return Recovery(partial, result, result.error or "re-encoded chunk failed verification")
    source.unlink()
    recovered = FlacResult(
        result.path, result.frames, result.pcm_md5, result.sha256, True, recovered=True
    )
    lost = len(buf) - end
    return Recovery(partial, recovered, f"recovered {samples} frames; dropped {lost} bytes")


def recover_partials(directory: Path) -> list[Recovery]:
    """Run at startup over a session folder (or all of them)."""
    found = [
        *directory.rglob(f"*.flac{PARTIAL_SUFFIX}"),
        *directory.rglob(f"*.flac{CRASHED_SUFFIX}"),  # from an interrupted recovery
    ]
    return [recover_partial(p) for p in sorted(found)]


def _dtype(fmt: AudioFormat) -> str:
    return "int16" if fmt.sample_format == "int16" else "int32"


def _give_up(source: Path, final: Path, reason: str) -> Recovery:
    os.replace(source, final.with_name(final.name + UNRECOVERABLE_SUFFIX))
    log.warning("could not recover %s: %s", final.name, reason)
    return Recovery(source, None, reason)
