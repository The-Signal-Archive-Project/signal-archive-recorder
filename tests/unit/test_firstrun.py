# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""First-run setup: the wizard, device recommendations, and starting without arguments."""

import math
import socket
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from signal_archive_recorder import cli
from signal_archive_recorder.audio.device import DeviceInfo
from signal_archive_recorder.audio.file_backend import FileBackend
from signal_archive_recorder.clockmon.monitor import NtpAnswer
from signal_archive_recorder.config import load_config
from signal_archive_recorder.firstrun.devices import rank
from signal_archive_recorder.firstrun.wizard import (
    LevelReport,
    SetupCancelled,
    Wizard,
    listen_for_wsjtx,
    measure_levels,
)
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.upload.consent import ConsentStore
from signal_archive_recorder.upload.token import KeyringUnavailableError, Token, TokenStore
from tests.fakes.fake_audio import FakeBackend
from tests.fakes.fake_hf import FakeHub

TOKEN = "hf_FAKEtokenValue1234567890abcdef"
GOOD = LevelReport(peak_dbfs=-12.0, rms_dbfs=-30.0, clipped=0, seconds=3.0)
SILENT = LevelReport(peak_dbfs=-math.inf, rms_dbfs=-math.inf, clipped=0, seconds=3.0)


def dev(name: str, api: str, channels: int = 2, rate: int = 48_000) -> DeviceInfo:
    return DeviceInfo(0, name, api, channels, rate)


# -- recommendations ----------------------------------------------------------------


def names(devices: list[DeviceInfo], system: str) -> list[tuple[str, str, bool]]:
    return [(c.device.name, c.device.host_api, c.recommended) for c in rank(devices, system)]


def test_windows_prefers_radio_codec_over_wasapi_and_demotes_microphones() -> None:
    devices = [
        dev("Microphone Array (Realtek(R) Audio)", "Windows WASAPI"),
        dev("Microphone (USB Audio CODEC )", "MME"),
        dev("Stereo Mix (Realtek(R) Audio)", "Windows WASAPI"),
        dev("Microphone (USB Audio CODEC )", "Windows WASAPI"),
        dev("Line In (Realtek(R) Audio)", "Windows WASAPI"),
        dev("Microphone (USB Audio CODEC )", "Windows WDM-KS"),
    ]
    ranked = names(devices, "Windows")
    assert ranked[0] == ("Microphone (USB Audio CODEC )", "Windows WASAPI", True)
    assert ranked[1] == ("Microphone (USB Audio CODEC )", "MME", True)
    assert ("Line In (Realtek(R) Audio)", "Windows WASAPI", True) in ranked
    assert ranked[-2:] == [
        ("Microphone Array (Realtek(R) Audio)", "Windows WASAPI", False),
        ("Stereo Mix (Realtek(R) Audio)", "Windows WASAPI", False),
    ]
    wdm = next(c for c in rank(devices, "Windows") if "WDM-KS" in c.device.host_api)
    assert any("WSJT-X" in r for r in wdm.reasons)


def test_linux_prefers_the_sound_server_and_hides_alsa_plugins() -> None:
    devices = [  # what PortAudio reported on the development laptop, plus a radio
        dev("HDA Intel PCH: ALC236 Analog (hw:0,0)", "ALSA", rate=44_100),
        dev("USB Audio CODEC: - (hw:2,0)", "ALSA"),
        dev("sysdefault", "ALSA", 128), dev("lavrate", "ALSA", 128), dev("jack", "ALSA"),
        dev("pipewire", "ALSA", 128), dev("pulse", "ALSA", 32), dev("default", "ALSA", 128),
        dev("upmix", "ALSA", 8),
    ]  # fmt: skip
    ranked = names(devices, "Linux")
    assert [n for n, _, _ in ranked] == [
        "USB Audio CODEC: - (hw:2,0)",  # radio, but raw (warned)
        "default",
        "pipewire",
        "pulse",
        "HDA Intel PCH: ALC236 Analog (hw:0,0)",
    ]
    raw = rank(devices, "Linux")[0]
    assert any("lock the card" in r for r in raw.reasons)
    assert not any(n in ("sysdefault", "lavrate", "jack", "upmix") for n, _, _ in ranked)


