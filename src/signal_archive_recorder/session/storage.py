# SPDX-License-Identifier: Apache-2.0
"""On-disk layout of a session:

    <root>/sessions/<session_id>/
        session.json
        <chunk_id>.flac            (or .flac.partial while being written)
        <chunk_id>.meta.json
        <source>_decodes.jsonl     (e.g. wsjtx_decodes.jsonl)

Session ids are the UTC start time, so folders sort chronologically:
20261005T120307Z, with a -2, -3 suffix if one already exists.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NS_PER_S = 1_000_000_000


def utc_compact(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns // NS_PER_S, UTC).strftime("%Y%m%dT%H%M%SZ")


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to a temporary file, fsync it, then rename it into place."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class SessionDir:
    session_id: str
    path: Path

    @property
    def session_json(self) -> Path:
        return self.path / "session.json"

    def chunk_id(self, index: int, first_sample_ns: int) -> str:
        return f"{index:04d}_{utc_compact(first_sample_ns)}"

    def flac(self, chunk_id: str) -> Path:
        return self.path / f"{chunk_id}.flac"

    def meta(self, chunk_id: str) -> Path:
        return self.path / f"{chunk_id}.meta.json"

    def decode_log(self, source: str) -> Path:
        return self.path / f"{source}_decodes.jsonl"


class SessionStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.sessions = root / "sessions"

    def create(self, start_ns: int) -> SessionDir:
        self.sessions.mkdir(parents=True, exist_ok=True)
        base = utc_compact(start_ns)
        for n in range(1, 1000):
            session_id = base if n == 1 else f"{base}-{n}"
            path = self.sessions / session_id
            try:
                path.mkdir()
            except FileExistsError:
                continue
            return SessionDir(session_id, path)
        raise RuntimeError(f"too many sessions starting at {base}")
