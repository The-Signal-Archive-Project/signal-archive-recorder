# SPDX-License-Identifier: Apache-2.0
"""What a session would share, removing chunks, and checks before upload.

Review shows exactly the files and identifying fields that would be published.
Removing a chunk moves its audio and metadata (and the labels from its time
window) into local/removed/, which is never uploaded. Preflight re-verifies every
FLAC and re-validates every JSON file immediately before upload.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from signal_archive_recorder.audio.flac_recovery import parse_stream_info
from signal_archive_recorder.audio.flac_writer import sha256_file, verify_flac
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.metadata.builder import MetadataBuilder, _schemas
from signal_archive_recorder.metadata.privacy import machine_secrets
from signal_archive_recorder.session.storage import write_json_atomic
from signal_archive_recorder.upload.hub import matches


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChunkSummary:
    chunk_id: str
    start_utc: str | None
    seconds: float
    mode: str | None
    band: str | None
    dial_hz: int | None
    verified: bool
    tx_seconds: float


@dataclass(frozen=True)
class Review:
    session_id: str
    started_utc: str
    ended_utc: str | None
    finished: bool
    chunks: list[ChunkSummary]
    callsign: str | None
    grid: str | None
    hf_username: str | None
    labels: list[str]
    files: list[tuple[str, int]] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return sum(c.seconds for c in self.chunks)

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.files)


def _load(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def upload_files(session_dir: Path) -> list[Path]:
    """Every file in the session that an upload would send."""
    return sorted(
        p
        for p in session_dir.rglob("*")
        if p.is_file() and matches(p.relative_to(session_dir).as_posix())
    )


def review(session_dir: Path) -> Review:
    session = _load(session_dir / "session.json")
    chunks = []
    for chunk_id in session["chunks"]:
        meta_path = session_dir / "recordings" / f"{chunk_id}.meta.json"
        if not meta_path.exists():
            continue
        meta = _load(meta_path)
        audio = meta["audio"]
        rate = audio["sample_rate"]
        chunks.append(
            ChunkSummary(
                chunk_id=chunk_id,
                start_utc=meta["time"]["first_sample_utc"],
                seconds=audio["sample_count"] / rate,
                mode=meta["mode"]["mode_id"]["value"],
                band=meta["radio"]["band"]["value"],
                dial_hz=meta["radio"]["dial_hz"]["value"],
                verified=bool(audio["flac"] and audio["flac"]["verified"]),
                tx_seconds=sum(e - s for s, e in meta["tx_intervals"]) / rate,
            )
        )
    return Review(
        session_id=session["session_id"],
        started_utc=session["started_utc"],
        ended_utc=session["ended_utc"],
        finished=session["ended_utc"] is not None or session["end_reason"] == "crashed",
        chunks=chunks,
        callsign=session["operator"]["callsign"]["value"],
        grid=session["location"]["grid"]["value"],
        hf_username=session["operator"]["hf_username"],
        labels=session.get("labels", []),
        files=[
            (p.relative_to(session_dir).as_posix(), p.stat().st_size)
            for p in upload_files(session_dir)
        ],
    )


def format_review(r: Review) -> str:
    lines = [
        f"Session {r.session_id}: {r.started_utc} to {r.ended_utc or '(still recording)'}",
        f"  {len(r.chunks)} chunks, {r.seconds / 60:.1f} minutes, "
        f"{r.total_bytes / 1e6:.1f} MB in {len(r.files)} files",
        f"  Bands: {', '.join(sorted({c.band for c in r.chunks if c.band})) or 'unknown'}",
        f"  Modes: {', '.join(sorted({c.mode for c in r.chunks if c.mode})) or 'unknown'}",
        "  Shared about you:",
        f"    callsign: {r.callsign or 'not shared'}",
        f"    grid: {r.grid or 'not shared'}",
        f"    Hugging Face user: {r.hf_username or '(the account that uploads)'}",
        f"  Decoder labels: {', '.join(r.labels) or 'none'} (kept separate from recordings)",
        "  Chunks:",
    ]
    for c in r.chunks:
        tx = f", {c.tx_seconds:.0f} s TX" if c.tx_seconds else ""
        state = "" if c.verified else "  NOT VERIFIED"
        band, mode = c.band or "-", c.mode or "-"
        lines.append(f"    {c.chunk_id}  {c.seconds:6.1f} s  {band:>5}  {mode:>6}{tx}{state}")
    return "\n".join(lines)


def remove_chunk(session_dir: Path, chunk_id: str, builder: MetadataBuilder) -> None:
    """Move a chunk, and the labels from its time window, out of the upload."""
    recordings = session_dir / "recordings"
    meta_path = recordings / f"{chunk_id}.meta.json"
    if not meta_path.exists():
        raise ReviewError(f"No chunk {chunk_id} in this session")
    meta = _load(meta_path)
    removed = session_dir / "local" / "removed"
    removed.mkdir(parents=True, exist_ok=True)
    for path in recordings.glob(f"{chunk_id}.*"):
        os.replace(path, removed / path.name)

    start = meta["time"]["first_sample_ns"]
    end = None
    if start is not None:
        audio = meta["audio"]
        end = start + audio["sample_count"] * 1_000_000_000 // audio["sample_rate"]
    for source_dir in sorted((session_dir / "labels").glob("*")):
        _filter_jsonl(
            source_dir / "chunk_stats.jsonl", removed / f"{source_dir.name}_chunk_stats.jsonl",
            lambda line: line.get("chunk_id") == chunk_id,
        )  # fmt: skip
        if start is not None and end is not None:
            _filter_jsonl(
                source_dir / "decodes.jsonl", removed / f"{source_dir.name}_decodes.jsonl",
                lambda line: start <= line.get("received_ns", -1) < end,
            )  # fmt: skip

    session_path = session_dir / "session.json"
    session = _load(session_path)
    session["chunks"] = [c for c in session["chunks"] if c != chunk_id]
    builder.validate_session(session)
    write_json_atomic(session_path, session)


def _filter_jsonl(path: Path, removed_to: Path, drop: Any) -> None:
    if not path.exists():
        return
    keep: list[str] = []
    gone: list[str] = []
    for raw in path.read_text("utf-8").splitlines():
        if raw.strip():
            (gone if drop(json.loads(raw)) else keep).append(raw)
    if gone:
        with removed_to.open("a", encoding="utf-8") as f:
            f.write("".join(x + "\n" for x in gone))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(x + "\n" for x in keep), "utf-8")
        tmp.replace(path)


def preflight(session_dir: Path, extra_secrets: tuple[str, ...] = ()) -> list[str]:
    """Problems that must stop an upload; an empty list means it's fine to send."""
    problems: list[str] = []
    schemas, registry = _schemas()

    def check(name: str, doc: Any, where: str) -> None:
        validator = jsonschema.Draft202012Validator(schemas[name], registry=registry)
        for error in validator.iter_errors(doc):
            problems.append(f"{where}: {error.message}")
            return

    session_path = session_dir / "session.json"
    if not session_path.exists():
        return ["session.json is missing"]
    session = _load(session_path)
    check("session", session, "session.json")
    if session.get("ended_utc") is None and session.get("end_reason") != "crashed":
        problems.append("the session is still recording")

    listed = set(session.get("chunks", []))
    present = {
        p.name.removesuffix(".meta.json") for p in (session_dir / "recordings").glob("*.meta.json")
    }
    for missing in sorted(listed - present):
        problems.append(f"{missing}: listed in session.json but missing")
    for extra in sorted(present - listed):
        problems.append(f"{extra}: not listed in session.json")

    for chunk_id in sorted(listed & present):
        meta = _load(session_dir / "recordings" / f"{chunk_id}.meta.json")
        check("chunk", meta, f"{chunk_id}.meta.json")
        flac = meta.get("audio", {}).get("flac")
        if not flac:
            continue
        path = session_dir / "recordings" / flac["file"]
        if not path.exists() or not flac["verified"]:
            problems.append(f"{chunk_id}: FLAC missing or failed verification when written")
            continue
        audio = meta["audio"]
        fmt = AudioFormat(audio["sample_rate"], audio["channels"],
                          "int16" if audio["bit_depth"] == 16 else "int24")  # fmt: skip
        if not verify_flac(path, fmt, flac["pcm_md5"]).ok:
            problems.append(f"{chunk_id}: audio no longer matches its checksum")
        elif sha256_file(path) != flac["sha256"]:
            problems.append(f"{chunk_id}: file changed since it was recorded")

    for stats in sorted(session_dir.glob("labels/*/chunk_stats.jsonl")):
        for line in stats.read_text("utf-8").splitlines():
            check("label_stats", json.loads(line), stats.relative_to(session_dir).as_posix())
    for decodes in sorted(session_dir.glob("labels/*/decodes.jsonl")):
        for line in decodes.read_text("utf-8").splitlines():
            check("decode", json.loads(line), decodes.relative_to(session_dir).as_posix())

    secrets = [s.lower() for s in (*machine_secrets(), *extra_secrets) if len(s) >= 3]
    for path in upload_files(session_dir):
        data = path.read_bytes()
        if path.suffix == ".flac":
            # Only the header and tags can hold text; scanning compressed audio for short
            # strings would find them by chance.
            data = data[: parse_stream_info(data).audio_offset]
        text = data.decode("latin-1").lower()
        if any(s in text for s in secrets):
            problems.append(
                f"{path.relative_to(session_dir).as_posix()}: contains a private detail"
            )
    return problems
