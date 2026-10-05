# SPDX-License-Identifier: Apache-2.0
"""The mode registry: every mode the recorder knows about, loaded from modes.json.

Mode-specific behaviour lives here as data. Core code asks the registry instead of
naming modes itself, so adding a mode is a modes.json change, not a code change.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal

import jsonschema

NS_PER_S = 10**9
SECONDS_PER_DAY = 86_400
UNKNOWN_MODE_ID = "unknown"

Sideband = Literal["usb", "lsb", "none"]


class RegistryError(ValueError):
    """modes.json is malformed or internally inconsistent."""


def seconds_to_ns(seconds: Decimal | int | float) -> int:
    """Convert exactly; reject periods that aren't a whole number of nanoseconds."""
    ns = Decimal(str(seconds)) * NS_PER_S
    if ns != ns.to_integral_value():
        raise RegistryError(f"period {seconds} s is not a whole number of nanoseconds")
    return int(ns)


@dataclass(frozen=True)
class Timing:
    kind: Literal["slotted", "async"]
    period_ns: int | None = None
    allowed_periods_ns: tuple[int, ...] = ()

    @property
    def slotted(self) -> bool:
        return self.kind == "slotted"


@dataclass(frozen=True)
class Mode:
    id: str
    display_name: str
    family: str
    timing: Timing
    nominal_bw_hz: float | None
    sideband: Sideband | None
    aliases: Mapping[str, tuple[str, ...]]
    params_schema: str | None
    notes: str = ""


class Resolution(Enum):
    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModeResolution:
    """The outcome of mapping one source's raw mode string to a registry mode."""

    source: str
    raw: str
    kind: Resolution
    mode: Mode
    candidates: tuple[Mode, ...] = field(default=())

    @property
    def needs_mapping(self) -> bool:
        """True when a maintainer should add this raw string to modes.json."""
        return self.kind is Resolution.UNKNOWN

    @property
    def needs_context(self) -> bool:
        """True when another source (usually the decoder) must pick among candidates."""
        return self.kind is Resolution.AMBIGUOUS


def _load_schema() -> dict[str, Any]:
    text = files(__package__).joinpath("registry.schema.json").read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


def _parse_timing(data: Mapping[str, Any], mode_id: str) -> Timing:
    if data["kind"] == "async":
        return Timing(kind="async")
    period = seconds_to_ns(data["period_s"])
    allowed = tuple(seconds_to_ns(p) for p in data["allowed_periods_s"])
    if period not in allowed:
        raise RegistryError(f"{mode_id}: period_s must be one of allowed_periods_s")
    day_ns = SECONDS_PER_DAY * NS_PER_S
    for p in allowed:
        if day_ns % p:
            raise RegistryError(f"{mode_id}: slot period {p / NS_PER_S} s does not divide a day")
    return Timing(kind="slotted", period_ns=period, allowed_periods_ns=allowed)


class ModeRegistry:
    """Lookup of modes by id and by (source, raw mode string)."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        try:
            jsonschema.validate(data, _load_schema())
        except jsonschema.ValidationError as exc:
            raise RegistryError(f"modes.json failed schema validation: {exc.message}") from exc

        self.version: int = data["registry_version"]
        self._modes: dict[str, Mode] = {}
        self._aliases: dict[tuple[str, str], Mode] = {}

        for entry in data["modes"]:
            mode = Mode(
                id=entry["id"],
                display_name=entry["display_name"],
                family=entry["family"],
                timing=_parse_timing(entry["timing"], entry["id"]),
                nominal_bw_hz=(
                    None if entry["nominal_bw_hz"] is None else float(entry["nominal_bw_hz"])
                ),
                sideband=entry["sideband"],
                aliases=MappingProxyType({s: tuple(a) for s, a in entry["aliases"].items()}),
                params_schema=entry["params_schema"],
                notes=entry.get("notes", ""),
            )
            if mode.id in self._modes:
                raise RegistryError(f"duplicate mode id {mode.id!r}")
            self._modes[mode.id] = mode
            for source, raws in mode.aliases.items():
                for raw in raws:
                    key = (source, raw.casefold())
                    if key in self._aliases:
                        other = self._aliases[key].id
                        raise RegistryError(
                            f"alias {raw!r} for source {source!r} maps to both {other!r} "
                            f"and {mode.id!r}"
                        )
                    self._aliases[key] = mode

        if UNKNOWN_MODE_ID not in self._modes:
            raise RegistryError(f"registry must define the {UNKNOWN_MODE_ID!r} fallback mode")

        self._rig_modes: dict[tuple[str, str], Mapping[str, Any]] = {}
        for source, table in data["rig_modes"].items():
            for raw, info in table.items():
                key = (source, raw.casefold())
                if key in self._aliases:
                    raise RegistryError(
                        f"{raw!r} for source {source!r} is both a mode alias and a rig mode"
                    )
                self._rig_modes[key] = info

    @classmethod
    def load_default(cls) -> ModeRegistry:
        """The registry shipped with the package."""
        text = files(__package__).joinpath("modes.json").read_text(encoding="utf-8")
        return cls(json.loads(text, parse_float=Decimal))

    def __iter__(self) -> Iterator[Mode]:
        return iter(self._modes.values())

    def __len__(self) -> int:
        return len(self._modes)

    def get(self, mode_id: str) -> Mode:
        return self._modes[mode_id]

    @property
    def unknown(self) -> Mode:
        return self._modes[UNKNOWN_MODE_ID]

    def resolve(self, source: str, raw: str) -> ModeResolution:
        """Map a raw mode string from a source. Matching ignores case and outer whitespace."""
        key = (source, raw.strip().casefold())
        if (mode := self._aliases.get(key)) is not None:
            return ModeResolution(source, raw, Resolution.EXACT, mode)
        if (info := self._rig_modes.get(key)) is not None:
            candidates = tuple(
                m
                for m in self
                if m.id != UNKNOWN_MODE_ID
                and m.sideband == info["sideband"]
                and not (info["data"] and m.family == "analog")
            )
            return ModeResolution(source, raw, Resolution.AMBIGUOUS, self.unknown, candidates)
        return ModeResolution(source, raw, Resolution.UNKNOWN, self.unknown)
