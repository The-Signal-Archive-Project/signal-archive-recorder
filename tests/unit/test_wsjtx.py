# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 4: the WSJT-X UDP listener, checked against traffic captured from WSJT-X."""

import json
import socket
import struct
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st

from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import (
    Decode,
    Event,
    FreqChanged,
    ModeChanged,
    SourceDown,
    SourceUp,
    TxEnded,
    TxStarted,
)
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener

SESSION1 = Path(__file__).resolve().parents[1] / "fixtures" / "udp" / "wsjtx-session1"
REGISTRY = ModeRegistry.load_default()
S = 10**9


def capture(folder: Path = SESSION1) -> list[bytes]:
    index = [json.loads(line) for line in (folder / "index.jsonl").open()]
    return [(folder / e["file"]).read_bytes() for e in index]


def utc_ns(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()) * S


class Harness:
    def __init__(self, clock: FakeClock | None = None, **kwargs: object) -> None:
        self.clock = clock or FakeClock(utc_ns("2026-10-05T22:35:14"))
        self.bus = EventBus(self.clock)
        self.events: list[Event] = []
        self.bus.subscribe(lambda s: self.events.append(s.event))
        self.listener = WsjtxListener(self.bus, REGISTRY, self.clock, **kwargs)  # type: ignore[arg-type]

    def feed(self, datagrams: list[bytes]) -> list[Event]:
        for d in datagrams:
            self.listener.handle(d)
        self.bus.close()
        return self.events


# -- parsing captured traffic --------------------------------------------------


def test_parse_fixture_session1() -> None:
    parsed = [m.parse(d) for d in capture()]
    assert Counter(type(p).__name__ for p in parsed) == {
        "Heartbeat": 29,
        "Status": 83,
        "Decode": 40,
        "Close": 1,
    }

    first = parsed[0]
    assert isinstance(first, m.Status)
    assert (first.client_id, first.dial_hz, first.mode) == ("WSJT-X", 14_074_000, "FT8")
    assert (first.transmitting, first.tr_period_s, first.config_name) == (False, None, "Default")

    q65 = parsed[63]
    assert isinstance(q65, m.Status)
    assert (q65.mode, q65.tr_period_s, q65.freq_tolerance_hz) == ("Q65", 30, 50)

    tune = parsed[144]
    assert isinstance(tune, m.Status) and tune.transmitting and tune.tx_message == "TUNE"

    ft8 = parsed[92]
    assert isinstance(ft8, m.Decode)
    assert ft8.time_ms == (13 * 3600 + 34 * 60 + 30) * 1000  # sample file 210703_133430.wav
    assert (ft8.snr_db, ft8.df_hz, ft8.mode_symbol) == (16, 2571, "~")
    assert ft8.message == "W1FC F5BZB -08"
    assert ft8.dt_s == pytest.approx(0.3, abs=0.05)
    assert ft8.off_air and not ft8.low_confidence

    ft4 = parsed[120]
    assert isinstance(ft4, m.Decode) and (ft4.time_ms, ft4.mode_symbol) == (2000, "+")

    beat = parsed[2]
    assert isinstance(beat, m.Heartbeat)
    assert (beat.max_schema, beat.version, beat.revision) == (3, "3.0.2-devel", "")

    assert parsed[-1] == m.Close("WSJT-X")


def test_encoder_reproduces_capture_exactly() -> None:
    """encode() and the fake emitter produce byte-identical WSJT-X datagrams."""
    for i, datagram in enumerate(capture()):
        message = m.parse(datagram)
        assert not isinstance(message, m.Unknown)
        assert m.encode(message) == datagram, f"datagram {i}"


def test_null_and_empty_strings() -> None:
    status = m.parse(capture()[0])
    assert isinstance(status, m.Status)
    assert status.dx_call is None  # null (length 0xFFFFFFFF)
    assert status.de_call == ""  # empty (length 0)


def test_unknown_type_counted() -> None:
    h = Harness()
    datagram = m.Writer().u32(m.MAGIC).u32(2).u32(99).utf8("WSJT-X").u32(7).to_bytes()
    events = h.feed([datagram])
    assert h.listener.stats.unknown_types == {99: 1}
    assert [type(e) for e in events] == [SourceUp]


