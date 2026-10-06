# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The real sample rate behind Linux sound-server devices.

On Linux, PortAudio reaches PipeWire or PulseAudio through ALSA plugin devices
("pipewire", "pulse", "default") whose reported default rate doesn't match the
server's (for example 44100 Hz while PipeWire runs at 48000 Hz). Recording at the
wrong rate makes the server resample, so the recording is no longer bit-exact.
This asks the server itself: PipeWire's graph clock rate, or PulseAudio's default
sample specification.
"""

from __future__ import annotations

import platform
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from signal_archive_recorder.audio.device import DeviceInfo
from signal_archive_recorder.clockmon.os_sync import run

SERVER_DEVICES = ("pipewire", "pulse", "default")
Runner = Callable[[Sequence[str]], str | None]


@dataclass(frozen=True)
class ServerRate:
    rate: int
    server: str  # "pipewire" or "pulseaudio"


def parse_pw_metadata(out: str) -> int | None:
    m = re.search(r"key:'clock\.force-rate' value:'(\d+)'", out)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    m = re.search(r"key:'clock\.rate' value:'(\d+)'", out)
    return int(m.group(1)) if m else None


def parse_pactl_info(out: str) -> tuple[int | None, str]:
    rate = re.search(r"^Default Sample Specification:.*?(\d+)Hz", out, re.MULTILINE)
    server = "pipewire" if "PipeWire" in out else "pulseaudio"
    return (int(rate.group(1)) if rate else None), server


def server_rate(runner: Runner = run) -> ServerRate | None:
    out = runner(["pw-metadata", "-n", "settings", "0"])
    if out and (rate := parse_pw_metadata(out)):
        return ServerRate(rate, "pipewire")
    out = runner(["pactl", "info"])
    if out:
        rate, server = parse_pactl_info(out)
        if rate:
            return ServerRate(rate, server)
    return None


def is_server_device(device: DeviceInfo, system: str | None = None) -> bool:
    system = system or platform.system()
    return system == "Linux" and device.name.strip().lower() in SERVER_DEVICES


def native_rate(device: DeviceInfo, runner: Runner = run, system: str | None = None) -> int:
    """The rate to record at so nothing resamples: the server's for server devices."""
    if is_server_device(device, system):
        found = server_rate(runner)
        if found:
            return found.rate
    return device.default_sample_rate
