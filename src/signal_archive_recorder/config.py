# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Recorder configuration, from a TOML file.

[storage]
root = "~/SignalArchive"

[audio]
device = "USB Audio CODEC"   # part of the device name (see --list-devices)
# sample_rate = 48000        # default: the device's own rate
sample_format = "int24"      # int16 or int24 (FLAC stores integers only)
# channels = 2
buffer_seconds = 10
# file = "reference.wav"     # instead of a device: play a WAV as if it were one
# file_speed = 1.0           # faster than real time for testing
# file_loop = false

[chunking]
target_seconds = 300

[station]
callsign = "W9XYZ"
share_callsign = false       # opt in to publish it (it is public, and helps attribution)
grid = "EN52wa"
grid_precision = 4           # 0 (withheld), 4, 6 or 8
hf_username = "your-hf-name"

[wsjtx]
enabled = true
port = 2237
bind = "127.0.0.1"
# group = "224.0.0.1"        # multicast, to share WSJT-X with GridTracker/JTAlert

[clock]
enabled = true               # NTP check at start and every interval_s
interval_s = 600
# servers = ["pool.ntp.org", "time.cloudflare.com", "time.google.com"]

[upload]
repo = "signal-archive-project/signal-archive-intake"
schedule = "manual"          # manual, while_recording or overnight
overnight_window = "01:00-06:00"   # local time, for schedule = "overnight"
max_mbps = 0                 # average upload cap in megabits/s; 0 = no cap

(and under [storage]: max_gb = 0 and delete_after_days = 0 limit the archive by
deleting only sessions whose upload is confirmed; 0 = off)
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from signal_archive_recorder.audio.format import SampleFormat
from signal_archive_recorder.clockmon.monitor import DEFAULT_SERVERS
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.upload.service import Window


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AudioConfig:
    device: str = ""
    sample_rate: int | None = None
    sample_format: SampleFormat = "int24"
    channels: int | None = None
    buffer_seconds: float = 10.0
    file: Path | None = None
    file_speed: float = 1.0
    file_loop: bool = False


@dataclass(frozen=True)
class WsjtxConfig:
    enabled: bool = True
    port: int = 2237
    bind: str = "127.0.0.1"
    group: str | None = None


@dataclass(frozen=True)
class ClockConfig:
    enabled: bool = True
    interval_s: float = 600.0
    servers: tuple[str, ...] = DEFAULT_SERVERS


@dataclass(frozen=True)
class UploadConfig:
    repo: str = "signal-archive-project/signal-archive-intake"
    require_decoder: bool = True
    schedule: str = "manual"
    overnight_window: str = "01:00-06:00"
    max_mbps: float = 0.0

    @property
    def max_bytes_per_s(self) -> float | None:
        return self.max_mbps * 1e6 / 8 if self.max_mbps else None


START_MODES = ("with_decoder", "always")


