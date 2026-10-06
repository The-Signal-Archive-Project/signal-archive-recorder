# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Where the recorder keeps its own settings (not recordings)."""

from __future__ import annotations

import os
import platform
from pathlib import Path

APP = "signal-archive-recorder"


def config_dir() -> Path:
    """Per-user settings folder; SIGNAL_ARCHIVE_CONFIG_DIR overrides it."""
    if override := os.environ.get("SIGNAL_ARCHIVE_CONFIG_DIR"):
        return Path(override)
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP


def default_config_file() -> Path:
    return config_dir() / "recorder.toml"


def example_config() -> str:
    """The starter configuration shipped with the package."""
    from importlib.resources import files

    return (
        files("signal_archive_recorder.data").joinpath("recorder.example.toml").read_text("utf-8")
    )
