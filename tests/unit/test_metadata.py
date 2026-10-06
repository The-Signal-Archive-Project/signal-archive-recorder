# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 6: public metadata, schemas and privacy."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder.core.events import (
    CaptureWarning,
    Decode,
    FreqChanged,
    ModeChanged,
    SettingChanged,
    SourceDown,
    SourceUp,
)
from signal_archive_recorder.metadata.builder import MetadataBuilder, MetadataError, band_for
from signal_archive_recorder.metadata.privacy import DecodeRedactor, Scrubber, machine_secrets
from signal_archive_recorder.metadata.settings import Consent, StationSettings
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener
from tests.fakes.session_rig import REGISTRY, Rig, frames, utc_ns

SOURCE_UNAVAILABLE = {"value": None, "reason": "source_unavailable"}


def run(tmp_path: Path, settings: StationSettings | None = None, **rig: Any) -> Rig:
    builder = MetadataBuilder(REGISTRY, settings or StationSettings())
    return Rig(tmp_path, builder=builder, **rig)


def wsjtx(rig: Rig, mode: str = "ft8", **mode_kw: Any) -> None:
    rig.publish(SourceUp(source="wsjtx", detail="WSJT-X 3.0.2"))
    rig.publish(FreqChanged(source="wsjtx", dial_hz=14_074_000))
    rig.publish(ModeChanged(source="wsjtx", mode_id=mode, raw_mode=mode.upper(), **mode_kw))


def test_unknown_is_null_with_reason(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=20)  # no decoder and no rig control running
    [chunk] = rig.finish()
    radio = chunk["radio"]
    for field in ("dial_hz", "band", "rig_mode", "sideband", "filter_hz", "agc", "noise_blanker",
                  "noise_reduction"):  # fmt: skip
        assert radio[field] == SOURCE_UNAVAILABLE, field
    assert chunk["mode"]["mode_id"] == SOURCE_UNAVAILABLE
    assert chunk["mode"]["params"] == {}
    assert "decodes" not in chunk
    assert rig.label_stats() == {}  # no decoder, no labels
    assert chunk["path"]["type"] == SOURCE_UNAVAILABLE


def test_values_present_when_reported(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=20)
    wsjtx(rig)
    rig.publish(SourceUp(source="rigctld"))
    rig.publish(SettingChanged(source="rigctld", name="rig_mode", value="PKTUSB"))
    rig.publish(SettingChanged(source="rigctld", name="filter_hz", value=3000))
    [chunk] = rig.finish()
    radio = chunk["radio"]
    assert radio["dial_hz"] == {"value": 14_074_000, "reason": None, "source": "wsjtx"}
    assert radio["band"]["value"] == "20m"
    assert radio["rig_mode"] == {"value": "PKTUSB", "reason": None, "source": "rigctld"}
    assert radio["filter_hz"]["value"] == 3000
    assert radio["agc"] == SOURCE_UNAVAILABLE


def test_median_dt(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=30)
    wsjtx(rig)
    rig.pump_to(10)
    for dt in (0.1, 0.3, -0.2, 0.5):
        rig.publish(Decode(source="wsjtx", mode_id="ft8", text="CQ K1ABC FN42", dt_s=dt))
    rig.publish(Decode(source="wsjtx", mode_id="ft8", text="CQ X", dt_s=2.0, off_air=True))
    [chunk] = rig.finish()
    stats = rig.label_stats()[chunk["chunk_id"]]
    assert stats["count"]["value"] == 4
    assert stats["median_dt_s"]["value"] == pytest.approx(0.2)  # off-air excluded
    assert stats["off_air_count"] == 1


def test_median_dt_not_applicable_for_async(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=20)
    wsjtx(rig, mode="psk31")
    rig.publish(Decode(source="wsjtx", mode_id="psk31", text="cq cq", dt_s=0.1))
    [chunk] = rig.finish()
    stats = rig.label_stats()[chunk["chunk_id"]]
    na = {"value": None, "reason": "not_applicable"}
    assert stats["count"] == na and stats["median_dt_s"] == na


def test_no_decodes_is_not_reported(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=20)
    wsjtx(rig)
    [chunk] = rig.finish()
    stats = rig.label_stats()[chunk["chunk_id"]]  # the decoder ran but heard nothing
    assert stats["count"]["value"] == 0
    assert stats["median_dt_s"] == {"value": None, "reason": "not_reported"}


