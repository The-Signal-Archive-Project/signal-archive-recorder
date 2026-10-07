# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The Windows installer agrees with the app (names, mutex, registry, commands).

The installer itself is built and smoke-tested on Windows in CI
(.github/workflows/windows-installer.yml); these checks run everywhere.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from signal_archive_recorder import cli
from signal_archive_recorder.ui import autostart
from signal_archive_recorder.ui.app import WINDOWS_MUTEX

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
ISS = (WIN / "signal-archive-recorder.iss").read_text()
SPEC = (ROOT / "installer" / "pyinstaller" / "signal-archive-recorder.spec").read_text()


def build_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build", WIN / "build.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_file_versions() -> None:
    build = build_module()
    assert build.numeric("0.2.0") == (0, 2, 0)
    assert build.numeric("0.3.0b1") == (0, 3, 0)
    assert build.numeric("1.10.3rc2") == (1, 10, 3)
    info = build.version_info("0.3.0b1")
    assert "filevers=(0, 3, 0, 0)" in info and "'ProductVersion', '0.3.0b1'" in info


def test_installer_and_app_agree() -> None:
    assert f"AppMutex={WINDOWS_MUTEX}" in ISS
    assert f'ValueName: "{autostart.VALUE_NAME}"' in ISS
    assert autostart.RUN_KEY in ISS
    # The exe names in the spec are the ones the installer starts and calls.
    assert (
        'name="SignalArchiveRecorder"' in SPEC
        and '#define AppExe "SignalArchiveRecorder.exe"' in ISS
    )
    assert 'name="signal-archive-recorder"' in SPEC
    assert '#define CliExe "signal-archive-recorder.exe"' in ISS
    # The registry value the installer writes is what the app's checkbox writes.
    assert 'ValueData: """{app}\\{#AppExe}"""' in ISS


def test_uninstaller_calls_real_forget_options() -> None:
    used = set(re.findall(r"--[a-z-]+", " ".join(re.findall(r"Params := .*", ISS))))
    assert used == {"--yes", "--everything", "--token"}
    forget = cli.build_parser().parse_args(["forget", *sorted(used)])
    assert forget.yes and forget.everything and forget.token


def test_uninstall_warns_before_removing_everything() -> None:
    assert "Warning: this permanently deletes every recording" in ISS
    assert "MB_DEFBUTTON2" in ISS  # the second confirmation defaults to No
    assert "TokenBox.Checked := True;" in ISS  # everything ticks the token box


def test_icon_is_shipped() -> None:
    from importlib.resources import files

    assert files("signal_archive_recorder.data").joinpath("icon.png").read_bytes()[:4] == b"\x89PNG"
    assert (WIN / "icon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
