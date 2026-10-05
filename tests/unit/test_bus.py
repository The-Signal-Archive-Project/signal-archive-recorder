# SPDX-License-Identifier: Apache-2.0
import threading
import time
from itertools import pairwise

import pytest

from signal_archive_recorder.core.bus import BusClosedError, EventBus
from signal_archive_recorder.core.clock import FakeClock, SystemClock
from signal_archive_recorder.core.events import FreqChanged, Stamped, TxStarted


def test_bus_ordering() -> None:
    """Events from several threads arrive in timestamp order, with none lost, at >10k/s."""
    threads, per_thread = 8, 5_000
    bus = EventBus(SystemClock())
    received: list[Stamped] = []
    bus.subscribe(received.append)
    start = threading.Barrier(threads)

    def produce(n: int) -> None:
        start.wait()
        for i in range(per_thread):
            bus.publish(FreqChanged(source=f"t{n}", dial_hz=i))

    workers = [threading.Thread(target=produce, args=(n,)) for n in range(threads)]
    began = time.perf_counter()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    bus.close(timeout=10)
    elapsed = time.perf_counter() - began

    total = threads * per_thread
    assert len(received) == total
    assert [s.seq for s in received] == list(range(total))
    assert all(a.t_ns <= b.t_ns for a, b in pairwise(received))
    for n in range(threads):  # each producer's own order is preserved
        mine = [s.event for s in received if s.event.source == f"t{n}"]
        assert [e.dial_hz for e in mine if isinstance(e, FreqChanged)] == list(range(per_thread))
    assert total / elapsed > 10_000, f"only {total / elapsed:.0f} events/s"


def test_stamps_with_clock() -> None:
    clock = FakeClock(start_ns=1_000)
    bus = EventBus(clock)
    first = bus.publish(TxStarted(source="x"))
    clock.advance(500)
    second = bus.publish(TxStarted(source="x"))
    bus.close()
    assert (first.t_ns, first.seq) == (1_000, 0)
    assert (second.t_ns, second.seq) == (1_500, 1)


def test_failing_subscriber_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus(FakeClock())
    good: list[Stamped] = []

    def bad(_: Stamped) -> None:
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(good.append)
    for _ in range(3):
        bus.publish(TxStarted(source="x"))
    bus.close()
    assert len(good) == 3
    assert len([r for r in caplog.records if r.exc_info]) == 3


def test_unsubscribe() -> None:
    bus = EventBus(FakeClock())
    got: list[Stamped] = []
    unsubscribe = bus.subscribe(got.append)
    bus.publish(TxStarted(source="x"))
    bus.close()  # drains, so the first event is delivered before we unsubscribe
    unsubscribe()
    assert len(got) == 1


def test_publish_after_close_fails() -> None:
    bus = EventBus(FakeClock())
    bus.close()
    bus.close()  # idempotent
    with pytest.raises(BusClosedError):
        bus.publish(TxStarted(source="x"))


def test_events_are_immutable() -> None:
    event = FreqChanged(source="x", dial_hz=14_074_000, raw={"a": 1})
    with pytest.raises(AttributeError):
        event.dial_hz = 7_074_000  # type: ignore[misc]
    assert event.raw == {"a": 1}
