# SPDX-License-Identifier: Apache-2.0
import pytest

from signal_archive_recorder.core.clock import FakeClock, SystemClock


def test_fake_clock_advance() -> None:
    clock = FakeClock(start_ns=100)
    clock.advance(50)
    assert clock.now_ns() == 150
    assert clock.monotonic_ns() == 50
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_fake_clock_auto_advance() -> None:
    clock = FakeClock(start_ns=0, auto_advance_ns=10)
    assert [clock.now_ns() for _ in range(3)] == [0, 10, 20]


def test_wall_step_leaves_monotonic_alone() -> None:
    clock = FakeClock(start_ns=1_000)
    clock.advance(5)
    clock.set_wall_ns(0)
    assert clock.now_ns() == 0
    assert clock.monotonic_ns() == 5


def test_system_clock_is_sane() -> None:
    clock = SystemClock()
    assert clock.now_ns() > 1_700_000_000 * 10**9
    a = clock.monotonic_ns()
    assert clock.monotonic_ns() >= a
