# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Start the recorder's tray app when the operator logs in. No Qt here.

- Windows: a value under HKEY_CURRENT_USER\\...\\CurrentVersion\\Run (no admin needed)
- Linux and other XDG desktops: ~/.config/autostart/signal-archive-recorder.desktop
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SignalArchiveRecorder"
DESKTOP_NAME = "signal-archive-recorder.desktop"


def launch_command() -> list[str]:
    """How to start the tray app from scratch, for this installation."""
    if getattr(sys, "frozen", False):  # a packaged app: the executable itself starts the tray
        return [sys.executable]
    script = shutil.which("signal-archive-recorder")
    if script:
        return [script, "tray"]
    python = sys.executable
    if platform.system() == "Windows":
        windowed = Path(python).with_name("pythonw.exe")
        python = str(windowed) if windowed.exists() else python
    return [python, "-m", "signal_archive_recorder.cli", "tray"]


def _autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart"


def desktop_entry(command: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Signal Archive Recorder\n"
        "Comment=Record receive audio for the Signal Archive Project\n"
        f"Exec={shlex.join(command)}\n"
        "Icon=signal-archive-recorder\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


class Autostart:
    def __init__(self, system: str | None = None, folder: Path | None = None) -> None:
        self.system = system or platform.system()
        self.folder = folder or _autostart_dir()

    @property
    def entry(self) -> Path:
        return self.folder / DESKTOP_NAME

    def enabled(self) -> bool:
        if self.system == "Windows":
            return _windows_get() is not None
        return self.entry.exists()

    def enable(self, command: list[str] | None = None) -> None:
        command = command or launch_command()
        if self.system == "Windows":
            _windows_set(subprocess.list2cmdline(command))
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        self.entry.write_text(desktop_entry(command), encoding="utf-8")

    def disable(self) -> None:
        if self.system == "Windows":
            _windows_delete()
            return
        self.entry.unlink(missing_ok=True)


if sys.platform == "win32":
    import winreg

    def _windows_get() -> str | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, VALUE_NAME)
                return str(value)
        except OSError:
            return None

    def _windows_set(command: str) -> None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)

    def _windows_delete() -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, VALUE_NAME)
        except OSError:
            pass

else:

    def _windows_get() -> str | None:
        raise OSError("the Windows registry exists only on Windows")

    def _windows_set(command: str) -> None:
        raise OSError("the Windows registry exists only on Windows")

    def _windows_delete() -> None:
        raise OSError("the Windows registry exists only on Windows")
