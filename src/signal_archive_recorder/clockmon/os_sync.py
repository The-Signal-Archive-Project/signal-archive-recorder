# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""What the operating system says about its own clock synchronisation.

Read with the platform's own tool: timedatectl or chronyc on Linux, w32tm on
Windows, systemsetup on macOS. Only a yes/no and the tool's name are kept: the
tools' full output can name local time servers, which isn't published.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

Runner = Callable[[Sequence[str]], str | None]


@dataclass(frozen=True)
class OsSync:
    synchronized: bool | None  # None: couldn't tell
    tool: str | None


def run(cmd: Sequence[str]) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        done = subprocess.run(list(cmd), capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def parse_timedatectl(out: str) -> bool | None:
    m = re.search(r"^NTPSynchronized=(yes|no)$", out, re.MULTILINE)
    return None if m is None else m.group(1) == "yes"


def parse_chronyc(out: str) -> bool | None:
    m = re.search(r"^Leap status\s*:\s*(.+)$", out, re.MULTILINE)
    if m is None:
        return None
    return m.group(1).strip().lower() == "normal"


def parse_w32tm(out: str) -> bool | None:
    m = re.search(r"^Source:\s*(.+)$", out, re.MULTILINE)
    if m is None:
        return None
    source = m.group(1).strip().lower()
    return not (source.startswith("local cmos clock") or source.startswith("free-running"))


def parse_macos(out: str) -> bool | None:
    m = re.search(r"Network Time:\s*(On|Off)", out, re.IGNORECASE)
    return None if m is None else m.group(1).lower() == "on"


def read_os_sync(runner: Runner = run, system: str | None = None) -> OsSync:
    system = system or platform.system()
    attempts: list[tuple[str, list[str], Callable[[str], bool | None]]]
    if system == "Linux":
        attempts = [
            ("timedatectl", ["timedatectl", "show", "-p", "NTPSynchronized"], parse_timedatectl),
            ("chronyc", ["chronyc", "tracking"], parse_chronyc),
        ]
    elif system == "Windows":
        attempts = [("w32tm", ["w32tm", "/query", "/status"], parse_w32tm)]
    elif system == "Darwin":
        attempts = [("systemsetup", ["systemsetup", "-getusingnetworktime"], parse_macos)]
    else:
        attempts = []
    for tool, cmd, parse in attempts:
        out = runner(cmd)
        if out is not None and (result := parse(out)) is not None:
            return OsSync(result, tool)
    return OsSync(None, None)
