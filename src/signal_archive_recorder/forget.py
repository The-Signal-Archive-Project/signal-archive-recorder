# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""What uninstalling removes: `signal-archive-recorder forget`.

- Always: start-at-login (the program is going away).
- `token`: the Hugging Face token in the OS keyring.
- `everything`: also all recordings (including any not uploaded yet), settings,
  consent and logs. Implies `token`.

Recordings are only ever deleted from the archive's `sessions` folder; the archive
folder itself goes only if nothing else is left in it, so a storage root pointed at
a folder with other files in it never loses them.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from signal_archive_recorder.config import ConfigError, load_config
from signal_archive_recorder.paths import config_dir, default_config_file, log_dir
from signal_archive_recorder.session.cleanup import folder_size
from signal_archive_recorder.ui.autostart import Autostart
from signal_archive_recorder.upload.queue import UploadRecord, UploadState
from signal_archive_recorder.upload.token import TokenStore

log = logging.getLogger(__name__)

DEFAULT_ROOT = Path("~/SignalArchive")


@dataclass
class ForgetPlan:
    token: bool
    everything: bool
    storage_root: Path
    config_dir: Path
    log_dir: Path
    sessions: int = 0
    not_uploaded: int = 0  # never sent, or sent but not yet confirmed
    size_bytes: int = 0
    removed: list[str] = field(default_factory=list)

    @property
    def sessions_dir(self) -> Path:
        return self.storage_root / "sessions"


def plan(config_path: Path | None, *, token: bool, everything: bool) -> ForgetPlan:
    path = config_path or default_config_file()
    root = DEFAULT_ROOT.expanduser()
    with contextlib.suppress(ConfigError, OSError):
        root = load_config(path).storage_root  # a custom recordings folder
    p = ForgetPlan(
        token or everything,
        everything,
        root,
        config_dir(),  # the app's own settings folder only, never a --config file's folder
        log_dir(),
    )
    if p.sessions_dir.is_dir():
        for folder in p.sessions_dir.iterdir():
            if not folder.is_dir():
                continue
            p.sessions += 1
            p.size_bytes += folder_size(folder)
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                if UploadRecord.load(folder).state is not UploadState.VALIDATED:
                    p.not_uploaded += 1
    return p


def describe(p: ForgetPlan) -> str:
    lines = ["This will:", "  - stop Signal Archive Recorder starting when you log in"]
    if p.token:
        lines.append("  - remove your Hugging Face token from this computer")
    if p.everything:
        lines += [
            f"  - PERMANENTLY DELETE {p.sessions} recorded session(s), "
            f"{p.size_bytes / 1e9:.2f} GB, in {p.sessions_dir}",
            f"    {p.not_uploaded} of them are not uploaded (or not yet confirmed) and "
            "will be lost for good"
            if p.not_uploaded
            else "    all of them were uploaded",
            f"  - delete your settings and consent in {p.config_dir}",
            f"  - delete the logs in {p.log_dir}",
        ]
    else:
        lines.append(f"Your recordings ({p.sessions_dir}) and settings are kept.")
    return "\n".join(lines)


def _safe_to_delete(path: Path) -> bool:
    """Never a home folder, a drive or filesystem root, or anything outside a folder."""
    resolved = path.resolve()
    return resolved != Path.home().resolve() and len(resolved.parts) > 2


def apply(p: ForgetPlan, *, tokens: TokenStore, autostart: Autostart) -> list[str]:
    try:
        autostart.disable()
        p.removed.append("start at login")
    except OSError as exc:
        log.warning("couldn't remove start-at-login: %s", exc)
    if p.token:
        tokens.delete()
        p.removed.append("Hugging Face token")
    if not p.everything:
        return p.removed
    from signal_archive_recorder.applog import stop_file_logging

    stop_file_logging()  # Windows can't delete an open log file
    if p.sessions_dir.is_dir() and _safe_to_delete(p.sessions_dir):
        shutil.rmtree(p.sessions_dir)
        p.removed.append(str(p.sessions_dir))
        with contextlib.suppress(OSError):
            p.storage_root.rmdir()  # only if empty: other files there are never touched
    for folder in (p.config_dir, p.log_dir):
        if folder.is_dir() and _safe_to_delete(folder):
            shutil.rmtree(folder, ignore_errors=True)
            p.removed.append(str(folder))
    return p.removed