def test_events_list(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=40)
    wsjtx(rig)
    rig.pump_to(10)
    rig.publish(SourceUp(source="rigctld"))
    rig.publish(SettingChanged(source="rigctld", name="agc", value="fast"))
    rig.pump_to(20)
    rig.publish(FreqChanged(source="wsjtx", dial_hz=7_074_000))
    chunks = rig.finish()
    first = [(e["type"], e["frame_offset"]) for e in chunks[0]["events"]]
    assert ("SettingChanged", frames(10)) in first
    assert [e["type"] for e in chunks[1]["events"]] == ["FreqChanged"]
    event = chunks[1]["events"][0]
    assert event == {
        "type": "FreqChanged",
        "source": "wsjtx",
        "t_ns": utc_ns("12:03:27"),
        "frame_offset": 0,
        "dial_hz": 7_074_000,
    }


def _text_files(folder: Path) -> list[tuple[str, str]]:
    return [
        (str(p.relative_to(folder)), p.read_bytes().decode("latin-1"))
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    ]


def test_privacy_no_leaks(tmp_path: Path) -> None:
    home = str(Path.home())
    device = "USB Audio CODEC SN:7F3A9C21"
    settings = StationSettings(callsign="W9XYZ", grid="EN52wa", hf_username="volunteer-hf")
    builder = MetadataBuilder(REGISTRY, settings, extra_secrets=(device,))
    rig = Rig(tmp_path, seconds=30, builder=builder)
    secrets = [*machine_secrets(), device, "7F3A9C21", "W9XYZ", "EN52wa", "EN52WA"]

    listener = WsjtxListener(
        rig.bus, REGISTRY, rig.clock, decode_log=rig.session.decode_log("wsjtx")
    )
    status = m.Status(
        f"WSJT-X {machine_secrets()[0]}", 14_074_000, "FT8", None, "-15", "FT8", False, False,
        False, 1500, 1500, "W9XYZ", "EN52wa", None, False, None, False, 0, None, None,
        "Default", "CQ W9XYZ EN52",
    )  # fmt: skip
    listener.handle(m.encode(status))
    rig.publish(SourceUp(source="sneaky", detail=f"helper on {machine_secrets()[0]}"))
    rig.publish(CaptureWarning(source="audio", code="os_resampling", message=f"{device} at {home}"))
    rig.publish(SettingChanged(source="sneaky", name="debug_path", value=f"{home}/x"))
    rig.pump_to(10)
    for text in ("W9XYZ K1ABC -12", "CQ W9XYZ EN52", "VE3/W9XYZ K1ABC R-03"):
        listener.handle(m.encode(m.Decode(status.client_id, True, 1000, -5, 0.1, 900, "~", text,
                                          False, False)))  # fmt: skip
    rig.bus.wait_idle()
    rig.publish(SourceDown(source="sneaky", reason=f"crashed: OSError('{home}/.cfg')"))
    rig.finish()

    for name, text in _text_files(rig.session.path):
        for secret in secrets:
            assert secret.lower() not in text.lower(), f"{secret!r} leaked into {name}"
    session = json.loads((rig.session.path / "session.json").read_text())
    assert session["operator"]["callsign"] == {"value": None, "reason": "user_withheld"}
    assert session["location"]["grid"]["value"] == "EN52"  # default precision 4
    assert session["operator"]["hf_username"] == "volunteer-hf"  # public on HF anyway
    decodes = [json.loads(x) for x in rig.session.decode_log("wsjtx").read_text().splitlines()]
    assert [d["text"] for d in decodes] == [
        "<OWN_CALL> K1ABC -12",
        "CQ <OWN_CALL> EN52",  # the grid is shared at 4 characters
        "<OWN_CALL> K1ABC R-03",
    ]
    assert all(d["redacted"] for d in decodes)


@pytest.mark.parametrize(
    ("grid", "precision", "expected", "reason"),
    [
        ("EN52wa", 4, "EN52", None),
        ("EN52wa", 6, "EN52wa", None),
        ("EN52wa38", 8, "EN52wa38", None),
        ("EN52wa", 0, None, "user_withheld"),
        ("EN52", 6, "EN52", None),  # can't share more than is known
        (None, 4, None, "not_reported"),
    ],
)
def test_grid_precision(
    tmp_path: Path, grid: str | None, precision: int, expected: str | None, reason: str | None
) -> None:
    rig = run(tmp_path, StationSettings(grid=grid, grid_precision=precision), seconds=5)
    rig.finish()
    location = json.loads((rig.session.path / "session.json").read_text())["location"]
    assert location["grid"] == {"value": expected, "reason": reason}
    assert location["grid_precision"] == len(expected or "")


