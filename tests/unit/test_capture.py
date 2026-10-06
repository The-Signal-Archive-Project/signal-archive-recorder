# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import threading
import time
from itertools import product

import pytest

from signal_archive_recorder.audio.capture import Capture
from signal_archive_recorder.audio.format import AudioFormat, SampleFormat
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import AudioGap, CaptureWarning, Event, Stamped
from tests.fakes.fake_audio import FakeAudioDevice, noise

START_NS = 1_791_200_000 * 10**9
JITTER = (480, 512, 97, 1024, 1)


class Collector:
    def __init__(self) -> None:
        self.chunks: list[tuple[int, bytes]] = []

    def write(self, data: bytes, stream_frame: int) -> None:
        self.chunks.append((stream_frame, data))

    def joined(self) -> bytes:
        return b"".join(d for _, d in self.chunks)


class StallingSink(Collector):
    """Blocks inside its first write until released, like a disk that has hung."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes, stream_frame: int) -> None:
        self.entered.set()
        assert self.release.wait(10), "test never released the stalled sink"
        super().write(data, stream_frame)


def run_capture(
    fmt: AudioFormat, sinks: list[Collector], *, buffer_seconds: float
) -> tuple[Capture, EventBus, list[Event]]:
    bus = EventBus(FakeClock(START_NS))
    events: list[Event] = []
    bus.subscribe(lambda s: events.append(s.event))
    capture = Capture(
        fmt, bus=bus, clock=FakeClock(START_NS), sinks=list(sinks), buffer_seconds=buffer_seconds
    )
    capture.start()
    return capture, bus, events


def wait_drained(capture: Capture) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        stats = capture.stats()
        if stats.written_frames + stats.lost_frames == stats.delivered_frames:
            return
        time.sleep(0.005)
    raise AssertionError("writer never drained the buffer")


def assert_frames_contiguous(sink: Collector, fmt: AudioFormat) -> None:
    expected = 0
    for frame, data in sink.chunks:
        assert frame == expected
        expected += len(data) // fmt.frame_bytes


FORMATS = list(product(["int16", "int24", "float32"], [1, 2], [44_100, 48_000]))


@pytest.mark.parametrize(
    ("sample_format", "channels", "rate"),
    FORMATS,
    ids=[f"{f}-{c}ch-{r}" for f, c, r in FORMATS],
)
def test_bit_exact_passthrough(sample_format: SampleFormat, channels: int, rate: int) -> None:
    fmt = AudioFormat(rate, channels, sample_format)
    data = noise(fmt, 2.0, seed=rate + channels)
    sink = Collector()
    capture, bus, events = run_capture(fmt, [sink], buffer_seconds=10)
    FakeAudioDevice(fmt, data, capture.on_audio, block_frames=JITTER).pump()
    stats = capture.stop(timeout=10)
    bus.close()

    assert sink.joined() == data
    assert_frames_contiguous(sink, fmt)
    assert stats.delivered_frames == stats.written_frames == len(data) // fmt.frame_bytes
    assert stats.lost_frames == 0
    assert not [e for e in events if isinstance(e, AudioGap)]


def test_writer_stall_no_drop() -> None:
    fmt = AudioFormat(48_000, 2, "int24")
    data = noise(fmt, 3.0)
    sink = StallingSink()
    capture, bus, _ = run_capture(fmt, [sink], buffer_seconds=5)
    device = FakeAudioDevice(fmt, data, capture.on_audio, block_frames=JITTER)

    device.pump(480)
    assert sink.entered.wait(5)
    device.pump(2 * fmt.sample_rate)  # 2 s of audio while the writer is stuck
    sink.release.set()
    device.pump()
    stats = capture.stop(timeout=10)
    bus.close()

    assert stats.lost_frames == 0
    assert sink.joined() == data
    assert_frames_contiguous(sink, fmt)


def test_overrun_annotated() -> None:
    fmt = AudioFormat(48_000, 1, "int16")
    data = noise(fmt, 4.0)
    sink = StallingSink()
    capture, bus, events = run_capture(fmt, [sink], buffer_seconds=1)
    device = FakeAudioDevice(fmt, data, capture.on_audio, block_frames=JITTER)

    device.pump(480)
    assert sink.entered.wait(5)
    device.pump(3 * fmt.sample_rate)  # 3 s into a 1 s buffer while the writer is stuck
    sink.release.set()
    wait_drained(capture)
    device.pump()  # the next accepted frames close the gap
    stats = capture.stop(timeout=10)
    bus.close()

    gaps = [e for e in events if isinstance(e, AudioGap)]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.reason == "buffer_overrun"
    assert gap.lost_frames == stats.lost_frames > 0
    assert stats.written_frames + stats.lost_frames == stats.delivered_frames
    assert stats.delivered_frames == len(data) // fmt.frame_bytes
    assert gap.est_t_ns == capture.timeline.frame_to_ns(gap.stream_frame)
    assert capture.timeline.frame_to_ns(0) == START_NS - fmt.frames_to_ns(480)  # first block

    # The written audio is exactly the input with the gap cut out...
    fb = fmt.frame_bytes
    lo, hi = gap.stream_frame * fb, (gap.stream_frame + gap.lost_frames) * fb
    assert sink.joined() == data[:lo] + data[hi:]
    # ...and the frame indices jump over it.
    frames = [f for f, _ in sink.chunks]
    assert gap.stream_frame + gap.lost_frames in frames


def test_driver_overflow_flagged() -> None:
    fmt = AudioFormat(48_000, 1, "int16")
    data = noise(fmt, 0.5)
    sink = Collector()
    capture, bus, events = run_capture(fmt, [sink], buffer_seconds=5)
    FakeAudioDevice(fmt, data, capture.on_audio, block_frames=(480,), overflow_blocks={3}).pump()
    stats = capture.stop(timeout=10)
    bus.close()

    gaps = [e for e in events if isinstance(e, AudioGap)]
    assert [(g.reason, g.lost_frames, g.stream_frame) for g in gaps] == [
        ("driver_overflow", None, 3 * 480)
    ]
    assert stats.driver_overflows == 1
    assert sink.joined() == data  # what did arrive is all kept


def test_failing_sink_isolated() -> None:
    class Broken:
        def write(self, data: bytes, stream_frame: int) -> None:
            raise OSError("disk full")

    fmt = AudioFormat(48_000, 1, "int16")
    data = noise(fmt, 0.5)
    good = Collector()
    bus = EventBus(FakeClock(START_NS))
    events: list[Stamped] = []
    bus.subscribe(events.append)
    capture = Capture(fmt, bus=bus, clock=FakeClock(START_NS), sinks=[Broken(), good])
    capture.start()
    FakeAudioDevice(fmt, data, capture.on_audio).pump()
    capture.stop(timeout=10)
    bus.close()

    assert good.joined() == data
    warnings = [s.event for s in events if isinstance(s.event, CaptureWarning)]
    assert warnings and all(w.code == "sink_failed" for w in warnings)
