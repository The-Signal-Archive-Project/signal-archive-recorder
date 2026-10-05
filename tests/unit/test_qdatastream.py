# SPDX-License-Identifier: Apache-2.0
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from signal_archive_recorder.sources.wsjtx.qdatastream import (
    QDateTime,
    Reader,
    TruncatedError,
    Writer,
)


def test_integers_are_big_endian() -> None:
    r = Reader(bytes.fromhex("0000002a ffffffff 00000000000001f4 ff".replace(" ", "")))
    assert r.u32() == 42
    assert r.i32() == -1
    assert r.u64() == 500
    assert r.u8() == 255
    assert r.at_end()


def test_double_and_bool() -> None:
    r = Reader(bytes.fromhex("3ff80000000000000100"))
    assert r.double() == 1.5
    assert r.boolean() is True
    assert r.boolean() is False


def test_null_vs_empty_byte_array() -> None:
    r = Reader(bytes.fromhex("ffffffff0000000000000003") + b"abc")
    assert r.byte_array() is None
    assert r.byte_array() == b""
    assert r.byte_array() == b"abc"


def test_qstring_is_utf16be() -> None:
    r = Reader(bytes.fromhex("00000004") + "Hé".encode("utf-16-be") + bytes.fromhex("ffffffff"))
    assert r.qstring() == "Hé"
    assert r.qstring() is None


def test_utf8_invalid_bytes_replaced() -> None:
    r = Reader(bytes.fromhex("00000002") + b"\xff\xfe")
    assert r.utf8() == "��"


def test_qdatetime_utc_to_unix() -> None:
    # 2026-10-05 is Julian day 2461319; 12:34:56.789 UTC.
    dt = QDateTime(2_461_319, (12 * 3600 + 34 * 60 + 56) * 1000 + 789, 1)
    r = Reader(Writer().qdatetime(dt).to_bytes())
    parsed = r.qdatetime()
    assert parsed == dt
    assert parsed.to_unix_ms() == 1_791_203_696_789
    assert r.at_end()


def test_qdatetime_offset_and_local() -> None:
    plus_two = QDateTime(2_440_588, 7_200_000, 2, offset_s=7200)
    assert Reader(Writer().qdatetime(plus_two).to_bytes()).qdatetime().to_unix_ms() == 0
    assert QDateTime(2_440_588, 0, 0).to_unix_ms() is None  # local time: unknown zone
    assert QDateTime(2_440_588, None, 1).to_unix_ms() is None


@pytest.mark.parametrize("data", [b"", b"\x00\x00", bytes.fromhex("00000005") + b"abc"])
def test_truncation_raises(data: bytes) -> None:
    with pytest.raises(TruncatedError):
        Reader(data).byte_array()


@given(
    u=st.integers(0, 2**32 - 2),
    i=st.integers(-(2**63), 2**63 - 1),
    d=st.floats(allow_nan=False),
    s=st.one_of(st.none(), st.text(st.characters(exclude_categories=["Cs"]))),
    b=st.booleans(),
)
def test_writer_reader_roundtrip(u: int, i: int, d: float, s: str | None, b: bool) -> None:
    buf = Writer().u32(u).i64(i).double(d).utf8(s).qstring(s).boolean(b).to_bytes()
    r = Reader(buf)
    assert r.u32() == u
    assert r.i64() == i
    got = r.double()
    assert got == d or (math.isinf(d) and got == d)
    assert r.utf8() == s
    assert r.qstring() == s
    assert r.boolean() is b
    assert r.at_end()
