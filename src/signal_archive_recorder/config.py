# SPDX-License-Identifier: Apache-2.0
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
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from signal_archive_recorder.audio.format import SampleFormat
from signal_archive_recorder.metadata.settings import StationSettings


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
class RecorderConfig:
    storage_root: Path = field(default_factory=lambda: Path("~/SignalArchive").expanduser())
    audio: AudioConfig = field(default_factory=AudioConfig)
    target_chunk_s: int = 300
    station: StationSettings = field(default_factory=StationSettings)
    wsjtx: WsjtxConfig = field(default_factory=WsjtxConfig)


_SECTIONS = {
    "storage": {"root"},
    "audio": {"device", "sample_rate", "sample_format", "channels", "buffer_seconds", "file",
              "file_speed", "file_loop"},
    "chunking": {"target_seconds"},
    "station": {"callsign", "share_callsign", "grid", "grid_precision", "hf_username",
                "station_profile_id"},
    "wsjtx": {"enabled", "port", "bind", "group"},
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
    try:
        return RecorderConfig(
            storage_root=path(data.get("storage", {}).get("root", "~/SignalArchive")),
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
