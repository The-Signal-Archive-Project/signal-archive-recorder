# SPDX-License-Identifier: Apache-2.0
"""Every registered mode produces session, chunk and decode files that validate."""

import json
from pathlib import Path

import jsonschema
import pytest

from signal_archive_recorder.core.events import Decode, FreqChanged, ModeChanged, SourceUp
from signal_archive_recorder.metadata.builder import MetadataBuilder, _schemas
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.modes import Mode
from tests.fakes.session_rig import REGISTRY, Rig

MODES = [m for m in REGISTRY if m.id != "unknown"]


def validator(name: str) -> jsonschema.Draft202012Validator:
    schemas, registry = _schemas()
    return jsonschema.Draft202012Validator(schemas[name], registry=registry)


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.id)
def test_session_and_chunk_validate(tmp_path: Path, mode: Mode) -> None:
    settings = StationSettings(callsign="W9XYZ", share_callsign=True, grid="EN52wa")
    rig = Rig(tmp_path, seconds=12, builder=MetadataBuilder(REGISTRY, settings))
    period = mode.timing.allowed_periods_ns[-1] / 1e9 if mode.timing.slotted else None
    rig.publish(SourceUp(source="decoder", detail="test decoder 1.0"))
    rig.publish(FreqChanged(source="decoder", dial_hz=14_074_000))
    rig.publish(ModeChanged(source="decoder", mode_id=mode.id, raw_mode=mode.display_name,
                            period_s=period))  # fmt: skip
    rig.pump_to(5)
    rig.publish(Decode(source="decoder", mode_id=mode.id, text="CQ K1ABC FN42", dt_s=0.2))
    chunks = rig.finish()

    folder = rig.session.path
    for path in (folder / "recordings").glob("*.meta.json"):
        validator("chunk").validate(json.loads(path.read_text()))
    validator("session").validate(json.loads((folder / "session.json").read_text()))
    stats = rig.label_stats("decoder")
    assert set(stats) == {c["chunk_id"] for c in chunks}
    for line in stats.values():
        validator("label_stats").validate(line)
    assert chunks and all(c["mode"]["mode_id"]["value"] == mode.id for c in chunks)
    if mode.params_schema and period is not None:
        assert chunks[0]["mode"]["params"]["period_s"] == period