def test_truncated_and_garbage() -> None:
    h = Harness()
    status = capture()[0]
    for n in range(len(status)):
        h.listener.handle(status[:n])  # every truncation point
    h.listener.handle(b"\x00\x01\x02\x03" + status[4:])  # bad magic
    h.bus.close()
    assert h.listener.stats.parse_errors > 0
    assert h.listener.stats.datagrams == len(status) + 1


@given(st.binary(max_size=300))
def test_random_bytes_never_raise(data: bytes) -> None:
    listener = WsjtxListener(EventBus(FakeClock()), REGISTRY, FakeClock())
    listener.handle(data)
    listener.handle(struct.pack(">II", m.MAGIC, 2) + data)


# -- events --------------------------------------------------------------------


def _summary(events: list[Event]) -> list[tuple[object, ...]]:
    out: list[tuple[object, ...]] = []
    for e in events:
        if isinstance(e, FreqChanged):
            out.append(("freq", e.dial_hz))
        elif isinstance(e, ModeChanged):
            out.append(("mode", e.mode_id, e.period_s))
        elif isinstance(e, (TxStarted, TxEnded, SourceUp)):
            out.append((type(e).__name__,))
        elif isinstance(e, SourceDown):
            out.append(("SourceDown", e.reason))
    return out


def test_status_to_events() -> None:
    h = Harness()
    summary = _summary(h.feed(capture()))
    assert summary == [
        ("SourceUp",),
        ("freq", 14_074_000),
        ("mode", "ft8", None),
        ("freq", 7_074_000),  # step 2: band change
        ("mode", "ft4", None),  # step 3: modes
        ("freq", 7_047_500),
        ("mode", "q65", 30.0),  # Q65 reports its 30 s T/R period
        ("mode", "ft8", None),
        ("freq", 7_074_000),
        ("mode", "ft4", None),  # step 5
        ("freq", 7_047_500),
        ("TxStarted",),  # step 6: Tune
        ("TxEnded",),
        ("SourceDown", "closed"),  # step 7
    ]
    tx = next(e for e in h.events if isinstance(e, TxStarted))
    assert tx.raw["tx_message"] == "TUNE"
    assert all(e.source == "wsjtx" for e in h.events)


def test_decode_event(tmp_path: Path) -> None:
    log_path = tmp_path / "wsjtx_decodes.jsonl"
    h = Harness(decode_log=log_path)
    decodes = [e for e in h.feed(capture()) if isinstance(e, Decode)]
    assert len(decodes) == 40
    assert Counter(d.mode_id for d in decodes) == {"ft8": 21, "ft4": 19}
    first = decodes[0]
    assert (first.text, first.snr_db, first.df_hz) == ("W1FC F5BZB -08", 16.0, 2571.0)
    assert first.raw["mode_symbol"] == "~"
    # Both sample files were played back, so none of these describe live audio.
    assert all(d.off_air and d.decoder_time_ns is None for d in decodes)
    assert h.listener.stats.off_air_decodes == 40

    lines = [json.loads(line) for line in log_path.open()]
    assert len(lines) == 40
    assert lines[0]["text"] == "W1FC F5BZB -08"
    assert lines[0]["off_air"] is True
    assert lines[0]["raw"]["time_ms"] == 48_870_000


def _live_decode(time_ms: int, symbol: str = "~") -> bytes:
    return m.encode(
        m.Decode("WSJT-X", True, time_ms, -10, 0.2, 1200, symbol, "CQ TEST", False, False)
    )


def test_live_decode_gets_utc_date() -> None:
    h = Harness(FakeClock(utc_ns("2026-10-05T13:35:00")))
    [d] = [e for e in h.feed([_live_decode(48_870_000)]) if isinstance(e, Decode)]
    assert d.decoder_time_ns == utc_ns("2026-10-05T13:34:30")
    assert not d.off_air


