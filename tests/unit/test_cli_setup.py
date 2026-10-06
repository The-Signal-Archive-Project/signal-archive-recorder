# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""First-run setup for someone who just installed the package."""

from pathlib import Path

import pytest

from signal_archive_recorder import cli
from signal_archive_recorder.audio.device import DeviceUnavailableError, SoundDeviceBackend
from signal_archive_recorder.config import load_config
from signal_archive_recorder.paths import default_config_file, example_config
from signal_archive_recorder.upload.token import KeyringUnavailableError, _SystemKeyring


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    monkeypatch.setenv("SIGNAL_ARCHIVE_CONFIG_DIR", str(path))
    return path


def test_example_config_ships_with_the_package() -> None:
    text = example_config()
    assert "[audio]" in text and "[station]" in text and "[upload]" in text
    assert "signal-archive-recorder record" in text


def test_init_writes_default_config(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init", "--device", 'My "Radio" Codec']) == 0
    path = default_config_file()
    assert path == config_dir / "recorder.toml" and path.exists()
    config = load_config(path)
    assert config.audio.device == 'My "Radio" Codec'
    assert config.station.share_callsign is False
    assert "Wrote" in capsys.readouterr().out


def test_init_refuses_to_overwrite(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init", "--device", "A"]) == 0
    assert cli.main(["init", "--device", "B"]) == cli.EXIT_CONFIG
    assert "already exists" in capsys.readouterr().err
    assert load_config(default_config_file()).audio.device == "A"
    assert cli.main(["init", "--device", "B", "--force"]) == 0
    assert load_config(default_config_file()).audio.device == "B"


def test_init_to_a_chosen_path(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "station.toml"
    assert cli.main(["init", "--config", str(target), "--device", "X"]) == 0
    assert load_config(target).audio.device == "X"


def test_commands_find_the_default_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["review"]) == cli.EXIT_CONFIG  # nothing yet: a helpful hint
    assert "signal-archive-recorder init" in capsys.readouterr().err
    cli.main(["init", "--device", "X"])
    assert cli.main(["review"]) == 0  # no --config needed once init has run


def test_missing_portaudio_explains_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delitem(__import__("sys").modules, "sounddevice", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)  # as if libportaudio were missing
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(DeviceUnavailableError, match="libportaudio2"):
        SoundDeviceBackend()


def test_missing_keyring_explains_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    import keyring.errors

    def no_backend(*args: object) -> None:
        raise keyring.errors.NoKeyringError("No recommended backend was available")

    monkeypatch.setattr(keyring, "set_password", no_backend)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(KeyringUnavailableError, match="gnome-keyring"):
        _SystemKeyring().set_password("s", "u", "p")
