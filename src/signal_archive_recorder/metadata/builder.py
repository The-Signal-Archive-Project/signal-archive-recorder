# SPDX-License-Identifier: Apache-2.0
"""Public metadata, built by allow-list and validated before it is written.

The session manager keeps a rich internal record of each chunk. This module copies
named fields from it into the published schema, never whole objects, so anything
an adapter added that isn't listed here is dropped (and logged locally). Free text
that survives is scrubbed of machine details, and the operator's callsign and grid
appear only as far as they chose to share them.
"""

from __future__ import annotations

import json
import logging
import math
import platform
import re
import statistics
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

from signal_archive_recorder import __version__
from signal_archive_recorder.clockmon.monitor import DEFAULT_SERVERS
from signal_archive_recorder.metadata.privacy import DecodeRedactor, Scrubber
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.modes.registry import ModeRegistry

log = logging.getLogger(__name__)

CHUNK_SCHEMA = "signal-archive-recorder/chunk/1"
LABEL_STATS_SCHEMA = "signal-archive-recorder/label-chunk-stats/1"
SESSION_SCHEMA = "signal-archive-recorder/session/1"
SOURCE_UNAVAILABLE = "source_unavailable"
NOT_REPORTED = "not_reported"
NOT_APPLICABLE = "not_applicable"
USER_WITHHELD = "user_withheld"
RIG_SETTINGS = ("sideband", "rig_mode", "filter_hz", "agc", "noise_blanker", "noise_reduction")
EVENT_FIELDS = {
    "FreqChanged": ("dial_hz",),
    "ModeChanged": ("mode_id", "raw_mode", "needs_mapping", "period_s"),
    "SettingChanged": ("name", "value"),
    "SourceUp": ("detail",),
    "SourceDown": ("reason",),
    "TxStarted": (),
    "TxEnded": (),
    "AudioGap": ("lost_frames", "reason"),
    "CaptureWarning": ("code",),
    "Note": ("detail",),
    "ClockChecked": (),  # published through _clock_check
}
OS_SYNC_TOOLS = ("timedatectl", "chronyc", "w32tm", "systemsetup")
DECODE_RAW_FIELDS = ("client_id", "new", "time_ms", "mode_symbol", "symbol_needs_mapping")


class MetadataError(ValueError):
    """Generated metadata failed schema validation (a bug: the file is not published)."""


def utc_iso(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns // 1000 / 1e6, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def known(value: Any, reason: str | None = None, source: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "reason": None if value is not None else reason}
    if source is not None:
        out["source"] = source
    return out


@cache
def _schemas() -> tuple[dict[str, dict[str, Any]], Registry[Any]]:
    loaded = {}
    for name in ("common", "chunk", "session", "decode", "label_stats"):
        text = files(__package__).joinpath(f"schemas/{name}.schema.json").read_text("utf-8")
        loaded[name] = json.loads(text)
    registry: Registry[Any] = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in loaded.values()
    )
    return loaded, registry


@cache
def _bands() -> list[tuple[int, int, str]]:
    data = json.loads(files(__package__).joinpath("bands.json").read_text("utf-8"))
    return [(b["low_hz"], b["high_hz"], b["band"]) for b in data["bands"]]


def band_for(dial_hz: int) -> str | None:
    return next((band for low, high, band in _bands() if low <= dial_hz <= high), None)


def _validator(name: str) -> jsonschema.Draft202012Validator:
    schemas, registry = _schemas()
    return jsonschema.Draft202012Validator(schemas[name], registry=registry)


@cache
def _params_validator(filename: str) -> jsonschema.Draft202012Validator:
    text = files("signal_archive_recorder.modes").joinpath(f"schemas/{filename}").read_text("utf-8")
    return jsonschema.Draft202012Validator(json.loads(text))


def _round(x: float | None, places: int = 2) -> float | None:
    return None if x is None or math.isinf(x) or math.isnan(x) else round(x, places)


_CRASH = re.compile(r"^crashed: (\w+)")


