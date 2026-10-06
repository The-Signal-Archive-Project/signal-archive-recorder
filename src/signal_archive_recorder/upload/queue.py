# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Uploading finished sessions, one pull request each, and following them up.

Each session's state lives in its local/upload.json:

    queued -> uploading -> pr_opened -> validated
                                     -> failed      (the intake validator rejected it)
    queued <- (any error while uploading: kept, retried later)
    blocked                          (preflight found a problem; fix it, then re-queue)

Local files are never deleted here. A failed attempt leaves everything in place
and the session queued. Before opening a PR, an existing open PR for the same
session is looked for first, so a crash between "PR created" and "state saved"
can't create a duplicate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.session.storage import SessionStorage, write_json_atomic
from signal_archive_recorder.upload.consent import ConsentStore
from signal_archive_recorder.upload.hub import DEFAULT_REPO, Hub, check_token
from signal_archive_recorder.upload.review import preflight, review
from signal_archive_recorder.upload.token import Token, TokenStore, scrub_token
from signal_archive_recorder.upload.validator import latest_verdict

log = logging.getLogger(__name__)


class UploadState(StrEnum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    PR_OPENED = "pr_opened"
    VALIDATED = "validated"
    FAILED = "failed"
    BLOCKED = "blocked"


class NotLoggedInError(RuntimeError):
    pass


@dataclass
class UploadRecord:
    state: UploadState = UploadState.QUEUED
    attempts: int = 0
    last_error: str | None = None
    pr_num: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    path_in_repo: str | None = None
    problems: list[str] = field(default_factory=list)
    validator_messages: list[str] = field(default_factory=list)
    updated_ns: int = 0

    @classmethod
    def load(cls, session_dir: Path) -> UploadRecord:
        path = session_dir / "local" / "upload.json"
        if not path.exists():
            return cls()
        data = json.loads(path.read_text("utf-8"))
        data["state"] = UploadState(data["state"])
        record = cls(**data)
        if record.state is UploadState.UPLOADING:  # interrupted: try again
            record.state = UploadState.QUEUED
        return record

    def save(self, session_dir: Path, now_ns: int) -> None:
        self.updated_ns = now_ns
        (session_dir / "local").mkdir(exist_ok=True)
        data: dict[str, Any] = asdict(self)
        data["state"] = self.state.value
        write_json_atomic(session_dir / "local" / "upload.json", data)


def pr_title(path_in_repo: str) -> str:
    return f"Add session {path_in_repo}"


class Uploader:
    def __init__(
        self,
        storage: SessionStorage,
        hub: Hub,
        tokens: TokenStore,
        consent: ConsentStore,
        clock: Clock,
        *,
        repo_id: str = DEFAULT_REPO,
    ) -> None:
        self.storage = storage
        self.hub = hub
        self.tokens = tokens
        self.consent = consent
        self.clock = clock
        self.repo_id = repo_id

    def sessions(self) -> list[tuple[Path, UploadRecord]]:
        if not self.storage.sessions.is_dir():
            return []
        found = sorted(p for p in self.storage.sessions.iterdir() if (p / "session.json").exists())
        return [(p, UploadRecord.load(p)) for p in found]

    def _token(self) -> Token:
        token = self.tokens.get()
        if token is None:
            raise NotLoggedInError("No Hugging Face token yet. Run: signal-archive-recorder login")
        return token

    def upload(self, session_dir: Path) -> UploadRecord:
        """Upload one session as a pull request, if it isn't already."""
        self.consent.require()
        record = UploadRecord.load(session_dir)
        if record.state in (UploadState.PR_OPENED, UploadState.VALIDATED, UploadState.FAILED):
            return record  # one PR per session
        token = self._token()
        identity = check_token(self.hub, token, self.repo_id)

        problems = preflight(session_dir)
        if problems:
            record.state, record.problems = UploadState.BLOCKED, problems
            record.save(session_dir, self.clock.now_ns())
            return record
        record.problems = []

        path_in_repo = f"contributions/{identity.username}/{session_dir.name}"
        title = pr_title(path_in_repo)
        record.state, record.path_in_repo = UploadState.UPLOADING, path_in_repo
        record.attempts += 1
        record.save(session_dir, self.clock.now_ns())
        try:
            pr = self.hub.find_open_pr(token, self.repo_id, identity.username, title)
            if pr is None:
                pr = self.hub.open_pr(
                    token,
                    self.repo_id,
                    session_dir,
                    path_in_repo,
                    title,
                    self._description(session_dir),
                )
        except Exception as exc:
            record.state = UploadState.QUEUED  # keep everything; retry later
            record.last_error = scrub_token(f"{type(exc).__name__}: {exc}", token)
            record.save(session_dir, self.clock.now_ns())
            log.warning("upload of %s failed: %s", session_dir.name, record.last_error)
            return record
        record.state, record.pr_num, record.pr_url = UploadState.PR_OPENED, pr.num, pr.url
        record.pr_state, record.last_error = "open", None
        record.save(session_dir, self.clock.now_ns())
        log.info("opened %s for %s", pr.url, session_dir.name)
        return record

    def _description(self, session_dir: Path) -> str:
        r = review(session_dir)
        bands = ", ".join(sorted({c.band for c in r.chunks if c.band})) or "unknown"
        modes = ", ".join(sorted({c.mode for c in r.chunks if c.mode})) or "unknown"
        return (
            f"Session {r.session_id}: {len(r.chunks)} chunks, {r.seconds / 60:.1f} minutes.\n\n"
            f"- Bands: {bands}\n- Modes: {modes}\n"
            f"- Decoder labels: {', '.join(r.labels) or 'none'}\n\n"
            "Uploaded by Signal Archive Recorder. Licensed CC BY 4.0."
        )

    def poll(self, session_dir: Path) -> UploadRecord:
        """Follow up an open PR: the validator's verdict, or a merge or close."""
        record = UploadRecord.load(session_dir)
        if record.state is not UploadState.PR_OPENED or record.pr_num is None:
            return record
        status = self.hub.pr_status(self._token(), self.repo_id, record.pr_num)
        record.pr_state = status.state
        verdict = latest_verdict(status.comments)
        if verdict is not None:
            record.state = UploadState.VALIDATED if verdict.passed else UploadState.FAILED
            record.validator_messages = list(verdict.messages)
        elif status.state == "merged":
            record.state = UploadState.VALIDATED
        elif status.state == "closed":
            record.state = UploadState.FAILED
            record.validator_messages = ["The pull request was closed without being merged."]
        record.save(session_dir, self.clock.now_ns())
        return record

    def remove_chunk(self, session_dir: Path, chunk_id: str, builder: Any) -> None:
        """Remove a chunk before upload; once a PR exists, it's too late here."""
        record = UploadRecord.load(session_dir)
        if record.state not in (UploadState.QUEUED, UploadState.BLOCKED):
            raise RuntimeError(
                f"{session_dir.name} is already uploaded ({record.state.value}); ask for removal "
                "on the pull request instead"
            )
        from signal_archive_recorder.upload.review import remove_chunk

        remove_chunk(session_dir, chunk_id, builder)

    def requeue(self, session_dir: Path) -> UploadRecord:
        """After fixing a blocked or failed session, let it upload again."""
        record = UploadRecord.load(session_dir)
        record.state, record.problems = UploadState.QUEUED, []
        record.pr_num = record.pr_url = record.pr_state = None
        record.save(session_dir, self.clock.now_ns())
        return record

    def upload_all(self) -> list[tuple[Path, UploadRecord]]:
        results = []
        for session_dir, record in self.sessions():
            if record.state is UploadState.QUEUED and review(session_dir).finished:
                results.append((session_dir, self.upload(session_dir)))
        return results

    def poll_all(self) -> list[tuple[Path, UploadRecord]]:
        return [(d, self.poll(d)) for d, r in self.sessions() if r.state is UploadState.PR_OPENED]
