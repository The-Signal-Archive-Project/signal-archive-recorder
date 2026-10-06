# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Runs the recorder for the window and tray, and reports its health. No Qt here.

Pause ends the current session cleanly (its last chunk is finished and the audio
device released) and resume starts a new one, so every session is one unbroken
recording.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from signal_archive_recorder.config import RecorderConfig
from signal_archive_recorder.core.events import (
    CaptureWarning,
    ClockChecked,
    FreqChanged,
    ModeChanged,
    Note,
    SourceDown,
    SourceUp,
    Stamped,
)
from signal_archive_recorder.modes.registry import ModeRegistry
from signal_archive_recorder.recorder import Recorder
from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.ui.health import DiskState, HealthInputs, WsjtxState
from signal_archive_recorder.upload.queue import UploadRecord, describe_result
from signal_archive_recorder.upload.review import ReviewError, format_review, review
from signal_archive_recorder.upload.screening import format_screens, screen_session

log = logging.getLogger(__name__)

RecorderFactory = Callable[[RecorderConfig], Recorder]


class RecorderController:
    def __init__(self, config: RecorderConfig, factory: RecorderFactory = Recorder) -> None:
        self.config = config
        self._factory = factory
        self.recorder: Recorder | None = None
        self.paused = False
        self.notes: list[str] = []
        self._lock = threading.Lock()
        self._health = HealthInputs(wsjtx="never" if config.wsjtx.enabled else "disabled")

    # -- control ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self.recorder is not None:
                return
            recorder = self._factory(self.config)
            session = recorder.start()
            assert recorder.bus is not None
            recorder.bus.subscribe(self._on_event)
            self.recorder, self.paused = recorder, False
            wsjtx: WsjtxState = "never" if self.config.wsjtx.enabled else "disabled"
            self._health = replace(
                self._health,
                recording=True,
                paused=False,
                session=session.session_id,
                wsjtx=wsjtx,
                disk="ok",
            )

    def stop(self, reason: str = "stopped") -> None:
        with self._lock:
            recorder, self.recorder = self.recorder, None
            self._health.recording = False
        if recorder is not None:
            recorder.stop(reason=reason)

    def pause(self) -> None:
        self.stop(reason="paused")
        self.paused = True
        self._health.paused = True

    def resume(self) -> None:
        self.paused = False
        self._health.paused = False
        self.start()

    def mark(self, text: str) -> bool:
        """Add an operator note at this moment of the recording."""
        text = text.strip()
        recorder = self.recorder
        if not text or recorder is None or recorder.bus is None:
            return False
        recorder.bus.publish(Note(source="operator", text=text[:200]))
        self.notes.append(text)
        return True

    # -- health -------------------------------------------------------------------

    def _on_event(self, stamped: Stamped) -> None:
        e = stamped.event
        h = self._health
        if isinstance(e, SourceUp) and e.source == "wsjtx":
            h.wsjtx = "up"
        elif isinstance(e, SourceDown) and e.source == "wsjtx":
            h.wsjtx = "down"
        elif isinstance(e, ModeChanged) and e.source == "wsjtx":
            h.mode = e.raw_mode
        elif isinstance(e, FreqChanged) and e.source == "wsjtx":
            h.dial_hz = e.dial_hz
        elif isinstance(e, ClockChecked):
            h.clock, h.clock_offset_s = e.status, e.offset_s
        elif isinstance(e, CaptureWarning):
            if e.code in ("disk_low", "disk_critical", "disk_ok"):
                disk: dict[str, DiskState] = {"disk_low": "low", "disk_critical": "critical"}
                h.disk = disk.get(e.code, "ok")
            elif e.code == "wsjtx_port_busy":
                h.wsjtx = "down"

    def snapshot(self) -> HealthInputs:
        h = replace(self._health)
        recorder = self.recorder
        if recorder is not None:
            level = recorder.live_level.latest() if recorder.live_level else None
            if level is not None:
                h.peak_dbfs = max(level.peak_dbfs)
                h.clipped = sum(level.clipped)
            if recorder.capture is not None:
                h.frames_lost = recorder.capture.stats().lost_frames
        h.uploads = self.upload_counts()
        return h

    def upload_counts(self) -> dict[str, int]:
        storage = SessionStorage(self.config.storage_root)
        if not storage.sessions.is_dir():
            return {}
        current = self._health.session if self._health.recording else None
        counts: Counter[str] = Counter()
        for folder in storage.sessions.iterdir():
            if folder.is_dir() and folder.name != current:
                counts[UploadRecord.load(folder).state.value] += 1
        return dict(counts)

    def upload_now(self) -> Any:
        """Upload finished sessions now (call off the UI thread)."""
        from signal_archive_recorder.cli import _uploader

        uploader = _uploader(self.config)
        results = uploader.upload_all()
        uploader.poll_all()
        return results

    # -- review and upload (for the review window; call off the UI thread) -------

    def finished_sessions(self) -> list[SessionRow]:
        storage = SessionStorage(self.config.storage_root)
        current = self._health.session if self._health.recording else None
        rows = []
        if storage.sessions.is_dir():
            for folder in sorted(storage.sessions.iterdir(), reverse=True):  # newest first
                if folder.name == current or not (folder / "session.json").exists():
                    continue
                try:
                    r = review(folder)
                except (ReviewError, OSError, ValueError) as exc:
                    log.warning("can't review %s: %s", folder.name, exc)
                    continue
                state = UploadRecord.load(folder).state.value
                rows.append(SessionRow(folder.name, state, len(r.chunks), r.seconds, r.finished))
        return rows

    def describe(self, session_id: str) -> str:
        """Everything an upload of this session would share, and the radio-audio checks."""
        folder = SessionStorage(self.config.storage_root).sessions / session_id
        record = UploadRecord.load(folder)
        lines = [format_review(review(folder)), ""]
        screens = screen_session(
            folder, ModeRegistry.load_default(), require_decoder=self.config.upload.require_decoder
        )
        lines += format_screens(screens)
        lines += ["", f"Upload state: {record.state.value}"]
        if record.pr_url:
            lines.append(f"Pull request: {record.pr_url}")
        if record.last_error:
            lines.append(f"Last error: {record.last_error}")
        lines += [f"Problem: {p}" for p in record.problems]
        return "\n".join(lines)

    def upload(self, session_ids: list[str]) -> list[str]:
        """Upload these sessions now; returns what happened, as lines of text."""
        from signal_archive_recorder.cli import _uploader

        uploader = _uploader(self.config)
        sessions = SessionStorage(self.config.storage_root).sessions
        lines: list[str] = []
        for session_id in session_ids:
            text, _ = describe_result(session_id, uploader.upload(sessions / session_id))
            lines += text
        return lines


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    state: str
    chunks: int
    seconds: float
    finished: bool
