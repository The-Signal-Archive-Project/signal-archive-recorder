# SPDX-License-Identifier: Apache-2.0
import threading
import time

import pytest

from signal_archive_recorder.audio.ringbuffer import RingBuffer


def frames(start: int, count: int, frame_bytes: int = 2) -> bytes:
    return b"".join(i.to_bytes(frame_bytes, "little") for i in range(start, start + count))


def test_write_read_roundtrip() -> None:
    ring = RingBuffer(8, 2)
    assert ring.write(frames(0, 5)) == 5
    assert ring.read(3) == frames(0, 3)
    assert ring.read(10) == frames(3, 2)


def test_wraparound() -> None:
    ring = RingBuffer(8, 2)
    ring.write(frames(0, 6))
    ring.read(6)
    assert ring.write(frames(6, 7)) == 7  # wraps past the end
    assert ring.read(7) == frames(6, 7)


def test_partial_write_when_full() -> None:
    ring = RingBuffer(4, 2)
    assert ring.write(frames(0, 3)) == 3
    assert ring.write(frames(3, 3)) == 1
    assert ring.write(frames(4, 1)) == 0
    assert ring.read(10) == frames(0, 4)


def test_read_timeout_returns_empty() -> None:
    assert RingBuffer(4, 2).read(4, timeout=0.01) == b""


def test_rejects_partial_frames() -> None:
    with pytest.raises(ValueError, match="whole number"):
        RingBuffer(4, 3).write(b"\x00" * 4)


def test_reader_wakes_on_write() -> None:
    ring = RingBuffer(4, 2)
    got: list[bytes] = []
    reader = threading.Thread(target=lambda: got.append(ring.read(4, timeout=5)))
    reader.start()
    ring.write(frames(0, 2))
    reader.join(5)
    assert got == [frames(0, 2)]


def test_concurrent_producer_consumer_preserves_stream() -> None:
    ring = RingBuffer(64, 4)
    total = 50_000
    out = bytearray()

    def consume() -> None:
        while len(out) < total * 4:
            out.extend(ring.read(50, timeout=1))

    consumer = threading.Thread(target=consume)
    consumer.start()
    sent = 0
    while sent < total:
        n = min(37, total - sent)
        taken = ring.write(frames(sent, n, 4))
        sent += taken
        if not taken:
            time.sleep(0)  # buffer full: let the consumer run
    consumer.join(10)
    assert bytes(out) == frames(0, total, 4)
