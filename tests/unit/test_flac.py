# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import hashlib
import subprocess
import sys
from itertools import product
from pathlib import Path

import pytest

from signal_archive_recorder.audio.flac_recovery import (
    UNRECOVERABLE_SUFFIX,
    crc8,
    crc16,
    parse_stream_info,
    recover_partials,
)
from signal_archive_recorder.audio.flac_writer import (
    FlacResult,
    FlacWriter,
    UnsupportedFormatError,
    decode_raw,
    header_md5,
    sha256_file,
    verify_flac,
)
from signal_archive_recorder.audio.format import AudioFormat, SampleFormat
from tests.fakes.fake_audio import noise, sine

FORMATS = list(product(["int16", "int24"], [1, 2], [44_100, 48_000]))


def write_chunk(path: Path, fmt: AudioFormat, data: bytes, block_frames: int = 4800) -> FlacResult:
    writer = FlacWriter(path, fmt)
    step = block_frames * fmt.frame_bytes
    for i in range(0, len(data), step):
        writer.write(data[i : i + step])
    return writer.close()


@pytest.mark.parametrize(
    ("sample_format", "channels", "rate"), FORMATS, ids=[f"{f}-{c}ch-{r}" for f, c, r in FORMATS]
)
def test_flac_roundtrip_exact(
    tmp_path: Path, sample_format: SampleFormat, channels: int, rate: int
) -> None:
    fmt = AudioFormat(rate, channels, sample_format)
    data = noise(fmt, 1.5, seed=channels + rate)
    path = tmp_path / "chunk.flac"
    writer = FlacWriter(path, fmt)
    writer.write(data)
    done = writer.close()

    assert done.verified and done.error is None
    assert done.path == path and path.exists()
    assert not path.with_name("chunk.flac.partial").exists()
    assert done.frames == len(data) // fmt.frame_bytes
    source_md5 = hashlib.md5(data).hexdigest()
    assert header_md5(path) == done.pcm_md5 == source_md5  # STREAMINFO proves bit-exactness
    assert decode_raw(path, fmt) == (done.frames, source_md5)


def test_streaming_writes_in_small_blocks(tmp_path: Path) -> None:
    fmt = AudioFormat(48_000, 2, "int24")
    data = sine(fmt, 2.0, freq_hz=1500)
    result = write_chunk(tmp_path / "c.flac", fmt, data, block_frames=97)
    assert result.verified
    assert result.pcm_md5 == hashlib.md5(data).hexdigest()


def test_sha256_matches_file(tmp_path: Path) -> None:
    fmt = AudioFormat(48_000, 1, "int16")
    path = tmp_path / "c.flac"
    result = write_chunk(path, fmt, noise(fmt, 0.5))
    assert result.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sha256_file(path) == result.sha256


def test_flac_verify_detects_corruption(tmp_path: Path) -> None:
    fmt = AudioFormat(48_000, 2, "int24")
    data = noise(fmt, 1.0)
    path = tmp_path / "c.flac"
    result = write_chunk(path, fmt, data)
    assert verify_flac(path, fmt, result.pcm_md5).ok

    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01  # one flipped bit in the middle of the audio
    path.write_bytes(bytes(raw))
    check = verify_flac(path, fmt, result.pcm_md5)
    assert not check.ok
    assert check.error


def test_unsupported_formats_rejected(tmp_path: Path) -> None:
    for sample_format in ("float32", "int32"):
        with pytest.raises(UnsupportedFormatError, match="int24"):
            FlacWriter(tmp_path / "c.flac", AudioFormat(48_000, 2, sample_format))  # type: ignore[arg-type]


def test_rejects_partial_frames(tmp_path: Path) -> None:
    writer = FlacWriter(tmp_path / "c.flac", AudioFormat(48_000, 2, "int24"))
    with pytest.raises(ValueError, match="whole number"):
        writer.write(b"\x00" * 7)
    writer.close()


def test_crc_check_values() -> None:
    # Standard check values for CRC-8 (poly 0x07) and CRC-16/UMTS (poly 0x8005).
    assert crc8(b"123456789") == 0xF4
    assert crc16(b"123456789") == 0xFEE8


_CRASHING_WRITER = """
import sys, time
from pathlib import Path
from signal_archive_recorder.audio.flac_writer import FlacWriter
from signal_archive_recorder.audio.format import AudioFormat
from tests.fakes.fake_audio import noise

fmt = AudioFormat(48_000, 2, "int24")
data = noise(fmt, 30.0, seed=42)
writer = FlacWriter(Path(sys.argv[1]), fmt)
step = 4800 * fmt.frame_bytes
for n, i in enumerate(range(0, len(data), step)):
    writer.write(data[i : i + step])
    if n == 40:
        print("ready", flush=True)
    if n > 40:
        time.sleep(0.01)
"""


def test_partial_file_on_crash(tmp_path: Path) -> None:
    fmt = AudioFormat(48_000, 2, "int24")
    path = tmp_path / "chunk.flac"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CRASHING_WRITER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    proc.kill()  # SIGKILL / TerminateProcess: no cleanup runs
    proc.wait(10)
    partial = path.with_name("chunk.flac.partial")
    assert partial.exists() and not path.exists()

    [recovery] = recover_partials(tmp_path)
    assert recovery.result is not None, recovery.reason
    result = recovery.result
    assert result.recovered and result.verified
    assert result.path == path
    assert sorted(p.name for p in tmp_path.iterdir()) == ["chunk.flac"]  # nothing left over
    assert result.frames >= 41 * 4800 - 4096  # at most one encoder block lost at the cut

    original = noise(fmt, 30.0, seed=42)
    prefix = original[: result.frames * fmt.frame_bytes]
    assert result.pcm_md5 == hashlib.md5(prefix).hexdigest()  # bit-exact prefix
    assert verify_flac(path, fmt, result.pcm_md5).ok


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"", "header"), (b"RIFF0000WAVE" * 4, "header"), (None, "no complete audio frames")],
)
def test_unrecoverable_partial_flagged(tmp_path: Path, content: bytes | None, reason: str) -> None:
    if content is None:  # a valid header but the crash came before the first frame
        fmt = AudioFormat(48_000, 1, "int16")
        ok = write_chunk(tmp_path / "ref.flac", fmt, noise(fmt, 0.5))
        whole = ok.path.read_bytes()
        content = whole[: parse_stream_info(whole).audio_offset]  # all metadata, no audio
        ok.path.unlink()
    partial = tmp_path / "c.flac.partial"
    partial.write_bytes(content)
    [recovery] = recover_partials(tmp_path)
    assert recovery.result is None
    assert reason in recovery.reason
    assert (tmp_path / f"c.flac{UNRECOVERABLE_SUFFIX}").exists()
    assert not partial.exists()


def test_interrupted_recovery_resumes(tmp_path: Path) -> None:
    """A .flac.crashed left by a recovery that itself stopped is picked up next time."""
    fmt = AudioFormat(48_000, 2, "int16")
    data = noise(fmt, 2.0, seed=9)
    done = write_chunk(tmp_path / "chunk.flac", fmt, data)
    cut = done.path.read_bytes()
    done.path.unlink()
    (tmp_path / "chunk.flac.crashed").write_bytes(cut[: len(cut) * 6 // 10])

    [recovery] = recover_partials(tmp_path)
    assert recovery.result is not None and recovery.result.recovered
    frames = recovery.result.frames
    assert 0 < frames < len(data) // fmt.frame_bytes
    assert recovery.result.pcm_md5 == hashlib.md5(data[: frames * fmt.frame_bytes]).hexdigest()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["chunk.flac"]
