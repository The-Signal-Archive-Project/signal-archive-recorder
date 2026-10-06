# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Startup recovery of sessions that ended in a crash.

Every `.flac.partial` under `sessions/*/recordings/` is recovered (see
audio.flac_recovery). A recovered chunk gets metadata describing only what can be
known from the audio itself, marked `recovered` / `crashed`, and its session's
session.json is updated. Files that can't be recovered move to the session's
`local/` folder, so `recordings/` only ever holds usable recordings.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import soundfile as sf

from signal_archive_recorder.audio.flac_recovery import Recovery, recover_partials
from signal_archive_recorder.audio.flac_writer import array_to_raw
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.audio.levels import LevelMeter
from signal_archive_recorder.metadata import rights
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.session.storage import SessionStorage, write_json_atomic

log = logging.getLogger(__name__)

UNAVAILABLE = {"value": None, "reason": "source_unavailable"}


def recover_storage(storage: SessionStorage, builder: MetadataBuilder) -> list[Recovery]:
    results: list[Recovery] = []
    if not storage.sessions.is_dir():
        return results
    for session_dir in sorted(p for p in storage.sessions.iterdir() if p.is_dir()):
        recordings = session_dir / "recordings"
        if not recordings.is_dir():
            continue
        found = recover_partials(recordings)
        recovered = []
        for r in found:
            if r.result is None:
                _move_to_local(session_dir, recordings)
                continue
            try:
                meta = _recovered_meta(session_dir.name, r, builder)
            except Exception:
                log.exception("could not describe recovered chunk %s", r.result.path.name)
                continue
            write_json_atomic(recordings / f"{meta['chunk_id']}.meta.json", meta)
            recovered.append(meta["chunk_id"])
        if found:
            _mark_crashed(session_dir / "session.json", recovered, builder)
        results.extend(found)
    return results


def _move_to_local(session_dir: Path, recordings: Path) -> None:
    local = session_dir / "local"
    local.mkdir(exist_ok=True)
    for bad in recordings.glob("*.unrecoverable"):
        os.replace(bad, local / bad.name)


def _recovered_meta(session_id: str, r: Recovery, builder: MetadataBuilder) -> dict[str, Any]:
    assert r.result is not None
    path = r.result.path
    chunk_id = path.name.removesuffix(".flac")
    info = sf.info(str(path))
    fmt = AudioFormat(
        int(info.samplerate), int(info.channels), "int16" if info.subtype == "PCM_16" else "int24"
    )
    meter = LevelMeter(fmt)
    with sf.SoundFile(str(path)) as f:
        credited = f.artist  # the credit recorded at the time, not today's settings
        dtype = "int16" if fmt.sample_format == "int16" else "int32"
        while len(block := f.read(65_536, dtype=dtype, always_2d=True)):
            meter.update(array_to_raw(block, fmt))
    record = {
        "chunk_id": chunk_id,
        "session_id": session_id,
        "index": int(chunk_id.split("_", 1)[0]),
        "audio": {
            "start_reason": "recovered",
            "end_reason": "crashed",
            "start_frame": 0,
            "end_frame": r.result.frames,
            "first_sample_frame": None,  # the timeline was lost with the crash
            "first_sample_ns": None,
            "sample_count": r.result.frames,
            "sync_points": [],
            "sample_rate": fmt.sample_rate,
            "channels": fmt.channels,
            "sample_format": fmt.sample_format,
            "gaps": [],
            "levels": asdict(meter.snapshot()),
        },
        "flac": {
            "file": path.name,
            "pcm_md5": r.result.pcm_md5,
            "sha256": r.result.sha256,
            "verified": r.result.verified,
        },
        "mode": dict(UNAVAILABLE),
        "dial_hz": dict(UNAVAILABLE),
        "settings": {},
        "tx_intervals": [],
        "sources": {},
        "events": [],
    }
    if credited:
        record["rights"] = {**rights.rights(builder.settings), "attribution": credited}
    return builder.chunk(record)


def _mark_crashed(session_json: Path, recovered: list[str], builder: MetadataBuilder) -> None:
    if not session_json.exists():
        return
    session = json.loads(session_json.read_text("utf-8"))
    if session.get("ended_ns") is None:
        session["end_reason"] = "crashed"
    session["chunks"] = sorted(set(session.get("chunks", [])) | set(recovered))
    builder.validate_session(session)
    write_json_atomic(session_json, session)
