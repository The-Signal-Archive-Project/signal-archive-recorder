# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""A diagnostics bundle that testers can attach to a bug report.

It holds what's needed to understand a problem on someone else's station: versions,
the OS, the audio inputs, the settings, recent logs and the state of each session.
It never holds audio, decoded messages or the token, and the computer's name, the
user name, home folder and the operator's callsign and grid are replaced with
<redacted>. The tester can open the zip and check before sending it anywhere.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

from signal_archive_recorder import __version__
from signal_archive_recorder.applog import _TOKEN, LOG_FILE
from signal_archive_recorder.metadata.privacy import Scrubber

README = """\
Signal Archive Recorder diagnostics

This zip helps the developers understand a problem on your station. It contains
no audio, no decoded messages and no login token. Your computer's name, your user
name, your home folder, callsign and grid have been replaced with <redacted>.
Open the files to check before attaching the zip to a bug report.
"""
_STATION_LINE = re.compile(r'^(\s*(?:callsign|grid)\s*=\s*)"[^"]*"', re.MULTILINE)


def _station_values(config_text: str) -> list[str]:
    return re.findall(r'^\s*(?:callsign|grid)\s*=\s*"([^"]+)"', config_text, re.MULTILINE)


def about() -> str:
    lines = [
        f"signal-archive-recorder {__version__}",
        f"python {sys.version.split()[0]} ({platform.python_implementation()})",
        f"os {platform.system()} {platform.release()} ({platform.machine()})",
        f"packaged {getattr(sys, 'frozen', False)}",
    ]
    try:
        import PySide6

        lines.append(f"pyside6 {PySide6.__version__}")
    except ImportError:
        lines.append("pyside6 not installed")
    try:
        import keyring

        lines.append(f"keyring backend {type(keyring.get_keyring()).__name__}")
    except Exception as exc:
        lines.append(f"keyring unavailable: {type(exc).__name__}: {exc}")
    for name in ("sounddevice", "soundfile", "numpy", "huggingface_hub"):
        try:
            module = __import__(name)
            lines.append(f"{name} {getattr(module, '__version__', '?')}")
        except Exception as exc:  # a broken audio library is itself a useful finding
            lines.append(f"{name} unavailable: {type(exc).__name__}: {exc}")
    return "\n".join(lines) + "\n"


def session_overview(sessions: Path) -> list[dict[str, object]]:
    """Each session's shape and upload state, without any recorded content."""
    rows: list[dict[str, object]] = []
    if not sessions.is_dir():
        return rows
    for folder in sorted(p for p in sessions.iterdir() if p.is_dir()):
        row: dict[str, object] = {"session": folder.name}
        recordings = folder / "recordings"
        if recordings.is_dir():
            names = [p.name for p in recordings.iterdir()]
            row["flac"] = sum(n.endswith(".flac") for n in names)
            row["other_files"] = sorted(
                {n.split(".", 1)[-1] for n in names if not n.endswith((".flac", ".meta.json"))}
            )
        for name, keys in (
            ("session.json", ("end_reason", "ended_utc", "started_utc")),
            ("local/upload.json", ("state", "last_error", "attempts")),
        ):
            try:
                data = json.loads((folder / name).read_text(encoding="utf-8"))
                row.update({k: data.get(k) for k in keys if k in data})
            except (OSError, ValueError):
                pass
        rows.append(row)
    return rows


def build(
    out: Path,
    *,
    config_path: Path | None,
    logs: Path,
    sessions: Path | None,
    devices: Callable[[], str] | None = None,
) -> Path:
    """Write the diagnostics zip to `out` and return it."""
    config_text = ""
    if config_path is not None and config_path.exists():
        config_text = config_path.read_text(encoding="utf-8", errors="replace")
    scrub = Scrubber(_station_values(config_text))

    def clean(text: str) -> str:
        return _TOKEN.sub("hf_****", scrub.text(text))

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        z.writestr("about.txt", clean(about()))
        if config_text:
            z.writestr("recorder.toml", clean(_STATION_LINE.sub(r'\1"<redacted>"', config_text)))
        else:
            z.writestr("recorder.toml", "# no configuration yet\n")
        if devices is not None:
            try:
                listing = devices()
            except Exception as exc:
                listing = f"listing audio inputs failed: {type(exc).__name__}: {exc}"
            z.writestr("audio-inputs.txt", clean(listing))
        if sessions is not None:
            overview = json.dumps(session_overview(sessions), indent=2)
            z.writestr("sessions.json", clean(overview))
        if logs.is_dir():
            for log in sorted(logs.glob(LOG_FILE + "*")):
                text = log.read_text(encoding="utf-8", errors="replace")
                z.writestr(f"logs/{log.name}", clean(text))
    return out
