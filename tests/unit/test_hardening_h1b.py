# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Hardening H1b: keeping one channel of a stereo input, and the update notice."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
import soundfile as sf

from signal_archive_recorder import cli
from signal_archive_recorder.audio.channels import (
    ChannelPicker,
    analyse,
    kept_format,
    recommend,
)
from signal_archive_recorder.audio.device import DeviceInfo
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.config import ConfigError, load_config, parse_config
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.firstrun.wizard import LevelReport
from signal_archive_recorder.recorder import Recorder
from signal_archive_recorder.updates import UpdateChecker, newest
from tests.fakes.fake_audio import FakeBackend
from tests.fakes.session_rig import utc_ns
from tests.integration.test_e2e import S, fake_ntp, make_config, validator
from tests.unit.test_firstrun import TOKEN, MemoryKeyring, Script, make_wizard

STEREO16 = AudioFormat(8_000, 2, "int16")
STEREO24 = AudioFormat(8_000, 2, "int24")
RNG = np.random.default_rng(7)


def stereo(
    left: npt.NDArray[np.float64], right: npt.NDArray[np.float64], fmt: AudioFormat = STEREO16
) -> bytes:
    """Interleave two float channels (-1..1) into the format's little-endian bytes."""
    scale = 2 ** (fmt.bits - 1) - 1
    ints = np.round(np.stack([left, right], axis=1) * scale).astype("<i4")
    raw = ints.astype("<i4").view(np.uint8).reshape(-1, 2, 4)
    return raw[:, :, : fmt.bytes_per_sample].tobytes()


def tone(seconds: float = 2.0, amp: float = 0.3, hz: float = 1000.0) -> npt.NDArray[np.float64]:
    t = np.arange(int(seconds * 8000)) / 8000
    return amp * np.sin(2 * np.pi * hz * t) + RNG.normal(0, 0.01, len(t))


# -- which channels to keep ------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "keep", "reason"),
    [
        (lambda a: (a, a), "left", "copy"),
        (lambda a: (a, np.zeros_like(a)), "left", "silent"),
        (lambda a: (np.zeros_like(a), a), "right", "silent"),
        (lambda a: (np.zeros_like(a), np.zeros_like(a)), "both", "no_audio"),
        (lambda a: (a, a + RNG.normal(0, 1e-4, len(a))), "left", "nearly_copy"),
        (lambda a: (a, RNG.normal(0, 3e-4, len(a))), "left", "quieter"),
        (lambda a: (a, tone(hz=1700.0)), "both", "different"),  # a second receiver
    ],
)
def test_channel_recommendation(make: Any, keep: str, reason: str) -> None:
    left, right = make(tone())
    for fmt in (STEREO16, STEREO24):
        rec = recommend(analyse(stereo(left, right, fmt), fmt))
        assert (rec.keep, rec.reason) == (keep, reason), fmt


def test_mono_input_keeps_everything() -> None:
    mono = AudioFormat(8_000, 1, "int16")
    rec = recommend(analyse(b"\x01\x00" * 800, mono))
    assert rec.keep == "both" and kept_format(mono, "left") == mono


class Collect:
    def __init__(self) -> None:
        self.data = b""
        self.frames: list[int] = []

    def write(self, data: bytes, stream_frame: int) -> None:
        self.data += data
        self.frames.append(stream_frame)