def test_macos_demotes_built_in_mic_and_labels_virtual_inputs() -> None:
    devices = [
        dev("MacBook Pro Microphone", "Core Audio", 1),
        dev("USB Audio CODEC", "Core Audio"),
        dev("BlackHole 2ch", "Core Audio"),
    ]
    ranked = rank(devices, "Darwin")
    assert ranked[0].device.name == "USB Audio CODEC" and ranked[0].recommended
    assert ranked[-1].device.name == "MacBook Pro Microphone" and not ranked[-1].recommended
    assert any("virtual" in r for r in ranked[1].reasons)


# -- the wizard ---------------------------------------------------------------------


class Script:
    """Answers questions in order, and keeps everything the wizard said."""

    def __init__(self, answers: list[str], secrets: list[str] = ()) -> None:  # type: ignore[assignment]
        self.answers, self.secrets, self.out = list(answers), list(secrets), []  # type: ignore[var-annotated]

    def say(self, text: str = "") -> None:
        self.out.append(text)

    def ask(self, question: str, default: str = "") -> str:
        self.out.append(f"? {question}")
        answer = self.answers.pop(0)
        return answer or default

    def secret(self, question: str) -> str:
        self.out.append(f"? {question}")
        return self.secrets.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


class MemoryKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


DEVICES = [dev("Microphone Array (Realtek(R) Audio)", "Windows WASAPI"),
           dev("Microphone (USB Audio CODEC )", "Windows WASAPI")]  # fmt: skip


def make_wizard(tmp_path: Path, script: Script, *, levels: list[LevelReport] | None = None,
                keyring: Any = None, hub: FakeHub | None = None) -> Wizard:  # fmt: skip
    hub = hub or FakeHub()
    hub.add_token(TOKEN)
    hub.add_token("hf_readonly000000000000", role="read")
    reports = list(levels or [GOOD])
    return Wizard(
        prompt=script,
        backend_factory=lambda: FakeBackend(DEVICES),
        hub_factory=lambda: hub,
        tokens=TokenStore(keyring if keyring is not None else MemoryKeyring()),
        consent=ConsentStore(tmp_path / "config" / "consent.json"),
        now_ns=lambda: 1_791_000_000 * 10**9,
        ntp_probe=lambda server: NtpAnswer(offset_s=0.3, delay_s=0.02, stratum=2),
        wsjtx_probe=lambda: "WSJT-X 3.0.2",
        level_probe=lambda backend, device: reports.pop(0),
        system="Windows",
    )


def test_wizard_happy_path(tmp_path: Path) -> None:
    script = Script(
        ["I agree", "1", "w9xyz", "n", "en52WA", "6"],
        secrets=["hf_readonly000000000000", TOKEN],  # a read-only token first: try again
    )
    wizard = make_wizard(tmp_path, script)
    config_path = tmp_path / "config" / "recorder.toml"
    choices = wizard.run(config_path)

    config = load_config(config_path)  # what was written is valid and complete
    assert config.audio.device == "Microphone (USB Audio CODEC )"  # the recommended one
    assert (config.station.callsign, config.station.share_callsign) == ("W9XYZ", False)
    assert (config.station.grid, config.station.grid_precision) == ("EN52wa", 6)
    assert choices.device == config.audio.device
    assert wizard.tokens.get() == Token(TOKEN)
    assert wizard.consent.require().consent_version == "1"
    text = script.text
    assert "read-only" in text and "Logged in as volunteer" in text
    assert "Recommended:" in text and "Found WSJT-X 3.0.2" in text
    assert "0.300 s behind true time (yellow)" in text
    assert TOKEN not in text


def test_returning_user_skips_done_steps(tmp_path: Path) -> None:
    keyring = MemoryKeyring()
    first = make_wizard(tmp_path, Script(["I agree", "1", "", ""], [TOKEN]), keyring=keyring)
    first.run(tmp_path / "a.toml")
    script = Script(["2", "y", "", ""])  # no consent or token questions this time
    make_wizard(tmp_path, script, keyring=keyring, levels=[SILENT]).run(tmp_path / "b.toml")
    assert "already agreed" in script.text and "Already logged in" in script.text
    assert "silent" in script.text
    assert load_config(tmp_path / "b.toml").audio.device.startswith("Microphone Array")


def test_declining_terms_saves_nothing(tmp_path: Path) -> None:
    wizard = make_wizard(tmp_path, Script(["no"]))
    with pytest.raises(SetupCancelled, match="Not accepted"):
        wizard.run(tmp_path / "recorder.toml")
    assert not (tmp_path / "recorder.toml").exists()
    assert wizard.consent.load() is None


