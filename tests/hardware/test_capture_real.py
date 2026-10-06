# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Record from a real sound card. Run with: pytest --hardware tests/hardware

Pick the device with SAR_TEST_DEVICE (part of its name) and the length with
SAR_TEST_SECONDS (default 30). For the full Stage 2 check, keep WSJT-X running and
decoding on the same device while this runs: it must not be disturbed.
"""

import os
import time

import pytest

from signal_archive_recorder.audio.capture import Capture
from signal_archive_recorder.audio.device import SoundDeviceBackend, open_input
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import SystemClock
from signal_archive_recorder.core.events import AudioGap, Event

pytestmark = pytest.mark.hardware


class Counter:
    def __init__(self, frame_bytes: int) -> None:
        self.frame_bytes = frame_bytes
        self.frames = 0
        self.next_frame = 0
        self.contiguous = True

    def write(self, data: bytes, stream_frame: int) -> None:
        self.contiguous &= stream_frame == self.next_frame
        self.next_frame = stream_frame + len(data) // self.frame_bytes
        self.frames += len(data) // self.frame_bytes


def test_capture_real() -> None:
    backend = SoundDeviceBackend()
    wanted = os.environ.get("SAR_TEST_DEVICE", "")
    devices = [d for d in backend.input_devices() if wanted.lower() in d.name.lower()]
    assert devices, f"no input device matching {wanted!r}"
    device = devices[0]
    seconds = float(os.environ.get("SAR_TEST_SECONDS", "30"))

    clock = SystemClock()
    bus = EventBus(clock)
    events: list[Event] = []
    bus.subscribe(lambda s: events.append(s.event))
    holder: dict[str, Capture] = {}  # the stream opens first, so forward to the capture
    opened = open_input(backend, device, lambda d, n, o: holder["c"].on_audio(d, n, o))
    counter = Counter(opened.delivered.frame_bytes)
    capture = Capture(opened.delivered, bus=bus, clock=clock, sinks=[counter])
    holder["c"] = capture
    print(f"\n{device.name} ({device.host_api}): delivered {opened.delivered}")
    for w in opened.warnings:
        print(f"warning: {w.message}")

    capture.start()
    opened.stream.start()
    time.sleep(seconds)
    opened.stream.stop()
    opened.stream.close()
    stats = capture.stop(timeout=10)
    bus.close()

    expected = seconds * opened.delivered.sample_rate
    assert stats.delivered_frames == pytest.approx(expected, rel=0.02)
    assert stats.lost_frames == 0
    assert stats.driver_overflows == 0
    assert counter.frames == stats.written_frames == stats.delivered_frames
    assert counter.contiguous
    assert not [e for e in events if isinstance(e, AudioGap)]