@pytest.mark.parametrize("fmt", [STEREO16, STEREO24])
@pytest.mark.parametrize("keep", ["left", "right"])
def test_picker_is_bit_exact(fmt: AudioFormat, keep: str) -> None:
    raw = RNG.integers(0, 256, 8000 * fmt.frame_bytes, dtype=np.uint8).tobytes()
    out = Collect()
    picker = ChannelPicker(fmt, keep, [out])  # type: ignore[arg-type]
    half = len(raw) // 2 // fmt.frame_bytes * fmt.frame_bytes
    picker.write(raw[:half], 0)
    picker.write(raw[half:], half // fmt.frame_bytes)
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 2, fmt.bytes_per_sample)
    assert out.data == frames[:, 0 if keep == "left" else 1, :].tobytes()
    assert out.frames == [0, half // fmt.frame_bytes]  # frame numbers unchanged


def test_picker_warns_when_the_dropped_channel_comes_alive() -> None:
    warnings: list[str] = []
    picker = ChannelPicker(STEREO16, "left", [Collect()], on_distinct=warnings.append)
    a = tone(10.0)
    for _ in range(4):
        picker.write(stereo(a, a), 0)  # a copy: fine
    assert warnings == []
    second_rx = tone(10.0, hz=1700.0)
    for _ in range(4):
        picker.write(stereo(a, second_rx), 0)
    assert len(warnings) == 1 and "right channel now carries different audio" in warnings[0]
    picker.write(stereo(a, second_rx), 0)
    assert len(warnings) == 1  # once


def test_recording_keeps_only_the_chosen_channel(tmp_path: Path) -> None:
    clock = FakeClock(utc_ns("12:03:07"))
    left = tone(30.0)
    data = stereo(left, left)
    device = DeviceInfo(0, "Fake Stereo Codec", "fake", 2, 8_000)
    backend = FakeBackend([device], data, on_block=lambda n: clock.advance(n * S // 8000))
    base = make_config(tmp_path)
    config = replace(
        base, audio=replace(base.audio, device="Fake Stereo", channels=2, keep_channel="left")
    )
    recorder = Recorder(config, backend=backend, clock=clock, ntp_probe=fake_ntp)
    session = recorder.start()
    assert backend.stream is not None
    backend.stream.pump(20 * 8000)
    recorder.stop(reason="test")

    meta = json.loads(session.session_json.read_text())
    validator("session").validate(meta)
    assert meta["audio"]["channels"] == 1
    assert meta["audio"]["channel_selection"] == {"device_channels": 2, "kept": "left"}
    [flac] = sorted(session.recordings.glob("*.flac"))
    audio, rate = sf.read(flac, dtype="int16", always_2d=True)
    assert audio.shape[1] == 1 and rate == 8000
    expected = np.frombuffer(data, "<i2").reshape(-1, 2)[: len(audio), 0]
    assert np.array_equal(audio[:, 0], expected)  # the left channel, bit for bit


def test_keep_channel_config() -> None:
    base = {"audio": {"device": "x"}}
    assert parse_config(base).audio.keep_channel == "both"
    assert (
        parse_config({"audio": {"device": "x", "keep_channel": "right"}}).audio.keep_channel
        == "right"
    )
    with pytest.raises(ConfigError, match="keep_channel"):
        parse_config({"audio": {"device": "x", "keep_channel": "middle"}})


def copy_report() -> LevelReport:
    a = tone()
    return LevelReport(-10.5, -20.0, 0, 3.0, analyse(stereo(a, a), STEREO16))


def test_terminal_setup_offers_the_recommendation(tmp_path: Path) -> None:
    script = Script(["I agree", "1", "", "", ""], [TOKEN])  # Enter: take the recommendation
    wizard = make_wizard(tmp_path, script)
    wizard.level_probe = lambda backend, device: copy_report()
    wizard.run(tmp_path / "r.toml")
    assert "right channel is an exact copy" in script.text
    assert load_config(tmp_path / "r.toml").audio.keep_channel == "left"


def test_setup_window_offers_the_recommendation(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.autostart import Autostart
    from signal_archive_recorder.ui.setup_wizard import SetupWizard

    env = make_wizard(tmp_path, Script([]), keyring=MemoryKeyring())
    env.level_probe = lambda backend, device: copy_report()
    config = tmp_path / "config" / "recorder.toml"
    wizard = SetupWizard(env, config, autostart=Autostart("Linux", tmp_path / "autostart"))
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.next()
    wizard.terms.agree.setChecked(True)
    wizard.next()
    wizard.login.token.setText(TOKEN)
    wizard.login.check_token()
    qtbot.waitUntil(wizard.login.isComplete)
    wizard.next()
    assert wizard.audio.channel_row.isHidden()  # nothing known before the test
    wizard.audio.test_level()
    qtbot.waitUntil(lambda: not wizard.audio.channel_row.isHidden())
    assert "exact copy" in wizard.audio.channel_note.text()
    assert wizard.audio.keep_channel() == "left"
    wizard.next()
    wizard.next()
    qtbot.waitUntil(wizard.checks.isComplete)
    wizard.next()
    wizard.accept()
    assert load_config(config).audio.keep_channel == "left"


def test_level_probe_analyses_both_channels(tmp_path: Path) -> None:
    from signal_archive_recorder.audio.file_backend import FileBackend
    from signal_archive_recorder.firstrun.wizard import measure_levels

    a = tone(4.0)
    wav = tmp_path / "s.wav"
    sf.write(wav, np.stack([a, np.zeros_like(a)], axis=1), 8000, subtype="PCM_24")
    backend = FileBackend(wav, speed=20.0)
    report = measure_levels(backend, backend.input_devices()[0], seconds=0.5)
    assert report.channels is not None and report.recommendation is not None
    assert report.recommendation.keep == "left" and report.recommendation.reason == "silent"


# -- update notice ------------------------------------------------------------------------


def rel(tag: str, **kw: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/x/releases/tag/{tag}",
        "prerelease": "b" in tag or "beta" in tag,
        "draft": False,
        **kw,
    }


RELEASES = [
    rel("v0.1.0"),
    rel("v0.2.0-beta.1"),
    rel("v0.2.0-beta.2"),
    rel("v0.3.0", draft=True),
    rel("nightly"),
]


def test_newest_release_rules() -> None:
    assert newest(RELEASES, "0.1.0") is None  # stable users aren't offered betas
    beta = newest(RELEASES, "0.2.0b1")
    assert beta is not None and beta.label == "0.2.0-beta.2" and beta.url.endswith("beta.2")
    assert newest(RELEASES, "0.2.0b2") is None  # newest already; the draft doesn't count
    stable = newest([*RELEASES, rel("v0.2.0")], "0.2.0b2")
    assert stable is not None and stable.label == "0.2.0"  # betas move on to the release
    assert newest([*RELEASES, rel("v0.2.0")], "0.1.0").label == "0.2.0"  # type: ignore[union-attr]


def test_checker_announces_once_and_survives_failures() -> None:
    seen: list[str] = []
    answers: list[Any] = [OSError("offline"), RELEASES, RELEASES]

    def fetch() -> list[dict[str, Any]]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    checker = UpdateChecker(lambda r: seen.append(r.label), fetch=fetch, current="0.2.0b1")
    assert checker.check_now() is None  # offline: nothing, no crash
    checker.check_now()
    checker.check_now()
    assert seen == ["0.2.0-beta.2"]


def test_check_update_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "fetch_update_list", lambda: [*RELEASES, rel("v9.0.0")])
    assert cli.main(["check-update"]) == 0
    assert "9.0.0" in capsys.readouterr().out
    monkeypatch.setattr(cli, "fetch_update_list", lambda: [])
    cli.main(["check-update"])
    assert "is the newest version" in capsys.readouterr().out


def test_updates_can_be_turned_off() -> None:
    assert parse_config({"audio": {"device": "x"}}).check_updates
    assert not parse_config({"audio": {"device": "x"}, "updates": {"check": False}}).check_updates


def test_window_and_tray_show_a_new_version(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.app import StatusWindow
    from tests.unit.test_stage9c import FakeController

    fake = FakeController()
    window = StatusWindow(fake, recordings=tmp_path, settings=tmp_path / "r.toml")
    qtbot.addWidget(window)
    assert window.update_banner.isHidden()
    fake.update = newest([rel("v9.0.0")], "0.1.0")  # type: ignore[attr-defined]
    window.refresh()
    assert not window.update_banner.isHidden()
    assert "9.0.0" in window.update_banner.text()


def test_controller_runs_the_update_checker(tmp_path: Path) -> None:
    from signal_archive_recorder.ui.controller import RecorderController
    from tests.unit.test_station import Rig

    rig = Rig(tmp_path)
    found = newest([rel("v9.0.0")], "0.1.0")
    started: list[bool] = []

    class FakeChecker:
        def __init__(self, on_new: Any) -> None:
            self.on_new = on_new

        def start(self) -> None:
            started.append(True)
            self.on_new(found)

        def stop(self) -> None:
            pass

    controller = RecorderController(rig.station.config, lambda c: rig.station, FakeChecker)  # type: ignore[arg-type]
    controller.start()
    try:
        assert started == [True] and controller.update == found
    finally:
        controller.stop()


# -- notices worded for where the operator is ----------------------------------------------


@pytest.mark.parametrize(
    ("current", "tag", "level", "words"),
    [
        ("0.3.0b1", "v0.3.0", "important", "please switch from the beta"),
        ("0.3.0rc2", "v0.3.0", "important", "betas aren't supported"),
        ("0.3.0b1", "v0.3.0-beta.2", "recommended", "newer beta"),
        ("0.3.0b2", "v0.3.0-rc.1", "recommended", "newer beta"),
        ("0.3.0", "v0.3.1", "recommended", "fixes"),
        ("0.3.0", "v0.4.0", "info", "new features"),
        ("0.3.0", "v1.0.0", "info", "new features"),
    ],
)
def test_update_notices(current: str, tag: str, level: str, words: str) -> None:
    from signal_archive_recorder.updates import notice

    release = newest([rel(tag)], current)
    assert release is not None
    n = notice(release, current)
    assert n.level == level and words in f"{n.title} {n.text}"


def test_moving_from_beta_to_release_mentions_the_real_archive() -> None:
    from signal_archive_recorder.updates import notice

    release = newest([rel("v0.3.0")], "0.3.0b3")
    assert release is not None
    assert "real archive" in notice(release, "0.3.0b3").text


def test_banner_colour_follows_the_level(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signal_archive_recorder import updates
    from signal_archive_recorder.ui import app
    from signal_archive_recorder.ui.app import NOTICE_COLOURS, StatusWindow
    from tests.unit.test_stage9c import FakeController

    monkeypatch.setattr(app, "notice", lambda r: updates.notice(r, "0.3.0b1"))
    fake = FakeController()
    window = StatusWindow(fake, recordings=tmp_path, settings=tmp_path / "r.toml")
    qtbot.addWidget(window)
    fake.update = newest([rel("v0.3.0")], "0.3.0b1")  # type: ignore[attr-defined]
    window.refresh()
    assert NOTICE_COLOURS["important"] in window.update_banner.styleSheet()
    assert "please switch from the beta" in window.update_banner.text()


# -- betas upload to the test dataset ---------------------------------------------------------


def test_betas_upload_to_the_test_dataset() -> None:
    from signal_archive_recorder.upload.hub import PRODUCTION_REPO, TEST_REPO, default_repo

    assert default_repo("0.3.0b1") == TEST_REPO
    assert default_repo("0.3.0rc1") == TEST_REPO
    assert default_repo("0.3.0") == PRODUCTION_REPO
    assert default_repo("1.2.3") == PRODUCTION_REPO


def test_setup_leaves_the_repo_to_the_version(tmp_path: Path) -> None:
    """Configs written by setup don't pin a repo, so upgrading to a release moves uploads
    from the test dataset to the real one by itself."""
    from signal_archive_recorder.firstrun.config_writer import SetupChoices, render

    text = render(SetupChoices(device="x"))
    assert not any(line.startswith("repo") for line in text.splitlines())
    (tmp_path / "r.toml").write_text(text)
    from signal_archive_recorder.upload.hub import DEFAULT_REPO

    assert load_config(tmp_path / "r.toml").upload.repo == DEFAULT_REPO


def test_beta_setup_says_where_recordings_go(tmp_path: Path) -> None:
    from signal_archive_recorder.upload.hub import TEST_REPO

    script = Script(["I agree", "1", "", "", ""], [TOKEN])
    wizard = make_wizard(tmp_path, script)
    wizard.repo_id = TEST_REPO
    wizard.run(tmp_path / "r.toml")
    assert "TEST dataset" in script.text


def test_a_beta_never_uploads_to_the_real_archive() -> None:
    from signal_archive_recorder.config import upload_repo
    from signal_archive_recorder.upload.hub import PRODUCTION_REPO, TEST_REPO

    assert upload_repo(PRODUCTION_REPO, "0.3.0b2") == TEST_REPO  # beta.1's setup wrote this
    assert upload_repo(PRODUCTION_REPO, "0.3.0") == PRODUCTION_REPO
    assert upload_repo("someone/their-own-test", "0.3.0b2") == "someone/their-own-test"
    assert upload_repo(TEST_REPO, "0.3.0") == TEST_REPO  # a release can still be pointed at test
