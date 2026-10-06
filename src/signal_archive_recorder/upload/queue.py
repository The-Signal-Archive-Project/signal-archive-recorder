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
    pr_missing                       (the PR was deleted on Hugging Face; re-queue to resend)

Local files are never deleted here. A failed attempt leaves everything in place
and the session queued. Before opening a PR, an existing open PR for the same
session is looked for first, so a crash between "PR created" and "state saved"
can't create a duplicate.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from signal_archive_recorder.core.clock import Clock
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.modes.registry import ModeRegistry
from signal_archive_recorder.session.storage import SessionStorage, write_json_atomic
from signal_archive_recorder.upload.consent import ConsentStore
from signal_archive_recorder.upload.hub import (
    DEFAULT_REPO,
    Hub,
    PrNotFoundError,
    PrRef,
    check_token,
)
from signal_archive_recorder.upload.review import preflight, remove_chunk, review, upload_files
from signal_archive_recorder.upload.screening import save as save_screening
from signal_archive_recorder.upload.screening import screen_session
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
    PR_MISSING = "pr_missing"  # the PR (or the repository) was deleted on Hugging Face


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
    excluded: list[dict[str, Any]] = field(default_factory=list)  # chunks screening kept back
    sent_files: list[str] = field(default_factory=list)  # confirmed in the PR so far
    failures: int = 0  # consecutive failed attempts, for backoff
    next_attempt_ns: int = 0  # automatic retries wait until then
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


@dataclass(frozen=True)
class DryRun:
    repo_id: str
    path_in_repo: str
    title: str
    files: list[tuple[str, int]]
    problems: list[str]
    username: str | None
    excluded: list[tuple[str, list[str]]] = field(default_factory=list)
    warnings: list[tuple[str, list[str]]] = field(default_factory=list)


BACKOFF_BASE_S = 60.0
BACKOFF_MAX_S = 6 * 3600.0


def describe_result(name: str, record: UploadRecord) -> tuple[list[str], bool]:
    """What happened to one session's upload, as lines of text, and whether it needs a look."""
    lines = [f"{name}: kept back {kept['chunk_id']}: {'; '.join(kept['reasons'])}"
             for kept in record.excluded]  # fmt: skip
    if record.state is UploadState.PR_OPENED:
        lines.append(f"{name}: pull request opened: {record.pr_url}")
        return lines, False
    if record.state is UploadState.VALIDATED:
        lines.append(f"{name}: already uploaded and confirmed")
        return lines, False
    if record.state is UploadState.BLOCKED:
        lines.append(f"{name}: not uploaded, problems found:")
        lines.extend(f"  - {p}" for p in record.problems)
        return lines, True
    lines.append(f"{name}: {record.state.value} ({record.last_error}); will retry")
    return lines, True


