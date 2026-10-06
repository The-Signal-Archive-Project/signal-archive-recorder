# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Contract tests run against every mode in the registry.

A new modes.json entry is covered here automatically. If one fails, fix the entry
rather than special-casing the mode.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from signal_archive_recorder.modes import ChunkPolicy, Mode, ModeRegistry, Resolution
from signal_archive_recorder.modes.chunk_policy import DAY_NS, DEFAULT_CHUNK_S

S = 10**9
REGISTRY = ModeRegistry.load_default()
MODES = list(REGISTRY)
SLOTTED = [(m, period) for m in MODES if m.timing.slotted for period in m.timing.allowed_periods_ns]
# 2000-01-01 to 2100-01-01, in nanoseconds
TIMES = st.integers(min_value=946_684_800 * S, max_value=4_102_444_800 * S)


def _mode_period_id(param: object) -> str:
    if isinstance(param, tuple):
        mode, period = param
        return f"{mode.id}@{period / S:g}s"
    return str(param)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.id)
def test_every_alias_resolves_to_its_mode(mode: Mode) -> None:
    for source, raws in mode.aliases.items():
        for raw in raws:
            res = REGISTRY.resolve(source, raw)
            assert res.kind is Resolution.EXACT
            assert res.mode is mode


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.id)
def test_default_policy_exists(mode: Mode) -> None:
    policy = ChunkPolicy.for_mode(mode)
    assert policy.length_ns >= DEFAULT_CHUNK_S * S


@pytest.mark.parametrize("mode_period", SLOTTED, ids=_mode_period_id)
@given(t=TIMES)
def test_chunk_policy_property(mode_period: tuple[Mode, int], t: int) -> None:
    mode, period_ns = mode_period
    policy = ChunkPolicy.for_mode(mode, period_s=period_ns / S)

    assert policy.length_ns % period_ns == 0
    assert policy.length_ns >= DEFAULT_CHUNK_S * S
    assert policy.length_ns - period_ns < DEFAULT_CHUNK_S * S  # smallest such multiple

    start, end = policy.chunk_start(t), policy.next_boundary(t)
    assert start <= t < end
    assert end - start <= policy.length_ns
    for boundary in (start, end):
        assert (boundary % DAY_NS) % period_ns == 0, "boundary is not a slot edge"
    assert policy.chunk_start(end) == end
