# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Normalised events that sources publish onto the session bus.

Sources translate whatever their program reports into these types and keep the
original fields in `raw`. Nothing downstream of the bus reads source-specific data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, kw_only=True)
class Event:
    source: str
    raw: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True, kw_only=True)
class SourceUp(Event):
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class SourceDown(Event):
    reason: str


@dataclass(frozen=True, kw_only=True)
class FreqChanged(Event):
    dial_hz: int


@dataclass(frozen=True, kw_only=True)
class ModeChanged(Event):
    """Published only by decoder sources; mode_id is a registry id and raw_mode the
    string the decoder reported. Rig-control sources publish their operating mode
    (PKTUSB, CW, ...) as SettingChanged(name="rig_mode") instead."""

    mode_id: str
    raw_mode: str
    needs_mapping: bool = False
    period_s: float | None = None
    # Mode-specific values, checked against the mode's params_schema from modes.json.
    params: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True, kw_only=True)
class TxStarted(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class TxEnded(Event):
    pass


@dataclass(frozen=True, kw_only=True)
class Decode(Event):
    """One decoded message. Fields a source doesn't report stay None."""

    mode_id: str
    text: str
    decoder_time_ns: int | None = None
    snr_db: float | None = None
    dt_s: float | None = None
    df_hz: float | None = None
    low_confidence: bool | None = None
    # True when the decoder was playing back a recording, not listening to the
    # audio being captured. Such decodes are logged but never counted in chunk stats.
    off_air: bool = False


@dataclass(frozen=True, kw_only=True)
class SettingChanged(Event):
    """A receiver or software setting such as filter width, AGC or noise blanker."""

    name: str
    value: str | int | float | bool | None


@dataclass(frozen=True, kw_only=True)
class AudioGap(Event):
    """Audio frames that never reached disk.

    stream_frame is the index (in frames delivered by the device) of the first lost
    frame; est_t_ns is its estimated UTC time. lost_frames is None when the driver
    reported an overflow without saying how much was lost.
    """

    stream_frame: int
    lost_frames: int | None
    est_t_ns: int
    reason: str


@dataclass(frozen=True, kw_only=True)
class CaptureWarning(Event):
    """Something the operator should know about; recording continues."""

    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class ClockChecked(Event):
    """One measurement of the computer clock against an NTP server.

    offset_s is NTP time minus computer time (None if no server answered), with
    delay_s the round trip that bounds its uncertainty. measured_ns is the computer
    clock when the answer arrived. Together with the audio sync points this links
    every sample to true UTC; the recorder records it and never corrects anything.
    """

    measured_ns: int
    offset_s: float | None
    delay_s: float | None = None
    stratum: int | None = None
    server: str | None = None
    status: str = "unknown"  # green, yellow, red, or unknown
    os_synchronized: bool | None = None
    os_sync_tool: str | None = None


@dataclass(frozen=True)
class Stamped:
    """An event as delivered by the bus: system time when published, plus sequence."""

    t_ns: int
    seq: int
    event: Event
