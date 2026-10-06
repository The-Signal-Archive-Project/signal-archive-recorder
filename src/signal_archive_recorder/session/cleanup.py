# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Keeping the local archive within its limits, without ever losing unsent work.

Only **confirmed** sessions can be deleted: those the intake validator passed or a
maintainer merged (upload state `validated`). Everything else (recording, queued,
uploading, waiting for review, failed, blocked, kept back) is never touched.

Two rules, both off by default:
- `delete_after_days`: delete confirmed sessions this long after confirmation.
- `max_gb`: if the archive is bigger, delete confirmed sessions, oldest first,
  until it fits. If that isn't enough, say so: the rest isn't safe to delete.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.upload.queue import UploadRecord, UploadState

log = logging.getLogger(__name__)

GB = 1_000_000_000
DAY_NS = 86_400 * 1_000_000_000


@dataclass
class CleanupReport:
    deleted: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    total_bytes: int = 0  # archive size afterwards
    warning: str | None = None


def folder_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def cleanup(
    storage: SessionStorage,
    *,
    now_ns: int,
    max_bytes: int | None = None,
    delete_after_days: float | None = None,
    dry_run: bool = False,
) -> CleanupReport:
    report = CleanupReport()
    if not storage.sessions.is_dir():
        return report
    sessions = sorted(p for p in storage.sessions.iterdir() if p.is_dir())  # oldest first
    sizes = {p: folder_size(p) for p in sessions}
    confirmed = [p for p in sessions if UploadRecord.load(p).state is UploadState.VALIDATED]

    def delete(path: Path) -> None:
        report.deleted.append(path.name)
        report.freed_bytes += sizes[path]
        if not dry_run:
            assert path.parent == storage.sessions  # never anything outside the archive
            shutil.rmtree(path)
            log.info("deleted confirmed session %s (%d bytes)", path.name, sizes[path])

    if delete_after_days:
        for path in confirmed:
            confirmed_ns = UploadRecord.load(path).updated_ns
            if now_ns - confirmed_ns >= delete_after_days * DAY_NS:
                delete(path)
    total = sum(sizes.values()) - report.freed_bytes
    if max_bytes:
        for path in confirmed:
            if total <= max_bytes:
                break
            if path.name not in report.deleted:
                delete(path)
                total -= sizes[path]
        if total > max_bytes:
            report.warning = (
                f"The archive is {total / GB:.1f} GB, over its {max_bytes / GB:.1f} GB limit, "
                "and nothing else is safe to delete (sessions are only deleted once their "
                "upload is confirmed). Upload and wait for review, or raise the limit."
            )
    report.total_bytes = total
    return report