class MetadataBuilder:
    def __init__(
        self,
        registry: ModeRegistry,
        settings: StationSettings,
        *,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.scrub = Scrubber(extra_secrets)
        self.redactor = DecodeRedactor(settings)
        self.dropped: list[str] = []
        self._chunk_validator = _validator("chunk")
        self._session_validator = _validator("session")
        self._decode_validator = _validator("decode")
        self._label_stats_validator = _validator("label_stats")

    def _drop(self, what: str) -> None:
        self.dropped.append(what)
        log.info("metadata: dropped %s (not in the published schema)", what)

    def _text(self, value: str) -> str:
        """Free text: scrubbed of machine details and, if not shared, the operator."""
        return self.redactor.text(self.scrub.text(value))[0]

    # -- chunks ------------------------------------------------------------------

    def chunk(self, rec: dict[str, Any]) -> dict[str, Any]:
        audio, start = rec["audio"], rec["audio"]["start_frame"]
        first_ns = audio["first_sample_ns"]
        mode_value = rec["mode"]["value"]
        meta = {
            "schema": CHUNK_SCHEMA,
            "chunk_id": rec["chunk_id"],
            "session_id": rec["session_id"],
            "index": rec["index"],
            "time": {
                "first_sample_utc": None if first_ns is None else utc_iso(first_ns),
                "first_sample_ns": first_ns,
                "start_reason": audio["start_reason"],
                "end_reason": audio["end_reason"],
            },
            "audio": self._audio(rec),
            "radio": self._radio(rec),
            "mode": {
                "mode_id": self._known(rec["mode"]),
                "raw": rec["mode"].get("raw"),
                "needs_mapping": bool(rec["mode"].get("needs_mapping", False)),
                "params": self._mode_params(rec["mode"]) if mode_value else {},
            },
            "path": {"type": known(None, SOURCE_UNAVAILABLE)},  # set by satellite sources
            "clock": {
                "previous_check": None
                if rec.get("clock_before") is None
                else self._clock_check(rec["clock_before"], rec["audio"]["start_frame"])
            },
            "tx_intervals": [[s, e] for s, e in rec["tx_intervals"]],  # already chunk-relative
            "sources": {
                name: {
                    "up_at_start": bool(src["up_at_start"]),
                    "down": [[s, e, self._reason(why)] for s, e, why in src["down"]],
                }
                for name, src in rec["sources"].items()
            },
            "events": [e for e in (self._event(ev, start) for ev in rec["events"]) if e],
        }
        self._check(self._chunk_validator, meta, rec["chunk_id"])
        return meta

    def _known(self, k: dict[str, Any]) -> dict[str, Any]:
        return known(k.get("value"), k.get("reason") or NOT_REPORTED, k.get("source"))

    def _audio(self, rec: dict[str, Any]) -> dict[str, Any]:
        a, flac = rec["audio"], rec.get("flac")
        gaps = [
            {
                "stream_frame": g["stream_frame"],
                "lost_frames": g["lost_frames"],
                "reason": g["reason"],
            }
            for g in a["gaps"]
        ]
        return {
            "sample_rate": a["sample_rate"],
            "channels": a["channels"],
            "bit_depth": {"int16": 16, "int24": 24}[a["sample_format"]],
            "sample_count": a["sample_count"],
            "start_frame": a["start_frame"],
            "end_frame": a["end_frame"],
            "first_sample_frame": a["first_sample_frame"],
            "flac": None
            if flac is None
            else {
                "file": flac["file"],
                "pcm_md5": flac["pcm_md5"],
                "sha256": flac["sha256"],
                "verified": flac["verified"],
            },
            "levels": {
                "peak_dbfs": [_round(x) for x in a["levels"]["peak_dbfs"]],
                "rms_dbfs": [_round(x) for x in a["levels"]["rms_dbfs"]],
                "clipped": list(a["levels"]["clipped"]),
            },
            "gaps": gaps,
            "buffer_overrun_frames": sum(
                g["lost_frames"] or 0 for g in gaps if g["reason"] == "buffer_overrun"
            ),
            "driver_overflows": sum(1 for g in gaps if g["reason"] == "driver_overflow"),
            "sync_points": [[int(f), int(t)] for f, t in a["sync_points"]],
        }

    def _radio(self, rec: dict[str, Any]) -> dict[str, Any]:
        dial = self._known(rec["dial_hz"])
        if dial["value"] is not None:
            band = known(band_for(dial["value"]), NOT_APPLICABLE, dial.get("source"))
        else:
            band = known(None, dial["reason"])
        radio = {"dial_hz": dial, "band": band}
        settings = rec.get("settings", {})
        for name in RIG_SETTINGS:
            k = settings.get(name)
            radio[name] = known(None, SOURCE_UNAVAILABLE) if k is None else self._known(k)
        for name in settings:
            if name not in RIG_SETTINGS:
                self._drop(f"setting {name!r}")
        return radio

    def _mode_params(self, mode: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if mode.get("period_s") is not None:
            params["period_s"] = mode["period_s"]
        params.update({k: v for k, v in (mode.get("params") or {}).items() if v is not None})
        schema = self.registry.get(mode["value"]).params_schema
        if schema is None:
            for key in params:
                self._drop(f"mode param {key!r} ({mode['value']} has no params)")
            return {}
        validator = _params_validator(schema)
        for key in list(params):
            if not validator.is_valid({key: params[key]}):
                self._drop(f"mode param {key!r}={params[key]!r}")
                del params[key]
        return params

    def label_stats(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        """Per-source decode statistics for a chunk: labels, published apart from it."""
        out = []
        labels = dict(rec.get("labels", {}))
        if decoder := rec["mode"].get("source"):  # the decoder ran, even if it heard nothing
            labels.setdefault(decoder, {"live": 0, "off_air": 0, "live_dt_s": []})
        for source, decodes in sorted(labels.items()):
            stats = {
                "schema": LABEL_STATS_SCHEMA,
                "chunk_id": rec["chunk_id"],
                "source": self._text(source),
                **self._decode_stats(rec, decodes),
            }
            self._check(self._label_stats_validator, stats, f"{rec['chunk_id']} {source} stats")
            out.append(stats)
        return out

    def _decode_stats(self, rec: dict[str, Any], decodes: dict[str, Any]) -> dict[str, Any]:
        off_air = int(decodes.get("off_air", 0))
        mode = rec["mode"]
        if mode["value"] is None:
            reason = mode.get("reason") or NOT_REPORTED
            return {"count": known(None, reason), "off_air_count": off_air,
                    "median_dt_s": known(None, reason)}  # fmt: skip
        if not self.registry.get(mode["value"]).timing.slotted:
            return {"count": known(None, NOT_APPLICABLE), "off_air_count": off_air,
                    "median_dt_s": known(None, NOT_APPLICABLE)}  # fmt: skip
        dts = [d for d in decodes.get("live_dt_s", []) if d is not None]
        median = _round(statistics.median(dts), 3) if dts else None
        return {
            "count": known(int(decodes.get("live", 0))),
            "off_air_count": off_air,
            "median_dt_s": known(median, NOT_REPORTED),
        }

    def _reason(self, why: str) -> str:
        crash = _CRASH.match(why)
        return f"crashed: {crash.group(1)}" if crash else self._text(why)

    def _event(self, ev: dict[str, Any], start: int) -> dict[str, Any] | None:
        kind = ev.get("type")
        if kind not in EVENT_FIELDS:
            self._drop(f"event type {kind!r}")
            return None
        if kind == "SettingChanged" and ev.get("name") not in RIG_SETTINGS:
            self._drop(f"setting event {ev.get('name')!r}")
            return None
        out: dict[str, Any] = {
            "type": kind,
            "source": self._text(ev["source"]),
            "t_ns": ev["t_ns"],
            "frame_offset": ev["stream_frame"] - start,
        }
        if ev.get("late"):
            out["late"] = True
        if kind == "ClockChecked":
            check = self._clock_check(ev, start)
            check.pop("frame_offset")
            return {**out, **check}
        for field in EVENT_FIELDS[kind]:
            if field not in ev:
                continue
            value = ev[field]
            if field == "reason" and isinstance(value, str):
                value = self._reason(value)
            elif isinstance(value, str):
                value = self._text(value)
            out[field] = value
        for field in set(ev) - set(out) - {"stream_frame", "message"}:
            self._drop(f"event field {kind}.{field}")
        return out

    def _clock_check(self, check: dict[str, Any], start_frame: int) -> dict[str, Any]:
        server = check.get("server")
        tool = check.get("os_sync_tool")
        return {
            "measured_ns": check["measured_ns"],
            "measured_utc": utc_iso(check["measured_ns"]),
            "frame_offset": check["stream_frame"] - start_frame,
            "offset_s": _round(check.get("offset_s"), 6),
            "delay_s": _round(check.get("delay_s"), 6),
            "stratum": check.get("stratum"),
            # A home network's time server name isn't published.
            "server": server if server in DEFAULT_SERVERS or server is None else "custom",
            "status": check.get("status", "unknown"),
            "os_synchronized": check.get("os_synchronized"),
            "os_sync_tool": tool if tool in OS_SYNC_TOOLS else None,
        }

    # -- sessions ----------------------------------------------------------------

    def session(self, info: dict[str, Any]) -> dict[str, Any]:
        s = self.settings
        if s.callsign and s.share_callsign:
            callsign = known(s.callsign)
        else:
            callsign = known(None, USER_WITHHELD if s.callsign else NOT_REPORTED)
        grid = s.shared_grid
        grid_reason = USER_WITHHELD if s.grid and s.grid_precision == 0 else NOT_REPORTED
        ended = info.get("ended_ns")
        meta = {
            "schema": SESSION_SCHEMA,
            "session_id": info["session_id"],
            "app": {"name": "signal-archive-recorder", "version": __version__},
            "platform": {
                "os": platform.system(),
                "os_release": self.scrub.text(platform.release()),
                "python": platform.python_version(),
            },
            "started_utc": utc_iso(info["started_ns"]),
            "started_ns": info["started_ns"],
            "ended_utc": None if ended is None else utc_iso(ended),
            "ended_ns": ended,
            "end_reason": info.get("end_reason"),
            "operator": {
                "callsign": callsign,
                "hf_username": s.hf_username,
                "station_profile_id": s.station_profile_id,
            },
            "location": {"grid": known(grid, grid_reason), "grid_precision": len(grid or "")},
            "consent": None
            if s.consent is None
            else {
                "accepted_utc": utc_iso(s.consent.accepted_ns),
                "license_id": s.consent.license_id,
            },
            "audio": {
                "sample_rate": info["audio"]["sample_rate"],
                "channels": info["audio"]["channels"],
                "bit_depth": {"int16": 16, "int24": 24}[info["audio"]["sample_format"]],
            },
            "clock": self._session_clock(info.get("clock_checks", [])),
            "software": {self._text(k): self._text(v) for k, v in info["software"].items()},
            "chunks": list(info["chunks"]),
            "labels": [self._text(x) for x in info.get("labels", [])],
        }
        self._check(self._session_validator, meta, info["session_id"])
        return meta

    def _session_clock(self, checks: list[dict[str, Any]]) -> dict[str, Any]:
        public = []
        for c in checks:
            entry = self._clock_check(c, 0)
            entry["stream_frame"] = entry.pop("frame_offset")
            public.append(entry)
        measured = any(c["offset_s"] is not None for c in public)
        return {
            "time_source": known("ntp" if measured else None, SOURCE_UNAVAILABLE),
            "checks": public,
        }

    def validate_session(self, doc: dict[str, Any]) -> None:
        self._check(self._session_validator, doc, doc.get("session_id", "session"))

    # -- decode logs -------------------------------------------------------------

    def decode_line(self, line: dict[str, Any]) -> dict[str, Any]:
        text, redacted = self.redactor.text(self.scrub.text(line.get("text", "")))
        out = {
            key: line.get(key)
            for key in ("received_ns", "source", "mode_id", "decoder_time_ns", "snr_db", "dt_s",
                        "df_hz", "low_confidence", "off_air")
        }  # fmt: skip
        out["text"] = text
        if redacted:
            out["redacted"] = True
        raw = line.get("raw") or {}
        out["raw"] = {
            k: self._text(v) if isinstance(v, str) else v
            for k, v in raw.items()
            if k in DECODE_RAW_FIELDS
        }
        for k in set(raw) - set(DECODE_RAW_FIELDS):
            self._drop(f"decode raw field {k!r}")
        self._check(self._decode_validator, out, "decode line")
        return out

    def publish_decode_log(self, path: Path) -> int:
        """Rewrite a decode log in place through the privacy filter; returns line count."""
        lines = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        public = [self.decode_line(x) for x in lines]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in public), "utf-8")
        tmp.replace(path)
        return len(public)

    def _check(self, validator: jsonschema.Draft202012Validator, doc: Any, what: str) -> None:
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            where = "/".join(str(p) for p in first.path)
            raise MetadataError(f"{what}: {where}: {first.message} ({len(errors)} errors)")
