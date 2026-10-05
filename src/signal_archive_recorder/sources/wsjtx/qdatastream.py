# SPDX-License-Identifier: Apache-2.0
"""Reading and writing Qt's QDataStream binary serialization.

Implemented from Qt's public documentation of the format ("Serializing Qt Data
Types"): big-endian integers, IEEE-754 doubles, length-prefixed byte arrays and
UTF-16 strings where a length of 0xFFFFFFFF means null, and QTime/QDate/QDateTime
as milliseconds since midnight, Julian day number and time-spec byte.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

NULL_LENGTH = 0xFFFFFFFF
JULIAN_DAY_UNIX_EPOCH = 2_440_588  # 1970-01-01
MS_PER_DAY = 86_400_000


class TruncatedError(ValueError):
    """The buffer ended in the middle of a value."""


@dataclass(frozen=True)
class QDateTime:
    """A QDateTime as serialized: Julian day, ms since midnight, time spec, offset."""

    julian_day: int
    ms_of_day: int | None  # None for an invalid (null) time
    time_spec: int  # 0 local, 1 UTC, 2 offset from UTC, 3 time zone
    offset_s: int = 0
    zone_id: bytes | None = None

    def to_unix_ms(self) -> int | None:
        """Milliseconds since the Unix epoch, when the value is UTC or has an offset."""
        if self.ms_of_day is None or self.time_spec not in (1, 2):
            return None
        ms = (self.julian_day - JULIAN_DAY_UNIX_EPOCH) * MS_PER_DAY + self.ms_of_day
        return ms - self.offset_s * 1000


class Reader:
    def __init__(self, buf: bytes) -> None:
        self._buf = buf
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self._buf) - self.pos

    def at_end(self) -> bool:
        return self.pos >= len(self._buf)

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self._buf):
            raise TruncatedError(f"need {n} bytes at offset {self.pos}, have {self.remaining}")
        out = self._buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def _unpack(self, fmt: str) -> int | float:
        value: int | float = struct.unpack(fmt, self._take(struct.calcsize(fmt)))[0]
        return value

    def u8(self) -> int:
        return int(self._unpack(">B"))

    def u32(self) -> int:
        return int(self._unpack(">I"))

    def i32(self) -> int:
        return int(self._unpack(">i"))

    def u64(self) -> int:
        return int(self._unpack(">Q"))

    def i64(self) -> int:
        return int(self._unpack(">q"))

    def boolean(self) -> bool:
        return self._take(1) != b"\x00"

    def double(self) -> float:
        return float(self._unpack(">d"))

    def byte_array(self) -> bytes | None:
        """QByteArray: None for a null array, b"" for an empty one."""
        length = self.u32()
        if length == NULL_LENGTH:
            return None
        return self._take(length)

    def utf8(self) -> str | None:
        """A QByteArray holding UTF-8 text. Invalid bytes are replaced, never fatal."""
        raw = self.byte_array()
        return None if raw is None else raw.decode("utf-8", errors="replace")

    def qstring(self) -> str | None:
        """QString: byte length, then UTF-16BE."""
        length = self.u32()
        if length == NULL_LENGTH:
            return None
        return self._take(length).decode("utf-16-be", errors="replace")

    def qtime(self) -> int | None:
        """QTime as milliseconds since midnight; None if invalid."""
        ms = self.u32()
        return None if ms == NULL_LENGTH else ms

    def qdatetime(self) -> QDateTime:
        julian_day = self.i64()
        ms = self.qtime()
        spec = self.u8()
        offset = self.i32() if spec == 2 else 0
        zone = self.byte_array() if spec == 3 else None
        return QDateTime(julian_day, ms, spec, offset, zone)


class Writer:
    """The inverse of Reader, for building test datagrams."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def to_bytes(self) -> bytes:
        return b"".join(self._parts)

    def u8(self, v: int) -> Writer:
        self._parts.append(struct.pack(">B", v))
        return self

    def u32(self, v: int) -> Writer:
        self._parts.append(struct.pack(">I", v))
        return self

    def i32(self, v: int) -> Writer:
        self._parts.append(struct.pack(">i", v))
        return self

    def u64(self, v: int) -> Writer:
        self._parts.append(struct.pack(">Q", v))
        return self

    def i64(self, v: int) -> Writer:
        self._parts.append(struct.pack(">q", v))
        return self

    def boolean(self, v: bool) -> Writer:
        self._parts.append(b"\x01" if v else b"\x00")
        return self

    def double(self, v: float) -> Writer:
        self._parts.append(struct.pack(">d", v))
        return self

    def byte_array(self, v: bytes | None) -> Writer:
        if v is None:
            return self.u32(NULL_LENGTH)
        self.u32(len(v))
        self._parts.append(v)
        return self

    def utf8(self, v: str | None) -> Writer:
        return self.byte_array(None if v is None else v.encode("utf-8"))

    def qstring(self, v: str | None) -> Writer:
        if v is None:
            return self.u32(NULL_LENGTH)
        encoded = v.encode("utf-16-be")
        self.u32(len(encoded))
        self._parts.append(encoded)
        return self

    def qtime(self, ms: int | None) -> Writer:
        return self.u32(NULL_LENGTH if ms is None else ms)

    def qdatetime(self, v: QDateTime) -> Writer:
        self.i64(v.julian_day).qtime(v.ms_of_day).u8(v.time_spec)
        if v.time_spec == 2:
            self.i32(v.offset_s)
        if v.time_spec == 3:
            self.byte_array(v.zone_id)
        return self