def test_callsign_shared_when_chosen(tmp_path: Path) -> None:
    settings = StationSettings(callsign="w9xyz", share_callsign=True, grid="EN52wa")
    rig = run(tmp_path, settings, seconds=5)
    rig.finish()
    session = json.loads((rig.session.path / "session.json").read_text())
    assert session["operator"]["callsign"] == {"value": "W9XYZ", "reason": None}
    redactor = DecodeRedactor(settings)
    assert redactor.text("W9XYZ K1ABC -12") == ("W9XYZ K1ABC -12", False)


def test_redaction_rules() -> None:
    hidden = DecodeRedactor(StationSettings(callsign="W9XYZ", grid="EN52wa"))
    assert hidden.text("W9XYZ/P K1ABC 73") == ("<OWN_CALL> K1ABC 73", True)
    assert hidden.text("W9XYZA K1ABC 73") == ("W9XYZA K1ABC 73", False)  # a different call
    assert hidden.text("CQ K1ABC FN42") == ("CQ K1ABC FN42", False)
    no_grid = DecodeRedactor(
        StationSettings(callsign="W9XYZ", share_callsign=True, grid="EN52wa", grid_precision=0)
    )
    assert no_grid.text("CQ W9XYZ EN52") == ("CQ W9XYZ <OWN_GRID>", True)
    assert no_grid.text("CQ K1ABC EN52") == ("CQ K1ABC EN52", False)  # a neighbour's grid stays


def test_scrubber() -> None:
    scrub = Scrubber(["SN:1234"])
    home = str(Path.home())
    text = scrub.text(f"opened {home}/a.wav and C:\\Users\\bob\\x on SN:1234")
    assert home not in text and "bob" not in text and "SN:1234" not in text


def test_extra_fields_stripped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    rig = run(tmp_path, seconds=10)
    wsjtx(rig, mode="q65", period_s=30.0, params={"freq_tolerance_hz": 50, "debug": "x"})
    rig.publish(SourceUp(source="sneaky"))
    rig.publish(SettingChanged(source="sneaky", name="debug_path", value="/tmp/x"))
    [chunk] = rig.finish()
    assert chunk["mode"]["params"] == {"period_s": 30.0, "freq_tolerance_hz": 50}
    assert "debug_path" not in json.dumps(chunk)
    dropped = rig.manager.builder.dropped
    assert any("debug_path" in d for d in dropped)
    assert any("'debug'" in d for d in dropped)
    assert "dropped" in caplog.text


def test_unmapped_mode_flagged(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=10)
    rig.publish(SourceUp(source="wsjtx"))
    rig.publish(ModeChanged(source="wsjtx", mode_id="unknown", raw_mode="FT9", needs_mapping=True))
    [chunk] = rig.finish()  # validated before it was written
    assert chunk["mode"]["mode_id"]["value"] == "unknown"
    assert (chunk["mode"]["raw"], chunk["mode"]["needs_mapping"]) == ("FT9", True)


def test_invalid_metadata_never_published(tmp_path: Path) -> None:
    rig = run(tmp_path, seconds=5)

    def broken(_: dict[str, Any]) -> dict[str, Any]:
        raise MetadataError("simulated bug")

    rig.manager.builder.chunk = broken  # type: ignore[method-assign]
    assert rig.finish() == []
    local = {p.name for p in rig.session.local.iterdir()}
    assert any(n.endswith(".meta.invalid.json") for n in local)  # kept, but never published
    assert not any(p.name.endswith(".meta.json") for p in rig.session.recordings.iterdir())


def test_consent_recorded(tmp_path: Path) -> None:
    consent = Consent(accepted_ns=utc_ns("09:00:00"), license_id="CC-BY-4.0")
    rig = run(tmp_path, StationSettings(consent=consent), seconds=5)
    rig.finish()
    session = json.loads((rig.session.path / "session.json").read_text())
    assert session["consent"] == {
        "accepted_utc": "2026-10-05T09:00:00.000000Z",
        "license_id": "CC-BY-4.0",
    }


@pytest.mark.parametrize(
    ("dial", "band"),
    [(14_074_000, "20m"), (7_047_500, "40m"), (50_313_000, "6m"), (9_000_000, None)],
)
def test_band_lookup(dial: int, band: str | None) -> None:
    assert band_for(dial) == band


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="grid"):
        StationSettings(grid="ZZ99")
    with pytest.raises(ValueError, match="precision"):
        StationSettings(grid_precision=5)
    with pytest.raises(ValueError, match="callsign"):
        StationSettings(callsign="not a call")
    assert StationSettings(grid="en52WA").grid == "EN52wa"
