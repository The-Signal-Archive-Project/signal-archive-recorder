# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import copy
import json
from decimal import Decimal
from importlib.resources import files
from typing import Any

import jsonschema
import pytest

from signal_archive_recorder.modes import ModeRegistry, RegistryError, Resolution

REQUIRED_MODES = {
    "ft8", "ft4", "wspr", "jt65", "q65", "msk144", "js8", "psk31", "rtty", "cw", "unknown",
}  # fmt: skip


def _raw_registry() -> dict[str, Any]:
    pkg = files("signal_archive_recorder.modes")
    data: dict[str, Any] = json.loads(
        pkg.joinpath("modes.json").read_text(encoding="utf-8"), parse_float=Decimal
    )
    return data


@pytest.fixture(scope="module")
def registry() -> ModeRegistry:
    return ModeRegistry.load_default()


def test_registry_loads_and_validates(registry: ModeRegistry) -> None:
    pkg = files("signal_archive_recorder.modes")
    schema = json.loads(pkg.joinpath("registry.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(_raw_registry(), schema)
    assert {m.id for m in registry} >= REQUIRED_MODES


def test_duplicate_id_rejected() -> None:
    data = _raw_registry()
    data["modes"].append(copy.deepcopy(data["modes"][0]) | {"aliases": {}})
    with pytest.raises(RegistryError, match="duplicate mode id"):
        ModeRegistry(data)


def test_duplicate_alias_per_source_rejected() -> None:
    data = _raw_registry()
    ft4 = next(m for m in data["modes"] if m["id"] == "ft4")
    ft4["aliases"]["wsjtx"].append("ft8")  # clashes with FT8, ignoring case
    with pytest.raises(RegistryError, match="maps to both"):
        ModeRegistry(data)


def test_same_alias_different_sources_allowed(registry: ModeRegistry) -> None:
    assert registry.resolve("wsjtx", "FT8").mode is registry.resolve("jtdx", "FT8").mode


def test_rig_control_sources_cannot_alias() -> None:
    """Option B: a rig setting such as PKTUSB or CW is never evidence of the mode."""
    data = _raw_registry()
    data["modes"][0]["aliases"]["hamlib"] = ["PKTUSB"]
    with pytest.raises(RegistryError, match="schema validation"):
        ModeRegistry(data)


def test_schema_violation_rejected() -> None:
    data = _raw_registry()
    data["modes"][0]["family"] = "telepathy"
    with pytest.raises(RegistryError, match="schema validation"):
        ModeRegistry(data)


def test_unknown_fallback_required() -> None:
    data = _raw_registry()
    data["modes"] = [m for m in data["modes"] if m["id"] != "unknown"]
    with pytest.raises(RegistryError, match="fallback"):
        ModeRegistry(data)


@pytest.mark.parametrize(
    ("timing", "message"),
    [
        ({"period_s": 15, "allowed_periods_s": [30]}, "must be one of"),
        ({"period_s": 7, "allowed_periods_s": [7]}, "does not divide a day"),
        ({"period_s": Decimal("1e-10"), "allowed_periods_s": [Decimal("1e-10")]}, "nanoseconds"),
    ],
)
def test_bad_timing_rejected(timing: dict[str, Any], message: str) -> None:
    data = _raw_registry()
    data["modes"][0]["timing"].update(timing)
    with pytest.raises(RegistryError, match=message):
        ModeRegistry(data)


def test_alias_lookup(registry: ModeRegistry) -> None:
    ft8 = registry.resolve("wsjtx", "FT8")
    assert ft8.kind is Resolution.EXACT
    assert ft8.mode.id == "ft8"
    assert not ft8.needs_mapping

    # Rig modes aren't modes: a rig source never resolves them, and if asked, they
    # are just unknown strings.
    assert registry.resolve("hamlib", "PKTUSB").kind is Resolution.UNKNOWN

    weird = registry.resolve("wsjtx", "FT9-EXPERIMENTAL")
    assert weird.kind is Resolution.UNKNOWN
    assert weird.mode.id == "unknown"
    assert weird.raw == "FT9-EXPERIMENTAL"
    assert weird.needs_mapping


def test_lookup_ignores_case_and_whitespace(registry: ModeRegistry) -> None:
    res = registry.resolve("fldigi", " bpsk31 ")
    assert res.mode.id == "psk31"
    assert res.raw == " bpsk31 "  # raw is kept exactly as reported


def test_alias_is_per_source(registry: ModeRegistry) -> None:
    # fldigi calls it BPSK31; WSJT-X never reports that string.
    assert registry.resolve("wsjtx", "BPSK31").kind is Resolution.UNKNOWN


def test_get_by_id(registry: ModeRegistry) -> None:
    assert registry.get("wspr").display_name == "WSPR"
    with pytest.raises(KeyError):
        registry.get("nope")


def test_missing_params_schema_rejected() -> None:
    data = _raw_registry()
    data["modes"][0]["params_schema"] = "nope_params.schema.json"
    with pytest.raises(RegistryError, match="missing"):
        ModeRegistry(data)
