# SPDX-License-Identifier: Apache-2.0
"""What the operator chose to share, and who they are on Hugging Face."""

from __future__ import annotations

import re
from dataclasses import dataclass

GRID_PRECISIONS = (0, 4, 6, 8)  # 0 = withheld
DEFAULT_GRID_PRECISION = 4
_GRID = re.compile(r"^[A-R]{2}([0-9]{2}([A-X]{2}([0-9]{2})?)?)?$", re.IGNORECASE)


def normalise_grid(grid: str) -> str:
    """Maidenhead in its usual form: FN42ab12."""
    g = grid.strip()
    if not _GRID.match(g):
        raise ValueError(f"{grid!r} is not a Maidenhead grid locator")
    return g[:2].upper() + g[2:4] + g[4:6].lower() + g[6:8]


def normalise_callsign(callsign: str) -> str:
    c = callsign.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+(/[A-Z0-9]+)*", c) or not 3 <= len(c) <= 15:
        raise ValueError(f"{callsign!r} doesn't look like a callsign")
    return c


@dataclass(frozen=True)
class Consent:
    accepted_ns: int
    license_id: str


@dataclass(frozen=True)
class StationSettings:
    callsign: str | None = None
    share_callsign: bool = False
    grid: str | None = None
    grid_precision: int = DEFAULT_GRID_PRECISION
    hf_username: str | None = None
    station_profile_id: str | None = None
    consent: Consent | None = None

    def __post_init__(self) -> None:
        if self.grid_precision not in GRID_PRECISIONS:
            raise ValueError(f"grid precision must be one of {GRID_PRECISIONS}")
        if self.callsign is not None:
            object.__setattr__(self, "callsign", normalise_callsign(self.callsign))
        if self.grid is not None:
            object.__setattr__(self, "grid", normalise_grid(self.grid))

    @property
    def shared_grid(self) -> str | None:
        """The grid at the chosen precision, or None if withheld or unknown."""
        if self.grid is None or self.grid_precision == 0:
            return None
        return self.grid[: self.grid_precision]  # the whole grid if it's shorter
