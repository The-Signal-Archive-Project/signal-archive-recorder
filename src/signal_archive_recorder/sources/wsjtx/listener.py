# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The WSJT-X / JTDX source adapter: UDP in, normalised bus events out.

Receive only: the socket is never written to. Unicast and multicast are both
supported; multicast lets several programs (GridTracker, JTAlert, this recorder)
share WSJT-X's stream. Bad datagrams are counted and dropped, never fatal.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.core.events import (
    Decode,
    FreqChanged,
    ModeChanged,
    SourceDown,
    SourceUp,
    TxEnded,
    TxStarted,
)
from signal_archive_recorder.modes.registry import ModeRegistry, Resolution
from signal_archive_recorder.sources.wsjtx import messages as m

log = logging.getLogger(__name__)

NS_PER_MS = 1_000_000
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
DEFAULT_PORT = 2237


@dataclass
class ListenerStats:
    datagrams: int = 0
    parse_errors: int = 0
    unknown_types: dict[int, int] = field(default_factory=dict)
    decodes: int = 0
    off_air_decodes: int = 0


@dataclass
class _ClientState:
    up: bool = False
    last_seen_mono_ns: int = 0
    dial_hz: int | None = None
    mode: str | None = None
    tr_period_s: int | None = None
    mode_id: str | None = None
    transmitting: bool = False
    version: str | None = None


SocketFactory = Callable[[], socket.socket]


