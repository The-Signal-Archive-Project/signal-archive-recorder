# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Write a recorder.toml from the choices made during setup, with explanations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SetupChoices:
    device: str
    sample_rate: int | None = None
    keep_channel: str = "both"
    callsign: str | None = None
    share_callsign: bool = False
    grid: str | None = None
    grid_precision: int = 4
    storage_root: str = "~/SignalArchive"
    wsjtx_port: int = 2237
    wsjtx_group: str | None = None


def _q(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def render(c: SetupChoices) -> str:
    def opt(key: str, value: str | None, comment: str) -> str:
        return f"{key} = {value}  # {comment}" if value is not None else f"# {key} =   # {comment}"

    return f"""\
# Signal Archive Recorder configuration, written by first-run setup.
# Edit freely, or run `signal-archive-recorder setup` again.

[storage]
root = {_q(c.storage_root)}   # sessions are saved here
max_gb = 0                   # archive size limit; only confirmed uploads are deleted; 0 = off
delete_after_days = 0        # delete confirmed uploads this many days later; 0 = never

[audio]
device = {_q(c.device)}   # see: signal-archive-recorder devices
{opt("sample_rate", str(c.sample_rate) if c.sample_rate else None,
     "default: the device's own rate (avoids OS resampling)")}
sample_format = "int24"      # int16 or int24; 16-bit sound cards fit in int24 losslessly
keep_channel = {_q(c.keep_channel)}   # stereo: both, or left/right if the other is a copy
buffer_seconds = 10

[recording]
start = "with_decoder"       # record only while WSJT-X is running; "always" = from start to stop

[chunking]
target_seconds = 300

[station]
{opt("callsign", _q(c.callsign) if c.callsign else None, "your callsign")}
share_callsign = {"true" if c.share_callsign else "false"}   # true: published with recordings
{opt("grid", _q(c.grid) if c.grid else None, "your Maidenhead locator")}
grid_precision = {c.grid_precision}   # characters shared: 0 (withheld), 4, 6 or 8

[wsjtx]
enabled = true
port = {c.wsjtx_port}
bind = "127.0.0.1"
{opt("group", _q(c.wsjtx_group) if c.wsjtx_group else None,
     "multicast, to share WSJT-X with GridTracker or JTAlert")}

[clock]
enabled = true               # checks the computer clock against NTP every 10 minutes
interval_s = 600

[upload]
repo = "signal-archive-project/signal-archive-intake"
schedule = "manual"          # manual, while_recording or overnight (uploads in the background)
overnight_window = "01:00-06:00"   # local time, for schedule = "overnight"
max_mbps = 0                 # average upload cap in megabits/s; 0 = no cap

[updates]
check = true                 # daily: is a newer version out? (asks GitHub; never installs)
"""  # fmt: skip


def write(path: Path, choices: SetupChoices) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(choices), encoding="utf-8")
    tmp.replace(path)