def test_live_decode_just_before_midnight() -> None:
    h = Harness(FakeClock(utc_ns("2026-10-06T00:00:05")))
    [d] = [e for e in h.feed([_live_decode(86_385_000)]) if isinstance(e, Decode)]
    assert d.decoder_time_ns == utc_ns("2026-10-05T23:59:45")


def test_unmapped_symbol_uses_status_mode() -> None:
    status = m.parse(capture()[63])  # Q65
    h = Harness()
    events = h.feed([m.encode(status), _live_decode(1000, symbol=":")])  # type: ignore[arg-type]
    [d] = [e for e in events if isinstance(e, Decode)]
    assert d.mode_id == "q65"
    assert d.raw["symbol_needs_mapping"] is True


def test_unknown_mode_flagged() -> None:
    status = m.parse(capture()[0])
    assert isinstance(status, m.Status)
    renamed = m.Status(**{**status.__dict__, "mode": "FT9"})
    [mode] = [e for e in Harness().feed([m.encode(renamed)]) if isinstance(e, ModeChanged)]
    assert (mode.mode_id, mode.raw_mode, mode.needs_mapping) == ("unknown", "FT9", True)


def test_heartbeat_timeout() -> None:
    clock = FakeClock(utc_ns("2026-10-05T22:35:14"))
    h = Harness(clock)
    tune = capture()[144]  # transmitting
    h.listener.handle(capture()[2])  # heartbeat
    h.listener.handle(tune)
    clock.advance(29 * S)
    h.listener.check_timeouts()
    clock.advance(2 * S)
    h.listener.check_timeouts()
    h.listener.handle(capture()[2])  # it comes back
    assert _summary(h.feed([])) == [
        ("SourceUp",),
        ("freq", 7_047_500),
        ("mode", "ft4", None),
        ("TxStarted",),
        ("TxEnded",),  # a source that vanishes mid-transmit can't leave TX open
        ("SourceDown", "timeout"),  # after 31 s, not 29 s
        ("SourceUp",),
    ]
    up = [e for e in h.events if isinstance(e, SourceUp)]
    assert up[0].detail == "WSJT-X 3.0.2-devel"


# -- sockets -------------------------------------------------------------------


class NoSendSocket(socket.socket):
    """Records any attempt to send; the listener must never make one."""

    sent: ClassVar[list[str]] = []

    def send(self, *args: object, **kwargs: object) -> int:  # type: ignore[override]
        NoSendSocket.sent.append("send")
        return 0

    def sendto(self, *args: object, **kwargs: object) -> int:  # type: ignore[override]
        NoSendSocket.sent.append("sendto")
        return 0

    def sendall(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        NoSendSocket.sent.append("sendall")


def _wait_for(predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_listener_never_sends() -> None:
    NoSendSocket.sent.clear()
    h = Harness(
        port=0,
        socket_factory=lambda: NoSendSocket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP),
    )
    h.listener.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for d in capture():
                sender.sendto(d, h.listener.address)
        _wait_for(lambda: h.listener.stats.datagrams == 153)
    finally:
        h.listener.stop()
        h.bus.close()
    assert NoSendSocket.sent == []
    assert any(isinstance(e, SourceDown) for e in h.events)


def test_multicast_shared() -> None:
    """Two listeners in one multicast group both get every datagram."""
    group = "239.255.73.99"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    harnesses = [Harness(port=port, group=group, bind="127.0.0.1") for _ in range(2)]
    try:
        for h in harnesses:
            h.listener.start()
    except OSError as exc:
        pytest.skip(f"multicast not available here: {exc}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sender.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1")
            )
            for d in capture()[:20]:
                sender.sendto(d, (group, port))
        for h in harnesses:
            _wait_for(lambda h=h: h.listener.stats.datagrams == 20)
    finally:
        for h in harnesses:
            h.listener.stop()
            h.bus.close()


def test_listener_thread_stops_promptly() -> None:
    h = Harness(port=0)
    h.listener.start()
    started = time.monotonic()
    h.listener.stop()
    h.bus.close()
    assert time.monotonic() - started < 2
    assert not any(t.name == "wsjtx-udp" for t in threading.enumerate())
