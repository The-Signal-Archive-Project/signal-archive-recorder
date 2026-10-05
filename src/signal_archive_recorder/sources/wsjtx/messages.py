# SPDX-License-Identifier: Apache-2.0
"""WSJT-X UDP datagrams, as documented in docs/protocols/wsjtx-udp.md.

That document was written from captured traffic only; every field there lists
its evidence. Fields marked unverified are parsed by position and not relied on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from signal_archive_recorder.sources.wsjtx.qdatastream import (
    NULL_LENGTH,
    Reader,
    TruncatedError,
    Writer,
)

MAGIC = 0xADBCCBDA
SCHEMA = 2


class MessageType(IntEnum):
    HEARTBEAT = 0
    STATUS = 1
    DECODE = 2
    CLOSE = 6


class ParseError(ValueError):
    """Not a WSJT-X datagram, or one that ends mid-field."""


@dataclass(frozen=True)
class Heartbeat:
    client_id: str
    max_schema: int
    version: str | None
    revision: str | None


@dataclass(frozen=True)
class Status:
    client_id: str
    dial_hz: int
    mode: str | None
    dx_call: str | None = None
    report: str | None = None
    tx_mode: str | None = None
    tx_enabled: bool = False
    transmitting: bool = False
    decoding: bool = False
    rx_df_hz: int | None = None
    tx_df_hz: int | None = None
    # Fields after this point are optional in older versions (unverified).
    de_call: str | None = None
    de_grid: str | None = None
    dx_grid: str | None = None
    flag_14: bool | None = None
    sub_mode: str | None = None
    flag_16: bool | None = None
    value_17: int | None = None
    freq_tolerance_hz: int | None = None
    tr_period_s: int | None = None
    config_name: str | None = None
    tx_message: str | None = None


@dataclass(frozen=True)
class Decode:
    client_id: str
    new: bool
    time_ms: int | None
    snr_db: int
    dt_s: float
    df_hz: int
    mode_symbol: str | None
    message: str | None
    low_confidence: bool
    off_air: bool


@dataclass(frozen=True)
class Close:
    client_id: str


@dataclass(frozen=True)
class Unknown:
    client_id: str
    type: int


Message = Heartbeat | Status | Decode | Close | Unknown


def _optional_u32(value: int) -> int | None:
    return None if value == NULL_LENGTH else value


def parse(datagram: bytes) -> Message:
    r = Reader(datagram)
    try:
        if r.u32() != MAGIC:
            raise ParseError("bad magic number")
        r.u32()  # schema: 2 in all captures; the layouts below are for it
        kind = r.u32()
        client_id = r.utf8() or ""
        if kind == MessageType.HEARTBEAT:
            return Heartbeat(client_id, r.u32(), r.utf8(), r.utf8())
        if kind == MessageType.STATUS:
            return _parse_status(r, client_id)
        if kind == MessageType.DECODE:
            return Decode(
                client_id,
                new=r.boolean(),
                time_ms=r.qtime(),
                snr_db=r.i32(),
                dt_s=r.double(),
                df_hz=r.u32(),
                mode_symbol=r.utf8(),
                message=r.utf8(),
                low_confidence=r.boolean(),
                off_air=r.boolean(),
            )
        if kind == MessageType.CLOSE:
            return Close(client_id)
        return Unknown(client_id, kind)
    except TruncatedError as exc:
        raise ParseError(f"truncated: {exc}") from exc


def _parse_status(r: Reader, client_id: str) -> Status:
    core = dict(
        dial_hz=r.u64(),
        mode=r.utf8(),
        dx_call=r.utf8(),
        report=r.utf8(),
        tx_mode=r.utf8(),
        tx_enabled=r.boolean(),
        transmitting=r.boolean(),
        decoding=r.boolean(),
        rx_df_hz=r.u32(),
        tx_df_hz=r.u32(),
    )
    readers = (
        ("de_call", r.utf8),
        ("de_grid", r.utf8),
        ("dx_grid", r.utf8),
        ("flag_14", r.boolean),
        ("sub_mode", r.utf8),
        ("flag_16", r.boolean),
        ("value_17", r.u8),
        ("freq_tolerance_hz", lambda: _optional_u32(r.u32())),
        ("tr_period_s", lambda: _optional_u32(r.u32())),
        ("config_name", r.utf8),
        ("tx_message", r.utf8),
    )
    extra: dict[str, object] = {}
    for name, read in readers:
        if r.at_end():  # an older sender that stops here (unverified)
            break
        extra[name] = read()
    return Status(client_id, **core, **extra)  # type: ignore[arg-type]


# -- encoding, for the fake emitter and tests -----------------------------------


def _header(kind: MessageType, client_id: str) -> Writer:
    return Writer().u32(MAGIC).u32(SCHEMA).u32(kind).utf8(client_id)


def encode(message: Heartbeat | Status | Decode | Close) -> bytes:
    if isinstance(message, Heartbeat):
        w = _header(MessageType.HEARTBEAT, message.client_id)
        return w.u32(message.max_schema).utf8(message.version).utf8(message.revision).to_bytes()
    if isinstance(message, Close):
        return _header(MessageType.CLOSE, message.client_id).to_bytes()
    if isinstance(message, Decode):
        w = _header(MessageType.DECODE, message.client_id)
        w.boolean(message.new).qtime(message.time_ms).i32(message.snr_db)
        w.double(message.dt_s).u32(message.df_hz).utf8(message.mode_symbol)
        w.utf8(message.message).boolean(message.low_confidence).boolean(message.off_air)
        return w.to_bytes()
    s = message
    w = _header(MessageType.STATUS, s.client_id)
    w.u64(s.dial_hz).utf8(s.mode).utf8(s.dx_call).utf8(s.report).utf8(s.tx_mode)
    w.boolean(s.tx_enabled).boolean(s.transmitting).boolean(s.decoding)
    w.u32(s.rx_df_hz or 0).u32(s.tx_df_hz or 0)
    w.utf8(s.de_call).utf8(s.de_grid).utf8(s.dx_grid).boolean(bool(s.flag_14))
    w.utf8(s.sub_mode).boolean(bool(s.flag_16)).u8(s.value_17 or 0)
    w.u32(NULL_LENGTH if s.freq_tolerance_hz is None else s.freq_tolerance_hz)
    w.u32(NULL_LENGTH if s.tr_period_s is None else s.tr_period_s)
    w.utf8(s.config_name).utf8(s.tx_message)
    return w.to_bytes()
