# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""What the tray colour and the status checklist say, as plain functions.

Tray colours:
- blue: ready, waiting for WSJT-X (the sound card is closed; nothing is recorded)
- green: recording, everything healthy
- yellow: something worth a look (WSJT-X stopped sending, clipping, clock off...)
- red: a problem that stops recording (no sound card, disk almost full, port taken)
- grey: paused by the operator
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Level = Literal["ok", "warn", "bad", "off"]
TrayState = Literal["blue", "green", "yellow", "red", "grey"]
WsjtxState = Literal["up", "down", "never", "disabled", "busy"]
DiskState = Literal["ok", "low", "critical"]
SILENT_DBFS = -70.0


@dataclass
class HealthInputs:
    recording: bool = False
    standby: bool = False  # waiting for WSJT-X, ready to record
    paused: bool = False
    problem: str | None = None  # why recording couldn't start (e.g. the sound card)
    audio_note: str | None = None  # e.g. a dropped stereo channel came to life
    peak_dbfs: float | None = None  # None: no audio yet
    clipped: int = 0
    frames_lost: int = 0
    wsjtx: WsjtxState = "never"
    mode: str | None = None
    dial_hz: int | None = None
    clock: str | None = None  # green, yellow, red, unknown; None before the first check
    clock_offset_s: float | None = None
    disk: DiskState = "ok"
    uploads: dict[str, int] = field(default_factory=dict)  # upload state -> sessions
    session: str | None = None


@dataclass(frozen=True)
class CheckItem:
    name: str
    level: Level
    detail: str


def checklist(h: HealthInputs) -> list[CheckItem]:
    items = []
    if h.paused:
        items.append(CheckItem("Recording", "off", "paused"))
    elif h.recording:
        items.append(CheckItem("Recording", "ok", f"session {h.session}" if h.session else "on"))
    elif h.problem:
        items.append(CheckItem("Recording", "bad", h.problem))
    elif h.standby:
        items.append(CheckItem("Recording", "off", "ready: starts when WSJT-X is running"))
    else:
        items.append(CheckItem("Recording", "bad", "not recording"))

    if not h.recording:
        items.append(CheckItem("Audio", "off", "-"))
    elif h.peak_dbfs is None or h.peak_dbfs < SILENT_DBFS:
        items.append(
            CheckItem("Audio", "warn", "silent: is the radio's audio reaching this input?")
        )
    elif h.clipped:
        items.append(CheckItem("Audio", "warn", "clipping: turn the radio's audio level down"))
    elif h.audio_note:
        items.append(CheckItem("Audio", "warn", h.audio_note))
    elif h.frames_lost:
        items.append(CheckItem("Audio", "warn", f"{h.frames_lost} samples lost (disk too slow?)"))
    else:
        items.append(CheckItem("Audio", "ok", f"peak {h.peak_dbfs:.0f} dBFS"))

    if h.wsjtx == "disabled":
        items.append(CheckItem("WSJT-X", "off", "not used"))
    elif h.wsjtx == "busy":
        level: Level = "bad" if h.standby else "warn"
        items.append(
            CheckItem(
                "WSJT-X",
                level,
                "another program has its UDP port (GridTracker "
                "or JTAlert?): use multicast to share it",
            )
        )
    elif h.wsjtx != "up" and h.standby:
        items.append(CheckItem("WSJT-X", "off", "not running"))
    elif h.wsjtx == "up":
        where = " ".join(x for x in (h.mode, _mhz(h.dial_hz)) if x)
        items.append(CheckItem("WSJT-X", "ok", where or "connected"))
    else:
        detail = "stopped sending" if h.wsjtx == "down" else "not heard yet"
        items.append(CheckItem("WSJT-X", "warn", f"{detail}: recordings without it are kept back"))

    if h.clock in (None, "unknown"):
        items.append(CheckItem("Clock", "warn" if h.clock else "off", "not checked yet"))
    else:
        offset = f"{h.clock_offset_s:+.3f} s" if h.clock_offset_s is not None else ""
        items.append(CheckItem("Clock", "ok" if h.clock == "green" else "warn", offset or h.clock))

    if h.disk == "critical":
        items.append(CheckItem("Disk", "bad", "almost full: recording will fail soon"))
    elif h.disk == "low":
        items.append(CheckItem("Disk", "warn", "getting low"))
    else:
        items.append(CheckItem("Disk", "ok", "space available"))

    stuck = sum(h.uploads.get(s, 0) for s in ("blocked", "failed", "pr_missing"))
    waiting = h.uploads.get("queued", 0)
    open_prs = h.uploads.get("pr_opened", 0)
    parts = [
        f"{n} {label}"
        for n, label in ((waiting, "to upload"), (open_prs, "in review"), (stuck, "need attention"))
        if n
    ]
    items.append(CheckItem("Uploads", "warn" if stuck else "ok", ", ".join(parts) or "up to date"))
    return items


def tray_state(h: HealthInputs) -> TrayState:
    if h.paused:
        return "grey"
    items = checklist(h)
    if any(i.level == "bad" for i in items):
        return "red"
    if any(i.level == "warn" for i in items):
        return "yellow"
    return "blue" if h.standby and not h.recording else "green"


def _mhz(dial_hz: int | None) -> str | None:
    return f"{dial_hz / 1e6:.3f} MHz" if dial_hz else None
