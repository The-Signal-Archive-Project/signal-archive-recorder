# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
from datetime import UTC, datetime

import pytest

from signal_archive_recorder.modes import ChunkPolicy, ModeRegistry

S = 10**9


def utc_ns(hhmmss: str, day: str = "2026-10-05") -> int:
    dt = datetime.fromisoformat(f"{day}T{hhmmss}").replace(tzinfo=UTC)
    return int(dt.timestamp()) * S


@pytest.fixture(scope="module")
def registry() -> ModeRegistry:
    return ModeRegistry.load_default()


def boundaries(policy: ChunkPolicy, start: int, count: int) -> list[int]:
    out = [policy.next_boundary(start)]
    while len(out) < count:
        out.append(policy.next_boundary(out[-1]))
    return out


def seconds_into_day(t_ns: int) -> int:
    return (t_ns % (86_400 * S)) // S


def test_chunk_policy_ft8(registry: ModeRegistry) -> None:
    policy = ChunkPolicy.for_mode(registry.get("ft8"))
    assert policy.length_ns == 300 * S
    t = utc_ns("12:03:07")
    assert policy.chunk_start(t) == utc_ns("12:00:00")
    assert policy.next_boundary(t) == utc_ns("12:05:00")
    for b in boundaries(policy, t, 20):
        assert seconds_into_day(b) % 300 == 0
        assert seconds_into_day(b) % 15 == 0  # slot edge


def test_chunk_policy_ft4_half_second_slots(registry: ModeRegistry) -> None:
    policy = ChunkPolicy.for_mode(registry.get("ft4"))
    assert policy.length_ns == 300 * S
    assert policy.slot_ns == 7_500_000_000


def test_chunk_policy_wspr(registry: ModeRegistry) -> None:
    policy = ChunkPolicy.for_mode(registry.get("wspr"))
    assert policy.length_ns == 360 * S
    t = utc_ns("12:03:07")
    assert policy.chunk_start(t) == utc_ns("12:00:00")
    assert policy.next_boundary(t) == utc_ns("12:06:00")
    for b in boundaries(policy, t, 20):
        assert seconds_into_day(b) % 120 == 0  # even minutes


def test_chunk_policy_async(registry: ModeRegistry) -> None:
    for mode_id in ("psk31", "rtty", "cw", "unknown"):
        policy = ChunkPolicy.for_mode(registry.get(mode_id))
        assert policy.length_ns == 300 * S
        assert policy.slot_ns is None
        for b in boundaries(policy, utc_ns("23:51:42"), 5):
            assert seconds_into_day(b) % 60 == 0


@pytest.mark.parametrize(
    ("period_s", "length_s"), [(15, 300), (30, 300), (60, 300), (120, 360), (300, 300)]
)
def test_q65_reported_periods(registry: ModeRegistry, period_s: int, length_s: int) -> None:
    policy = ChunkPolicy.for_mode(registry.get("q65"), period_s=period_s)
    assert policy.length_ns == length_s * S


def test_period_must_be_allowed(registry: ModeRegistry) -> None:
    with pytest.raises(ValueError, match="not in"):
        ChunkPolicy.for_mode(registry.get("ft8"), period_s=30)
    with pytest.raises(ValueError, match="async"):
        ChunkPolicy.for_mode(registry.get("cw"), period_s=15)


def test_target_length_is_configurable(registry: ModeRegistry) -> None:
    policy = ChunkPolicy.for_mode(registry.get("wspr"), target_s=600)
    assert policy.length_ns == 600 * S


def test_grid_restarts_at_midnight() -> None:
    # 7-minute chunks don't divide a day: the last chunk is cut at midnight.
    policy = ChunkPolicy(length_ns=420 * S, slot_ns=None)
    t = utc_ns("23:59:00")
    assert policy.next_boundary(t) == utc_ns("00:00:00", day="2026-10-06")
    assert policy.chunk_start(utc_ns("00:03:00", day="2026-10-06")) == utc_ns(
        "00:00:00", day="2026-10-06"
    )


def test_boundary_is_strictly_after(registry: ModeRegistry) -> None:
    policy = ChunkPolicy.for_mode(registry.get("ft8"))
    on_boundary = utc_ns("12:05:00")
    assert policy.chunk_start(on_boundary) == on_boundary
    assert policy.next_boundary(on_boundary) == utc_ns("12:10:00")
