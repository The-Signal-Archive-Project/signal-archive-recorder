# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Keeping room audio out: is each chunk really the radio the decoder listened to?"""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from signal_archive_recorder.core.events import FreqChanged, ModeChanged, SourceUp
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener
from signal_archive_recorder.upload.queue import UploadRecord, UploadState
from signal_archive_recorder.upload.screening import (
    band_excess_db,
    judge,
    passband_ratio_db,
    screen_session,
)
from tests.fakes.fake_signals import add_at, ft8_like, receiver_noise, to_int16
from tests.fakes.session_rig import FMT, REGISTRY, Rig, utc_ns
from tests.unit.test_upload import env, uploader  # noqa: F401  (fixtures and helpers)

SR = FMT.sample_rate  # 8 kHz: FT8 sits at 200-3000 Hz
SLOTS = [8, 23, 38]  # 12:03:15, :30 and :45, seconds into a session starting 12:03:07
DFS = [700, 1500]


def session(
    root: Path,
    *,
    signals: bool,
    decoder: bool = True,
    seconds: float = 60,
    decode_slots: list[float] = SLOTS,
    signal_slots: list[float] = SLOTS,
) -> Path:
    """A session from a fake receiver; optionally the decoder reports what it 'heard'."""
    audio = receiver_noise(SR, seconds)
    if signals:
        for i, slot in enumerate(signal_slots):
            for j, df in enumerate(DFS):
                add_at(
                    audio, ft8_like(SR, df, amplitude=0.03, seed=10 * i + j), SR, slot + 0.5 + 0.1
                )
    rig = Rig(
        root,
        seconds=seconds,
        data=to_int16(audio),
        builder=MetadataBuilder(REGISTRY, StationSettings()),
    )
    if decoder:
        listener = WsjtxListener(
            rig.bus, REGISTRY, rig.clock, decode_log=rig.session.decode_log("wsjtx")
        )
        rig.publish(SourceUp(source="wsjtx", detail="WSJT-X 3.0.2"))
        rig.publish(FreqChanged(source="wsjtx", dial_hz=14_074_000))
        rig.publish(ModeChanged(source="wsjtx", mode_id="ft8", raw_mode="FT8"))
        for slot in decode_slots:
            rig.pump_to(slot + 14.5)  # WSJT-X reports after the slot
            slot_ms = (utc_ns("12:03:07") // 1_000_000 + int(slot * 1000)) % 86_400_000
            for df in DFS:
                decode = m.Decode(
                    "WSJT-X", True, slot_ms, -10, 0.1, df, "~", f"CQ K1ABC {df}", False, False
                )
                listener.handle(m.encode(decode))
            rig.bus.wait_idle()
    rig.finish()
    return rig.session.path


# -- whole sessions -----------------------------------------------------------------


def test_radio_audio_passes(tmp_path: Path) -> None:
    [screen] = screen_session(session(tmp_path, signals=True), REGISTRY)
    assert screen.eligible, screen.reasons
    assert (screen.decodes_checked, screen.decodes_visible) == (6, 6)


def test_wrong_input_is_kept_back(tmp_path: Path) -> None:
    """WSJT-X is decoding the radio, but the recorder hears something else."""
    [screen] = screen_session(session(tmp_path, signals=False), REGISTRY)
    assert not screen.eligible
    assert (screen.decodes_checked, screen.decodes_visible) == (6, 0)
    assert "different input" in screen.reasons[0]


def test_no_decoder_is_kept_back_unless_allowed(tmp_path: Path) -> None:
    folder = session(tmp_path, signals=True, decoder=False)
    [screen] = screen_session(folder, REGISTRY)
    assert not screen.eligible and "no decoder" in screen.reasons[0]
    [allowed] = screen_session(folder, REGISTRY, require_decoder=False)
    assert allowed.eligible and "no decoder" in allowed.warnings[0]


def test_quiet_band_is_not_held_against_the_operator(tmp_path: Path) -> None:
    """Decoder running, band dead: can't confirm, but nothing says it's a microphone."""
    [screen] = screen_session(session(tmp_path, signals=False, decode_slots=[]), REGISTRY)
    assert screen.eligible
    assert any("too few decodes" in w for w in screen.warnings)


# -- uploads ------------------------------------------------------------------------


def test_upload_keeps_back_only_the_bad_chunk(env: dict[str, Any]) -> None:  # noqa: F811
    # 150 s crosses 12:05:00, so two chunks; the second has decodes but none of the signals.
    folder = session(
        env["root"], signals=True, seconds=150, decode_slots=[8, 23, 128, 143], signal_slots=[8, 23]
    )
    first, second = json.loads((folder / "session.json").read_text())["chunks"]
    up = uploader(env)
    record = up.upload(folder)
    assert record.state is UploadState.PR_OPENED
    assert [e["chunk_id"] for e in record.excluded] == [second]
    sent = " ".join(env["hub"].prs[0].files)
    assert first in sent and second not in sent
    kept = folder / "local" / "excluded"
    assert (kept / f"{second}.flac").exists()
    assert "different input" in (kept / f"{second}.why.txt").read_text()
    assert (folder / "local" / "screening.json").exists()


def test_upload_blocks_a_session_with_no_radio_audio(env: dict[str, Any]) -> None:  # noqa: F811
    folder = session(env["root"], signals=False)
    record = uploader(env).upload(folder)
    assert record.state is UploadState.BLOCKED
    assert any("different input" in p for p in record.problems)
    assert env["hub"].prs == []
    assert list((folder / "recordings").glob("*.flac"))  # nothing moved: it's all there
    assert UploadRecord.load(folder).state is UploadState.BLOCKED


def test_dry_run_says_what_would_be_kept_back(
    env: dict[str, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    from signal_archive_recorder import cli

    folder = session(
        env["root"], signals=True, seconds=150, decode_slots=[8, 23, 128, 143], signal_slots=[8, 23]
    )
    second = json.loads((folder / "session.json").read_text())["chunks"][1]
    uploader(env)
    assert cli.main(["upload", "--dry-run", "--config", str(env["config"])]) == 0
    out = capsys.readouterr().out
    assert f"{second} would be KEPT BACK" in out
    assert f"{second}.flac" not in out.split("KEPT BACK")[0]  # not in the files to send
    assert (folder / "recordings" / f"{second}.flac").exists()  # and nothing moved


# -- the measurements ---------------------------------------------------------------


def test_band_excess_finds_a_signal_only_where_it_is() -> None:
    audio = receiver_noise(SR, 15)
    add_at(audio, ft8_like(SR, 1200, amplitude=0.03), SR, 0.6)
    assert band_excess_db(audio, SR, 1200, 50, 3, 10) > 6  # type: ignore[operator]
    assert abs(band_excess_db(audio, SR, 2000, 50, 3, 10)) < 1.5  # type: ignore[arg-type]
    assert band_excess_db(audio, SR, 100, 50, 3, 10) is None  # too near the edge to judge
    assert band_excess_db(audio, SR, 1200, 50, 10, 20) is None  # past the end


def test_passband_tells_a_receiver_from_a_microphone() -> None:
    sr = 48_000
    white = receiver_noise(sr, 12)
    spectrum = np.fft.rfft(white)
    f = np.fft.rfftfreq(len(white), 1 / sr)
    spectrum[(f < 300) | (f > 2700)] = 0  # a 2.4 kHz receiver filter
    receiver = np.fft.irfft(spectrum, len(white))
    assert passband_ratio_db(receiver, sr) > 30  # type: ignore[operator]
    assert passband_ratio_db(white, sr) < 10  # type: ignore[operator]
    assert passband_ratio_db(receiver_noise(SR, 12), SR) is None  # 8 kHz can't show it


@pytest.mark.parametrize(
    ("kw", "eligible", "reason"),
    [
        ({"mode_known": True, "checked": 6, "visible": 6, "passband_db": 40.0}, True, None),
        (
            {"mode_known": True, "checked": 6, "visible": 2, "passband_db": 40.0},
            False,
            "different input",
        ),
        ({"mode_known": True, "checked": 6, "visible": 3, "passband_db": 40.0}, True, None),  # half
        (
            {"mode_known": True, "checked": 1, "visible": 0, "passband_db": 40.0},
            True,
            None,
        ),  # too few
        ({"mode_known": True, "checked": 0, "visible": 0, "passband_db": 4.0}, False, "microphone"),
        (
            {"mode_known": False, "checked": 0, "visible": 0, "passband_db": None},
            False,
            "no decoder",
        ),
    ],
)
def test_judge(kw: dict[str, Any], eligible: bool, reason: str | None) -> None:
    ok, reasons, _ = judge(require_decoder=True, **kw)
    assert ok is eligible
    if reason:
        assert reason in " ".join(reasons)
