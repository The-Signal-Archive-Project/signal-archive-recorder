# SPDX-License-Identifier: Apache-2.0
"""Where audio chunks start and end, derived from a mode's slot timing.

Chunks sit on a grid anchored to UTC midnight. A slotted mode's chunk length is the
smallest multiple of its slot period that is at least the target length, so every
boundary is a slot edge. Async modes use the target length directly.

All times are integer nanoseconds since the Unix epoch (UTC, POSIX time).
"""

from __future__ import annotations

from dataclasses import dataclass

from signal_archive_recorder.modes.registry import NS_PER_S, SECONDS_PER_DAY, Mode

DEFAULT_CHUNK_S = 300
DAY_NS = SECONDS_PER_DAY * NS_PER_S


@dataclass(frozen=True)
class ChunkPolicy:
    length_ns: int
    slot_ns: int | None

    def __post_init__(self) -> None:
        if self.length_ns <= 0:
            raise ValueError("chunk length must be positive")
        if self.slot_ns is not None and self.length_ns % self.slot_ns:
            raise ValueError("chunk length must be a multiple of the slot period")

    @classmethod
    def for_mode(
        cls,
        mode: Mode,
        *,
        period_s: float | None = None,
        target_s: int = DEFAULT_CHUNK_S,
    ) -> ChunkPolicy:
        """Policy for a mode, optionally with the slot period the source reports in use."""
        target_ns = target_s * NS_PER_S
        timing = mode.timing
        if not timing.slotted:
            if period_s is not None:
                raise ValueError(f"{mode.id} is async and has no slot period")
            return cls(length_ns=target_ns, slot_ns=None)

        assert timing.period_ns is not None
        slot_ns = timing.period_ns
        if period_s is not None:
            slot_ns = round(period_s * NS_PER_S)
            if slot_ns not in timing.allowed_periods_ns:
                allowed = [p / NS_PER_S for p in timing.allowed_periods_ns]
                raise ValueError(f"{mode.id}: period {period_s} s not in {allowed}")
        slots = -(-target_ns // slot_ns)  # ceiling division
        return cls(length_ns=slots * slot_ns, slot_ns=slot_ns)

    def chunk_start(self, t_ns: int) -> int:
        """Start of the grid chunk containing t_ns."""
        day_start = t_ns - t_ns % DAY_NS
        into_day = t_ns - day_start
        return day_start + into_day - into_day % self.length_ns

    def next_boundary(self, t_ns: int) -> int:
        """First chunk boundary strictly after t_ns.

        The grid restarts at each UTC midnight, so if the chunk length doesn't divide
        a day, the last chunk of the day is shorter.
        """
        day_start = t_ns - t_ns % DAY_NS
        return min(self.chunk_start(t_ns) + self.length_ns, day_start + DAY_NS)