@dataclass(frozen=True)
class RecorderConfig:
    storage_root: Path = field(default_factory=lambda: Path("~/SignalArchive").expanduser())
    max_gb: float = 0.0  # archive size limit (confirmed uploads only are deleted); 0 = off
    delete_after_days: float = 0.0  # delete confirmed uploads this long after; 0 = never
    audio: AudioConfig = field(default_factory=AudioConfig)
    target_chunk_s: int = 300
    station: StationSettings = field(default_factory=StationSettings)
    wsjtx: WsjtxConfig = field(default_factory=WsjtxConfig)
    clock: ClockConfig = field(default_factory=ClockConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    # with_decoder: stand by, and record only while a decoder (WSJT-X) is running.
    # always: record from start to stop (WAV playback, decoder-less modes).
    start: str = "with_decoder"


_SECTIONS = {
    "storage": {"root", "max_gb", "delete_after_days"},
    "audio": {"device", "sample_rate", "sample_format", "channels", "buffer_seconds", "file",
              "file_speed", "file_loop"},
    "chunking": {"target_seconds"},
    "station": {"callsign", "share_callsign", "grid", "grid_precision", "hf_username",
                "station_profile_id"},
    "wsjtx": {"enabled", "port", "bind", "group"},
    "clock": {"enabled", "interval_s", "servers"},
    "upload": {"repo", "require_decoder", "schedule", "overnight_window", "max_mbps"},
    "recording": {"start"},
}  # fmt: skip


def _check_keys(data: dict[str, Any]) -> None:
    for section, values in data.items():
        if section not in _SECTIONS:
            raise ConfigError(f"unknown section [{section}]")
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        unknown = set(values) - _SECTIONS[section]
        if unknown:
            raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")


def parse_config(data: dict[str, Any], base: Path = Path()) -> RecorderConfig:
    """Build a config from parsed TOML; relative paths are relative to `base`."""
    _check_keys(data)

    def path(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else base / p

    audio = data.get("audio", {})
    if audio.get("sample_format", "int24") not in ("int16", "int24"):
        raise ConfigError("[audio] sample_format must be int16 or int24")
    if not audio.get("device") and not audio.get("file"):
        raise ConfigError("[audio] needs a device (see --list-devices) or a file")
    station = data.get("station", {})
    wsjtx = data.get("wsjtx", {})
    clock = data.get("clock", {})
    upload = data.get("upload", {})
    storage = data.get("storage", {})
    if upload.get("schedule", "manual") not in ("manual", "while_recording", "overnight"):
        raise ConfigError("[upload] schedule must be manual, while_recording or overnight")
    try:
        Window.parse(upload.get("overnight_window", "01:00-06:00"))
    except ValueError as exc:
        raise ConfigError(f"[upload] overnight_window: {exc}") from None
    for section, key in (("upload", "max_mbps"), ("storage", "max_gb"),
                         ("storage", "delete_after_days")):  # fmt: skip
        if float(data.get(section, {}).get(key, 0)) < 0:
            raise ConfigError(f"[{section}] {key} can't be negative")
    start = data.get("recording", {}).get("start", "with_decoder")
    if start not in START_MODES:
        raise ConfigError('[recording] start must be "with_decoder" or "always"')
    if start == "with_decoder" and not wsjtx.get("enabled", True):
        raise ConfigError(
            '[recording] start = "with_decoder" waits for WSJT-X, but [wsjtx] is disabled. '
            'Enable it, or set start = "always".'
        )
    if float(clock.get("interval_s", 600)) < 60:
        raise ConfigError("[clock] interval_s must be at least 60 (be kind to NTP servers)")
    try:
        return RecorderConfig(
            storage_root=path(storage.get("root", "~/SignalArchive")),
            max_gb=float(storage.get("max_gb", 0)),
            delete_after_days=float(storage.get("delete_after_days", 0)),
            audio=AudioConfig(
                device=audio.get("device", ""),
                sample_rate=audio.get("sample_rate"),
                sample_format=audio.get("sample_format", "int24"),
                channels=audio.get("channels"),
                buffer_seconds=float(audio.get("buffer_seconds", 10.0)),
                file=path(audio["file"]) if audio.get("file") else None,
                file_speed=float(audio.get("file_speed", 1.0)),
                file_loop=bool(audio.get("file_loop", False)),
            ),
            target_chunk_s=int(data.get("chunking", {}).get("target_seconds", 300)),
            station=StationSettings(**station),
            wsjtx=WsjtxConfig(
                enabled=bool(wsjtx.get("enabled", True)),
                port=int(wsjtx.get("port", 2237)),
                bind=wsjtx.get("bind", "127.0.0.1"),
                group=wsjtx.get("group") or None,
            ),
            upload=UploadConfig(
                repo=upload.get("repo", UploadConfig.repo),
                require_decoder=bool(upload.get("require_decoder", True)),
                schedule=upload.get("schedule", "manual"),
                overnight_window=upload.get("overnight_window", "01:00-06:00"),
                max_mbps=float(upload.get("max_mbps", 0)),
            ),
            clock=ClockConfig(
                enabled=bool(clock.get("enabled", True)),
                interval_s=float(clock.get("interval_s", 600)),
                servers=tuple(clock.get("servers", DEFAULT_SERVERS)),
            ),
            start=start,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: Path) -> RecorderConfig:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    return parse_config(data, base=path.parent)