class WsjtxListener:
    def __init__(
        self,
        bus: EventBus,
        registry: ModeRegistry,
        clock: Clock,
        *,
        source: str = "wsjtx",
        port: int = DEFAULT_PORT,
        bind: str = "127.0.0.1",
        group: str | None = None,
        timeout_s: float = 30.0,
        decode_log: Path | None = None,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self.source = source
        self.stats = ListenerStats()
        self._bus = bus
        self._registry = registry
        self._clock = clock
        self._port = port
        self._bind = bind
        self._group = group
        self._timeout_ns = int(timeout_s * 1e9)
        self._decode_log = decode_log
        self._socket_factory = socket_factory or (
            lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        )
        self._clients: dict[str, _ClientState] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    # -- socket ------------------------------------------------------------------

    def start(self) -> None:
        self._sock = self._open_socket()
        self._thread = threading.Thread(target=self._run, name=f"{self.source}-udp", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        if self._sock:
            self._sock.close()

    @property
    def address(self) -> tuple[str, int]:
        assert self._sock is not None
        host, port = self._sock.getsockname()[:2]
        return host, port

    def _open_socket(self) -> socket.socket:
        sock = self._socket_factory()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self._group:
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self._port))
            mreq = struct.pack(
                "4s4s", socket.inet_aton(self._group), socket.inet_aton(self._bind or "0.0.0.0")
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        else:
            sock.bind((self._bind, self._port))
        sock.settimeout(0.25)
        return sock

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data = self._sock.recv(65_535)
            except TimeoutError:
                data = b""
            except OSError:
                if self._stop.is_set():
                    return
                log.exception("%s: socket error", self.source)
                continue
            if data:
                self.handle(data)
            self.check_timeouts()

    # -- protocol ----------------------------------------------------------------

    def handle(self, datagram: bytes) -> None:
        """Process one datagram. Safe to call directly in tests; never raises."""
        with self._lock:
            self.stats.datagrams += 1
            try:
                message = m.parse(datagram)
            except m.ParseError as exc:
                self.stats.parse_errors += 1
                log.debug("%s: dropped datagram: %s", self.source, exc)
                return
            try:
                self._dispatch(message)
            except Exception:
                log.exception("%s: failed to handle %r", self.source, message)

    def check_timeouts(self) -> None:
        now = self._clock.monotonic_ns()
        with self._lock:
            for client_id, state in self._clients.items():
                if state.up and now - state.last_seen_mono_ns > self._timeout_ns:
                    self._down(client_id, state, "timeout")

    def _client(self, client_id: str) -> _ClientState:
        state = self._clients.setdefault(client_id, _ClientState())
        state.last_seen_mono_ns = self._clock.monotonic_ns()
        if not state.up:
            state.up = True
            detail = f"{client_id} {state.version}" if state.version else client_id
            self._publish(SourceUp, client_id, detail=detail)
        return state

    def _down(self, client_id: str, state: _ClientState, reason: str) -> None:
        state.up = False
        if state.transmitting:
            state.transmitting = False
            self._publish(TxEnded, client_id)
        self._publish(SourceDown, client_id, reason=reason)

    def _dispatch(self, message: m.Message) -> None:
        if isinstance(message, m.Unknown):
            counts = self.stats.unknown_types
            counts[message.type] = counts.get(message.type, 0) + 1
            self._client(message.client_id)
            return
        if isinstance(message, m.Heartbeat):
            # Record the version first so SourceUp can report it.
            self._clients.setdefault(message.client_id, _ClientState()).version = message.version
            self._client(message.client_id)
        elif isinstance(message, m.Status):
            self._on_status(message)
        elif isinstance(message, m.Decode):
            self._on_decode(message)
        elif isinstance(message, m.Close):
            state = self._clients.get(message.client_id)
            if state and state.up:
                self._down(message.client_id, state, "closed")

    def _on_status(self, s: m.Status) -> None:
        state = self._client(s.client_id)
        if s.dial_hz != state.dial_hz:
            state.dial_hz = s.dial_hz
            self._publish(FreqChanged, s.client_id, dial_hz=s.dial_hz)
        if (s.mode, s.tr_period_s) != (state.mode, state.tr_period_s):
            state.mode, state.tr_period_s = s.mode, s.tr_period_s
            res = self._registry.resolve(self.source, s.mode or "")
            state.mode_id = res.mode.id
            self._publish(
                ModeChanged,
                s.client_id,
                mode_id=res.mode.id,
                raw_mode=s.mode or "",
                needs_mapping=res.needs_mapping,
                period_s=float(s.tr_period_s) if s.tr_period_s is not None else None,
                params={
                    k: v
                    for k, v in (
                        ("sub_mode", s.sub_mode),
                        ("freq_tolerance_hz", s.freq_tolerance_hz),
                    )
                    if v is not None
                },
            )
        if s.transmitting != state.transmitting:
            state.transmitting = s.transmitting
            self._publish(
                TxStarted if s.transmitting else TxEnded,
                s.client_id,
                extra_raw={"tx_message": s.tx_message},
            )

    def _on_decode(self, d: m.Decode) -> None:
        state = self._client(d.client_id)
        res = self._registry.resolve(self.source, d.mode_symbol or "")
        mode_id = res.mode.id
        if res.kind is Resolution.UNKNOWN and state.mode_id is not None:
            mode_id = state.mode_id  # an unmapped symbol: trust the current status mode
        decoder_time_ns = None if d.off_air else self._decode_time_ns(d.time_ms)
        event = Decode(
            source=self.source,
            mode_id=mode_id,
            text=d.message or "",
            decoder_time_ns=decoder_time_ns,
            snr_db=float(d.snr_db),
            dt_s=d.dt_s,
            df_hz=float(d.df_hz),
            low_confidence=d.low_confidence,
            off_air=d.off_air,
            raw={
                "client_id": d.client_id,
                "new": d.new,
                "time_ms": d.time_ms,
                "mode_symbol": d.mode_symbol,
                "symbol_needs_mapping": res.needs_mapping,
            },
        )
        self.stats.decodes += 1
        self.stats.off_air_decodes += d.off_air
        stamped = self._bus.publish(event)
        self._log_decode(stamped.t_ns, event)

    def _decode_time_ns(self, time_ms: int | None) -> int | None:
        """Attach the UTC date: the decode is from today, or just before midnight."""
        if time_ms is None:
            return None
        now_ms = self._clock.now_ns() // NS_PER_MS
        day_start = now_ms - now_ms % DAY_MS
        t = day_start + time_ms
        if t - now_ms > HOUR_MS:
            t -= DAY_MS
        return t * NS_PER_MS

    def _publish(
        self, kind: Any, client_id: str, *, extra_raw: dict[str, Any] | None = None, **fields: Any
    ) -> None:
        raw = {"client_id": client_id, **(extra_raw or {})}
        self._bus.publish(kind(source=self.source, raw=raw, **fields))

    def _log_decode(self, t_ns: int, event: Decode) -> None:
        if self._decode_log is None:
            return
        line = {
            "received_ns": t_ns,
            "source": event.source,
            "mode_id": event.mode_id,
            "text": event.text,
            "decoder_time_ns": event.decoder_time_ns,
            "snr_db": event.snr_db,
            "dt_s": event.dt_s,
            "df_hz": event.df_hz,
            "low_confidence": event.low_confidence,
            "off_air": event.off_air,
            "raw": dict(event.raw),
        }
        self._decode_log.parent.mkdir(parents=True, exist_ok=True)
        with self._decode_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
