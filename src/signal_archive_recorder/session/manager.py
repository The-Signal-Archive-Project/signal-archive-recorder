# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The session manager: owns the timeline, cuts audio into chunks, records context.

It is both an audio sink (fed by the capture writer thread) and a bus subscriber
(fed by the dispatcher thread). Every event is placed at a stream frame. Audio is
held back briefly before it is committed, so a frequency or mode change splits the
chunk at the exact frame it happened even when the event arrives a little after
the audio. An event later than the holdback is applied at the current position and
flagged `late`.

A chunk ends at the first of: the chunk policy's next boundary (from the mode at
the chunk's start), a dial-frequency change, a mode change, or the end of the
session. Values are known only while the source that reported them is up: a chunk
that starts while that source is down records null with reason source_unavailable.
"""

from __future__ import annotations

import bisect
import json
import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from signal_archive_recorder.audio.flac_writer import FlacResult, FlacWriter
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.audio.levels import LevelMeter
from signal_archive_recorder.audio.timeline import StreamTimeline
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.core.events import (
    AudioGap,
    CaptureWarning,
    ClockChecked,
    Decode,
    Event,
    FreqChanged,
    ModeChanged,
    Note,
    SettingChanged,
    SourceDown,
    SourceUp,
    Stamped,
    TxEnded,
    TxStarted,
)
from signal_archive_recorder.metadata.builder import MetadataBuilder, MetadataError
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.modes import DEFAULT_CHUNK_S, ChunkPolicy, ModeRegistry
from signal_archive_recorder.session.storage import SessionDir, SessionStorage, write_json_atomic

log = logging.getLogger(__name__)

SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass(frozen=True)
class Known:
    """A reported value and who reported it."""

    value: Any
    source: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(order=True)
class _Change:
    frame: int
    seq: int
    event: Event = field(compare=False)
    t_ns: int = field(compare=False)
    late: bool = field(compare=False, default=False)


@dataclass
class _Chunk:
    index: int
    start_frame: int
    start_reason: str
    policy_end: int
    mode: dict[str, Any]
    dial: dict[str, Any]
    sources_up: dict[str, bool]
    levels: LevelMeter
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    writer: FlacWriter | None = None
    chunk_id: str = ""
    first_written: int | None = None
    samples: int = 0
    tx_intervals: list[list[int]] = field(default_factory=list)
    tx_open: int | None = None
    down: dict[str, list[list[Any]]] = field(default_factory=dict)
    down_open: dict[str, tuple[int, str]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    # Third-party decoder output per source: labels, kept out of the recording metadata.
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    clock_before: dict[str, Any] | None = None


class SessionManager:
    def __init__(
        self,
        *,
        storage: SessionStorage,
        fmt: AudioFormat,
        registry: ModeRegistry,
        clock: Clock,
        bus: EventBus,
        timeline: StreamTimeline,
        target_chunk_s: int = DEFAULT_CHUNK_S,
        holdback_s: float = 2.0,
        compression_level: int = 8,
        builder: MetadataBuilder | None = None,
    ) -> None:
        self.format = fmt
        self._storage = storage
        self._registry = registry
        self._clock = clock
        self._bus = bus
        self._timeline = timeline
        self._target_s = target_chunk_s
        self._holdback = round(holdback_s * fmt.sample_rate)
        self._compression = compression_level
        self.builder = builder or MetadataBuilder(registry, StationSettings())
        self._lock = threading.RLock()

        self.session: SessionDir | None = None
        self._started_ns = 0
        self._pending: deque[tuple[int, bytes]] = deque()
        self._latest_end = 0
        self._pos = 0
        self._chunk: _Chunk | None = None
        self._chunk_count = 0
        self._changes: list[_Change] = []
        self._splits: list[tuple[int, str]] = []
        self._seq = 0
        self._last_dial: Any = None
        self._last_mode: Any = None
        # Live state, as of self._pos.
        self._mode: Known | None = None
        self._dial: Known | None = None
        self._up: dict[str, bool] = {}
        self._settings: dict[str, Known] = {}
        self.software: dict[str, str] = {}
        self.clock_checks: list[dict[str, Any]] = []
        self._tx = False
        self._finalizer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chunk-final")
        self._futures: list[Future[dict[str, Any]]] = []
        self.chunks: list[dict[str, Any]] = []
        self._unsubscribe: Any = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> SessionDir:
        self._started_ns = self._clock.now_ns()
        self.session = self._storage.create(self._started_ns)
        self._write_session(ended_ns=None, end_reason=None)
        self._unsubscribe = self._bus.subscribe(self.on_event)
        return self.session

    def close(self, end_reason: str = "stopped") -> list[dict[str, Any]]:
        """Commit everything, close the last chunk and wait for all chunks to finish."""
        with self._lock:
            while self._pending:
                self._commit(*self._pending.popleft())
            self._apply_changes(self._pos + 1)
            if self._chunk is not None:
                self._close_chunk(self._pos, "session_end")
        if self._unsubscribe:
            self._unsubscribe()
        for future in self._futures:
            future.result()
        self._finalizer.shutdown(wait=True)
        assert self.session is not None
        # Sources must be stopped first: decode logs are rewritten for publication.
        for decode_log in sorted(self.session.labels.glob("*/decodes.jsonl")):
            self.builder.publish_decode_log(decode_log)
        self._write_session(ended_ns=self._clock.now_ns(), end_reason=end_reason)
        return self.chunks

    # -- inputs ------------------------------------------------------------------

    def write(self, data: bytes, stream_frame: int) -> None:
        """Audio sink, called on the capture writer thread."""
        with self._lock:
            end = stream_frame + len(data) // self.format.frame_bytes
            self._pending.append((stream_frame, data))
            self._latest_end = max(self._latest_end, end)
            while self._pending and self._block_end(self._pending[0]) <= (
                self._latest_end - self._holdback
            ):
                self._commit(*self._pending.popleft())

    def on_event(self, stamped: Stamped) -> None:
        """Bus subscriber, called on the dispatcher thread."""
        event = stamped.event
        with self._lock:
            if isinstance(event, AudioGap):
                frame = event.stream_frame
            else:
                frame = self._timeline.ns_to_frame(stamped.t_ns)
            late = frame < self._pos and not isinstance(event, AudioGap)
            if late:
                frame = self._pos
            self._seq += 1
            bisect.insort(self._changes, _Change(frame, self._seq, event, stamped.t_ns, late))
            if isinstance(event, FreqChanged) and event.dial_hz != self._last_dial:
                self._last_dial = event.dial_hz
                self._add_split(frame, "freq_change")
            elif isinstance(event, ModeChanged):
                key = (event.mode_id, event.raw_mode, event.period_s)
                if key != self._last_mode:
                    self._last_mode = key
                    self._add_split(frame, "mode_change")

    def _add_split(self, frame: int, reason: str) -> None:
        bisect.insort(self._splits, (frame, reason))

    # -- committing audio --------------------------------------------------------

    def _block_end(self, block: tuple[int, bytes]) -> int:
        return block[0] + len(block[1]) // self.format.frame_bytes

    def _commit(self, stream_frame: int, data: bytes) -> None:
        fb = self.format.frame_bytes
        end = stream_frame + len(data) // fb
        if self._chunk is None:
            self._pos = stream_frame
            self._open_chunk(stream_frame, "session_start")
        self._advance(stream_frame)  # across a gap, if there is one
        while self._pos < end:
            chunk = self._chunk
            assert chunk is not None
            cut, reason = self._next_cut(chunk)
            seg_end = min(end, cut)
            self._apply_changes(seg_end)
            seg = data[(self._pos - stream_frame) * fb : (seg_end - stream_frame) * fb]
            self._write_audio(chunk, seg)
            self._pos = seg_end
            if self._pos == cut:
                self._close_chunk(cut, reason)
                self._open_chunk(cut, reason)

    def _advance(self, to: int) -> None:
        """Move over frames that never arrived, still cutting chunks on schedule."""
        while self._pos < to:
            assert self._chunk is not None
            cut, reason = self._next_cut(self._chunk)
            step = min(to, cut)
            self._apply_changes(step)
            self._pos = step
            if step == cut:
                self._close_chunk(cut, reason)
                self._open_chunk(cut, reason)

    def _next_cut(self, chunk: _Chunk) -> tuple[int, str]:
        landed_at_start = False
        while self._splits and self._splits[0][0] <= chunk.start_frame:
            self._splits.pop(0)  # part of this chunk's starting state
            landed_at_start = True
        if landed_at_start and chunk.samples == 0:
            # A (late) change on the chunk's first frame, before any audio: restart it
            # with the new values instead of leaving an empty chunk behind.
            self._apply_changes(chunk.start_frame + 1)
            self._refresh_start_state(chunk)
        if self._splits and self._splits[0][0] < chunk.policy_end:
            frame = self._splits[0][0]
            # Changes often arrive together (WSJT-X sends a new mode and its frequency
            # in one burst): report every reason for this cut.
            reasons = sorted({r for f, r in self._splits if f == frame})
            return frame, "+".join(reasons)
        return chunk.policy_end, "policy"

    def _write_audio(self, chunk: _Chunk, seg: bytes) -> None:
        if not seg:
            return
        if chunk.writer is None:
            assert self.session is not None
            chunk.first_written = self._pos
            chunk.writer = FlacWriter(
                self.session.flac(chunk.chunk_id),
                self.format,
                compression_level=self._compression,
                tags=self.builder.flac_tags(
                    session_id=self.session.session_id,
                    chunk_id=chunk.chunk_id,
                    first_sample_ns=self._timeline.frame_to_ns(self._pos),
                ),
            )
        chunk.writer.write(seg)
        chunk.levels.update(seg)
        chunk.samples += len(seg) // self.format.frame_bytes

    # -- state changes -----------------------------------------------------------

    def _apply_changes(self, before: int) -> None:
        """Apply every change at a frame before `before` to the live state and chunk."""
        while self._changes and self._changes[0].frame < before:
            self._apply(self._changes.pop(0))

    def _apply(self, change: _Change) -> None:
        e, chunk, frame = change.event, self._chunk, change.frame
        if isinstance(e, FreqChanged):
            self._dial = Known(e.dial_hz, e.source)
        elif isinstance(e, ModeChanged):
            detail = {
                "raw": e.raw_mode,
                "needs_mapping": e.needs_mapping,
                "period_s": e.period_s,
                "params": dict(e.params),
            }
            self._mode = Known(e.mode_id, e.source, detail)
        elif isinstance(e, SettingChanged):
            self._settings[e.name] = Known(e.value, e.source)
        elif isinstance(e, SourceUp):
            self._up[e.source] = True
            if e.detail:
                self.software.setdefault(e.source, e.detail)
            if chunk and e.source in chunk.down_open:
                start, reason = chunk.down_open.pop(e.source)
                chunk.down.setdefault(e.source, []).append([start, frame, reason])
        elif isinstance(e, SourceDown):
            self._up[e.source] = False
            if chunk and e.source not in chunk.down_open:
                chunk.down_open[e.source] = (frame, e.reason)
        elif isinstance(e, TxStarted):
            self._tx = True
            if chunk and chunk.tx_open is None:
                chunk.tx_open = frame
        elif isinstance(e, TxEnded):
            self._tx = False
            if chunk and chunk.tx_open is not None:
                chunk.tx_intervals.append([chunk.tx_open, frame])
                chunk.tx_open = None
        elif isinstance(e, ClockChecked):
            check = self._clock_record(e, frame)
            self.clock_checks.append(check)
        elif isinstance(e, AudioGap) and chunk:
            chunk.gaps.append(
                {"stream_frame": e.stream_frame, "lost_frames": e.lost_frames, "reason": e.reason}
            )
        if chunk is None:
            return
        if isinstance(e, Decode):
            stats = chunk.labels.setdefault(e.source, {"live": 0, "off_air": 0, "live_dt_s": []})
            if e.off_air:
                stats["off_air"] += 1
            else:
                stats["live"] += 1
                if e.dt_s is not None:
                    stats["live_dt_s"].append(e.dt_s)
            return
        chunk.events.append(self._event_record(change))

    def _event_record(self, change: _Change) -> dict[str, Any]:
        e = change.event
        record: dict[str, Any] = {
            "type": type(e).__name__,
            "source": e.source,
            "t_ns": change.t_ns,
            "stream_frame": change.frame,
        }
        if change.late:
            record["late"] = True
        if isinstance(e, FreqChanged):
            record["dial_hz"] = e.dial_hz
        elif isinstance(e, ModeChanged):
            record.update(
                mode_id=e.mode_id,
                raw_mode=e.raw_mode,
                needs_mapping=e.needs_mapping,
                period_s=e.period_s,
            )
        elif isinstance(e, SourceDown):
            record["reason"] = e.reason
        elif isinstance(e, SourceUp):
            record["detail"] = e.detail
        elif isinstance(e, SettingChanged):
            record.update(name=e.name, value=e.value)
        elif isinstance(e, CaptureWarning):
            record.update(code=e.code, message=e.message)
        elif isinstance(e, AudioGap):
            record.update(lost_frames=e.lost_frames, reason=e.reason)
        elif isinstance(e, ClockChecked):
            record.update(self._clock_record(e, change.frame))
        elif isinstance(e, Note):
            record["detail"] = e.text
        return record

    @staticmethod
    def _clock_record(e: ClockChecked, frame: int) -> dict[str, Any]:
        return {
            "measured_ns": e.measured_ns,
            "stream_frame": frame,
            "offset_s": e.offset_s,
            "delay_s": e.delay_s,
            "stratum": e.stratum,
            "server": e.server,
            "status": e.status,
            "os_synchronized": e.os_synchronized,
            "os_sync_tool": e.os_sync_tool,
        }

    # -- chunks ------------------------------------------------------------------

    def _known(self, known: Known | None) -> dict[str, Any]:
        if known is None:  # no source has ever reported it
            return {"value": None, "reason": SOURCE_UNAVAILABLE}
        if not self._up.get(known.source, False):
            return {"value": None, "reason": SOURCE_UNAVAILABLE, "source": known.source}
        return {"value": known.value, "reason": None, "source": known.source, **known.detail}

    def _open_chunk(self, frame: int, reason: str) -> None:
        assert self.session is not None
        chunk = _Chunk(
            index=self._chunk_count,
            start_frame=frame,
            start_reason=reason,
            policy_end=frame + 1,
            mode={},
            dial={},
            sources_up={},
            levels=LevelMeter(self.format),
        )
        chunk.chunk_id = self.session.chunk_id(chunk.index, self._timeline.frame_to_ns(frame))
        if self._tx:
            chunk.tx_open = frame
        for source, up in self._up.items():
            if not up:
                chunk.down_open[source] = (frame, "carried_over")
        self._chunk_count += 1
        self._chunk = chunk
        self._apply_changes(frame + 1)  # changes at the cut belong to the new chunk
        self._refresh_start_state(chunk)

    def _refresh_start_state(self, chunk: _Chunk) -> None:
        """The chunk's starting values and policy, from the live state at its start."""
        chunk.mode = self._known(self._mode)
        chunk.dial = self._known(self._dial)
        chunk.settings = {name: self._known(k) for name, k in self._settings.items()}
        chunk.sources_up = dict(self._up)
        chunk.clock_before = self.clock_checks[-1] if self.clock_checks else None
        policy = self._policy(chunk.mode)
        start_ns = self._timeline.frame_to_ns(chunk.start_frame)
        half_frame = 500_000_000 // self.format.sample_rate
        end = self._timeline.ns_to_frame(policy.next_boundary(start_ns + half_frame))
        chunk.policy_end = max(end, chunk.start_frame + 1)

    def _policy(self, mode: dict[str, Any]) -> ChunkPolicy:
        known = mode["value"] is not None
        registry_mode = self._registry.get(mode["value"]) if known else self._registry.unknown
        try:
            return ChunkPolicy.for_mode(
                registry_mode, period_s=mode.get("period_s"), target_s=self._target_s
            )
        except ValueError:
            # A reported period the registry doesn't allow: fall back to the default.
            log.warning("mode %s: unexpected period %s", mode["value"], mode.get("period_s"))
            return ChunkPolicy.for_mode(registry_mode, target_s=self._target_s)

    def _close_chunk(self, end: int, reason: str) -> None:
        chunk = self._chunk
        assert chunk is not None and self.session is not None
        self._chunk = None
        if chunk.tx_open is not None:
            chunk.tx_intervals.append([chunk.tx_open, end])
        for source, (start, why) in chunk.down_open.items():
            chunk.down.setdefault(source, []).append([start, end, why])
        audio = self._audio_record(chunk, end, reason)
        session = self.session
        self._futures.append(self._finalizer.submit(self._finalize, chunk, audio, session))

    def _audio_record(self, chunk: _Chunk, end: int, end_reason: str) -> dict[str, Any]:
        first = chunk.first_written
        return {
            "start_reason": chunk.start_reason,
            "end_reason": end_reason,
            "start_frame": chunk.start_frame,
            "end_frame": end,
            "first_sample_frame": first,
            "first_sample_ns": None if first is None else self._timeline.frame_to_ns(first),
            "sample_count": chunk.samples,
            "sync_points": [list(p) for p in self._timeline.sync_points(chunk.start_frame, end)],
        }

    def _finalize(
        self, chunk: _Chunk, audio: dict[str, Any], session: SessionDir
    ) -> dict[str, Any]:
        flac: FlacResult | None = None
        try:
            if chunk.writer is not None:
                flac = chunk.writer.close()
        except Exception as exc:  # the chunk is reported as failed, never dropped silently
            log.exception("finalising chunk %s failed", chunk.chunk_id)
            audio["error"] = repr(exc)
        record = self._chunk_record(chunk, audio, flac, session)
        try:
            meta = self.builder.chunk(record)
        except MetadataError:
            # A bug, not bad luck: keep the record locally, and never publish it.
            log.exception("chunk %s metadata failed validation", chunk.chunk_id)
            session.local.mkdir(exist_ok=True)
            write_json_atomic(session.local / f"{chunk.chunk_id}.meta.invalid.json", record)
            return record
        write_json_atomic(session.meta(chunk.chunk_id), meta)
        for stats in self.builder.label_stats(record):
            path = session.label_stats(stats["source"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(stats) + "\n")
        with self._lock:
            self.chunks.append(meta)
        return meta

    def _chunk_record(
        self, chunk: _Chunk, audio: dict[str, Any], flac: FlacResult | None, session: SessionDir
    ) -> dict[str, Any]:
        """The manager's full internal record; the builder decides what is published."""
        rel = chunk.start_frame
        levels = asdict(chunk.levels.snapshot())
        return {
            "chunk_id": chunk.chunk_id,
            "session_id": session.session_id,
            "index": chunk.index,
            "audio": {
                **audio,
                "sample_rate": self.format.sample_rate,
                "channels": self.format.channels,
                "sample_format": self.format.sample_format,
                "gaps": chunk.gaps,
                "levels": levels,
            },
            "flac": None
            if flac is None
            else {
                "file": flac.path.name,
                "pcm_md5": flac.pcm_md5,
                "sha256": flac.sha256,
                "verified": flac.verified,
                "error": flac.error,
            },
            "mode": chunk.mode,
            "dial_hz": chunk.dial,
            "settings": chunk.settings,
            "tx_intervals": [[s - rel, e - rel] for s, e in chunk.tx_intervals],
            "sources": {
                source: {
                    "up_at_start": chunk.sources_up.get(source, False),
                    "down": [[s - rel, e - rel, why] for s, e, why in spans if e > s],
                }
                for source, spans in {**{s: [] for s in chunk.sources_up}, **chunk.down}.items()
            },
            "labels": chunk.labels,
            "clock_before": chunk.clock_before,
            "events": chunk.events,
        }

    def _write_session(self, *, ended_ns: int | None, end_reason: str | None) -> None:
        assert self.session is not None
        with self._lock:
            chunk_ids = sorted(c["chunk_id"] for c in self.chunks)
            software = dict(self.software)
            clock_checks = list(self.clock_checks)
        found = self.session.labels.iterdir() if self.session.labels.is_dir() else iter(())
        labels = sorted(p.name for p in found if p.is_dir() and any(p.iterdir()))
        meta = self.builder.session(
            {
                "session_id": self.session.session_id,
                "started_ns": self._started_ns,
                "ended_ns": ended_ns,
                "end_reason": end_reason,
                "audio": {
                    "sample_rate": self.format.sample_rate,
                    "channels": self.format.channels,
                    "sample_format": self.format.sample_format,
                },
                "software": software,
                "chunks": chunk_ids,
                "labels": labels,
                "clock_checks": clock_checks,
            }
        )
        write_json_atomic(self.session.session_json, meta)