def test_no_token_saves_nothing(tmp_path: Path) -> None:
    wizard = make_wizard(tmp_path, Script(["I agree"], secrets=[""]))
    with pytest.raises(SetupCancelled, match="No token"):
        wizard.run(tmp_path / "recorder.toml")
    assert not (tmp_path / "recorder.toml").exists()
    assert wizard.tokens.get() is None


def test_missing_keyring_stops_setup_with_help(tmp_path: Path) -> None:
    class NoKeyring(MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise KeyringUnavailableError("No keyring service is running. Install gnome-keyring")

    wizard = make_wizard(tmp_path, Script(["I agree"]), keyring=NoKeyring())
    with pytest.raises(SetupCancelled, match="gnome-keyring"):
        wizard.run(tmp_path / "recorder.toml")


def test_bad_answers_are_asked_again(tmp_path: Path) -> None:
    script = Script(
        ["I agree", "9", "1", "not a call!", "k1abc", "y", "ZZ99", "fn42", "5"], [TOKEN]
    )
    make_wizard(tmp_path, script).run(tmp_path / "recorder.toml")
    config = load_config(tmp_path / "recorder.toml")
    assert (config.station.callsign, config.station.share_callsign) == ("K1ABC", True)
    assert (config.station.grid, config.station.grid_precision) == ("FN42", 4)  # 5 isn't valid
    assert "Please enter one of the numbers" in script.text


# -- probes -------------------------------------------------------------------------


def test_level_verdicts() -> None:
    assert GOOD.verdict == "good"
    assert "silent" in SILENT.verdict
    assert "clipping" in LevelReport(0.0, -3.0, 12, 3.0).verdict
    assert "hot" in LevelReport(-0.5, -6.0, 0, 3.0).verdict


def test_measure_levels_on_a_real_stream(tmp_path: Path) -> None:
    wav = tmp_path / "tone.wav"
    t = np.arange(48_000) / 48_000
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t) * 2**31).astype(np.int32)
    sf.write(wav, tone, 48_000, subtype="PCM_24")  # recordings open devices as 24-bit
    backend = FileBackend(wav)
    [device] = backend.input_devices()
    report = measure_levels(backend, device, seconds=0.4)
    assert report.peak_dbfs == pytest.approx(-6.0, abs=0.2)
    assert report.seconds > 0.2 and report.verdict == "good"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_wsjtx_probe_hears_a_heartbeat() -> None:
    port = free_port()

    def send() -> None:
        time.sleep(0.2)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(m.encode(m.Heartbeat("WSJT-X", 3, "3.0.2", "")), ("127.0.0.1", port))

    threading.Thread(target=send).start()
    assert listen_for_wsjtx(port, seconds=3) == "WSJT-X 3.0.2"
    assert listen_for_wsjtx(free_port(), seconds=0.3) is None


def test_wsjtx_probe_reports_a_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.bind(("127.0.0.1", 0))
        assert listen_for_wsjtx(holder.getsockname()[1], seconds=0.3) == "busy"


# -- starting without arguments ------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIGNAL_ARCHIVE_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config" / "recorder.toml"


def test_no_arguments_with_config_records(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["init", "--device", "X"]) == 0
    called = []
    monkeypatch.setattr(cli, "cmd_record", lambda args: called.append(args) or 0)
    assert cli.main([]) == 0
    assert called


def test_no_arguments_without_a_terminal_explains(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main([]) == cli.EXIT_CONFIG
    assert "in a terminal to set up" in capsys.readouterr().err
    assert not cli_env.exists()


def test_first_run_starts_the_wizard(tmp_path: Path, cli_env: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    script = Script(["I agree", "1", "", ""], [TOKEN])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(cli, "build_wizard", lambda: make_wizard(tmp_path, script))
    monkeypatch.setattr("builtins.input", lambda _: "n")  # don't start recording yet
    assert cli.main([]) == 0
    assert load_config(cli_env).audio.device == "Microphone (USB Audio CODEC )"


def test_setup_keeps_existing_settings_unless_asked(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.main(["init", "--device", "Original"])
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli.main(["setup"]) == 0
    assert load_config(cli_env).audio.device == "Original"