def plan_steps(session_dir: Path) -> list[list[str]]:
    """The order a session goes up in: each chunk, then labels and session.json.

    session.json goes last, so its presence in the PR means the upload is complete.
    """
    files = [p.relative_to(session_dir).as_posix() for p in upload_files(session_dir)]
    chunks: dict[str, list[str]] = {}
    for f in files:
        if f.startswith("recordings/"):
            chunks.setdefault(f.split("/", 1)[1].split(".", 1)[0], []).append(f)
    last = [f for f in files if not f.startswith("recordings/")]
    last.sort(key=lambda f: f == "session.json")
    return [sorted(chunks[c]) for c in sorted(chunks)] + ([last] if last else [])


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
        registry: ModeRegistry | None = None,
        builder: MetadataBuilder | None = None,
        require_decoder: bool = True,
        max_bytes_per_s: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_bytes_per_s = max_bytes_per_s
        self.sleep = sleep
        self.storage = storage
        self.registry = registry or ModeRegistry.load_default()
        self.builder = builder or MetadataBuilder(self.registry, StationSettings())
        self.require_decoder = require_decoder
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

        screens = screen_session(session_dir, self.registry, require_decoder=self.require_decoder)
        save_screening(session_dir, screens)
        excluded = [s for s in screens if not s.eligible]
        if screens and len(excluded) == len(screens):
            # Nothing here looks like radio audio: keep it all local and say why.
            record.state = UploadState.BLOCKED
            record.problems = [f"{s.chunk_id}: {'; '.join(s.reasons)}" for s in excluded]
            record.save(session_dir, self.clock.now_ns())
            return record
        for s in excluded:
            remove_chunk(session_dir, s.chunk_id, self.builder, into="excluded", why=s.reasons)
            record.excluded.append({"chunk_id": s.chunk_id, "reasons": s.reasons})

        path_in_repo = f"contributions/{identity.username}/{session_dir.name}"
        title = pr_title(path_in_repo)
        record.state, record.path_in_repo = UploadState.UPLOADING, path_in_repo
        record.attempts += 1
        record.save(session_dir, self.clock.now_ns())
        try:
            self._send(session_dir, record, token, identity.username, path_in_repo, title)
        except Exception as exc:
            # Keep everything, including the PR if one was opened: the next attempt
            # resumes from the files the PR already has.
            record.state = UploadState.QUEUED
            record.last_error = scrub_token(f"{type(exc).__name__}: {exc}", token)
            record.failures += 1
            delay = min(BACKOFF_BASE_S * 2 ** (record.failures - 1), BACKOFF_MAX_S)
            record.next_attempt_ns = self.clock.now_ns() + int(delay * 1e9)
            record.save(session_dir, self.clock.now_ns())
            log.warning("upload of %s failed (retry in %d s): %s", session_dir.name, delay,
                        record.last_error)  # fmt: skip
            return record
        record.state, record.pr_state, record.last_error = UploadState.PR_OPENED, "open", None
        record.failures, record.next_attempt_ns = 0, 0
        record.save(session_dir, self.clock.now_ns())
        log.info("uploaded %s as %s", session_dir.name, record.pr_url)
        return record

    def _send(
        self,
        session_dir: Path,
        record: UploadRecord,
        token: Token,
        username: str,
        path_in_repo: str,
        title: str,
    ) -> None:
        """Send the session in steps, skipping whatever the PR already has."""
        pr: PrRef | None = None
        if record.pr_num is not None and record.pr_url is not None:
            pr = PrRef(record.pr_num, record.pr_url)
        else:
            pr = self.hub.find_open_pr(token, self.repo_id, username, title)
        present = self.hub.pr_files(token, self.repo_id, pr.num) if pr else set()
        for step in plan_steps(session_dir):
            remaining = [f for f in step if f"{path_in_repo}/{f}" not in present]
            if not remaining:
                continue
            started = self.clock.monotonic_ns()
            if pr is None:
                pr = self.hub.open_pr(token, self.repo_id, session_dir, path_in_repo, title,
                                      self._description(session_dir), remaining)  # fmt: skip
                record.pr_num, record.pr_url = pr.num, pr.url
            else:
                self.hub.add_to_pr(token, self.repo_id, pr.num, session_dir, path_in_repo,
                                   remaining, f"Add {', '.join(remaining)}")  # fmt: skip
            record.sent_files = sorted(set(record.sent_files) | set(remaining))
            record.save(session_dir, self.clock.now_ns())
            self._pace(sum((session_dir / f).stat().st_size for f in remaining), started)
        if pr is not None and record.pr_num is None:  # everything was already there
            record.pr_num, record.pr_url = pr.num, pr.url

    def _pace(self, sent_bytes: int, started_ns: int) -> None:
        """Wait long enough that the average stays under the bandwidth cap."""
        if not self.max_bytes_per_s:
            return
        elapsed = (self.clock.monotonic_ns() - started_ns) / 1e9
        wait = sent_bytes / self.max_bytes_per_s - elapsed
        if wait > 0:
            self.sleep(wait)

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
        token = self._token()
        try:
            status = self.hub.pr_status(token, self.repo_id, record.pr_num)
        except PrNotFoundError:
            record.state, record.pr_state = UploadState.PR_MISSING, "missing"
            record.validator_messages = [
                f"Pull request #{record.pr_num} no longer exists on {self.repo_id}. To send "
                f"this session again: signal-archive-recorder requeue {session_dir.name}"
            ]
            record.save(session_dir, self.clock.now_ns())
            return record
        except Exception as exc:  # offline, Hugging Face down: try again next time
            record.last_error = scrub_token(f"{type(exc).__name__}: {exc}", token)
            record.save(session_dir, self.clock.now_ns())
            log.warning("couldn't check %s: %s", session_dir.name, record.last_error)
            return record
        record.pr_state, record.last_error = status.state, None
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
        remove_chunk(session_dir, chunk_id, builder)

    def requeue(self, session_dir: Path) -> UploadRecord:
        """After fixing a blocked or failed session, let it upload again."""
        record = UploadRecord.load(session_dir)
        record.state, record.problems = UploadState.QUEUED, []
        record.pr_num = record.pr_url = record.pr_state = record.last_error = None
        record.validator_messages, record.sent_files = [], []
        record.failures = record.next_attempt_ns = 0
        record.save(session_dir, self.clock.now_ns())
        return record

    def dry_run(self, session_dir: Path) -> DryRun:
        """Everything an upload would do, except sending anything."""
        self.consent.require()
        token = self.tokens.get()
        username = check_token(self.hub, token, self.repo_id).username if token else None
        files = [(p.relative_to(session_dir).as_posix(), p.stat().st_size)
                 for p in upload_files(session_dir)]  # fmt: skip
        path = f"contributions/{username or '<your-hf-username>'}/{session_dir.name}"
        screens = screen_session(session_dir, self.registry, require_decoder=self.require_decoder)
        excluded = [(s.chunk_id, s.reasons) for s in screens if not s.eligible]
        held = {chunk_id for chunk_id, _ in excluded}
        files = [(n, size) for n, size in files if n.split("/")[-1].split(".")[0] not in held]
        problems = preflight(session_dir)
        if screens and len(excluded) == len(screens):
            problems = problems + [f"{c}: {'; '.join(r)}" for c, r in excluded]
        warnings = [(s.chunk_id, s.warnings) for s in screens if s.warnings and s.eligible]
        return DryRun(self.repo_id, path, pr_title(path), files, problems, username,
                      excluded, warnings)  # fmt: skip

    def upload_all(self) -> list[tuple[Path, UploadRecord]]:
        """Upload every finished, queued session whose backoff has passed."""
        results = []
        now = self.clock.now_ns()
        for session_dir, record in self.sessions():
            if record.next_attempt_ns > now:
                continue
            if record.state is UploadState.QUEUED and review(session_dir).finished:
                results.append((session_dir, self.upload(session_dir)))
        return results

    def poll_all(self) -> list[tuple[Path, UploadRecord]]:
        return [(d, self.poll(d)) for d, r in self.sessions() if r.state is UploadState.PR_OPENED]
