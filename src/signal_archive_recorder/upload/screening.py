# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Is this chunk really the radio? Checks that keep room audio out of the dataset.

Three checks per chunk, none of which decodes anything:

1. **A decoder was running.** The chunk's mode came from a decoder (WSJT-X and
   friends). With none, there's no evidence the input is a radio at all.
2. **The audio contains what the decoder heard.** For each live decode, the
   decoder said when (its slot) and where (its audio frequency) a signal was. A
   real recording of the same receiver shows extra energy in that narrow band at
   that time, compared with the neighbouring frequencies; a microphone or the
   wrong input doesn't. Measured against WSJT-X's own sample recordings, every
   decode down to -17 dB SNR stood out by at least +1.9 dB, while the same
   positions in noise stayed within +/-0.5 dB. A live laptop microphone showed 1
   of 20 decodes (excluded); the real recording showed 20 of 20.
3. **It sounds like a receiver.** A rig's audio is band-limited by its filter, so
   there's little energy above about 4 kHz; a room microphone hears everything.
   SDRs and wide filters break this, so on its own it only warns.

A chunk is excluded when check 1 fails (unless decoders aren't required), when
check 2 finds the decodes missing, or when check 2 can't tell and check 3 looks
like a microphone. Excluded chunks stay on this computer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import soundfile as sf

from signal_archive_recorder.modes.registry import ModeRegistry

MIN_DECODE_SNR_DB = -18.0  # weaker decodes don't reliably stand out
VISIBLE_DB = 1.5  # band energy above the neighbours that counts as "there"
MIN_VISIBLE_FRACTION = 0.5
MIN_DECODES_TO_JUDGE = 2
# Receiver audio has far more energy at 300-2700 Hz than above 4 kHz. Measured: a real FT8
# recording 64.5 dB, a laptop microphone 14.3 dB. Tune as real recordings come in.
PASSBAND_WARN_DB = 20.0
RESOLUTION_HZ = 3.125
NS = 1_000_000_000


@dataclass
class ChunkScreen:
    chunk_id: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)  # why it's excluded
    warnings: list[str] = field(default_factory=list)
    decodes_checked: int = 0
    decodes_visible: int = 0
    passband_ratio_db: float | None = None


def _spectrum(seg: npt.NDArray[np.float64], sr: int) -> tuple[Any, Any] | None:
    n = int(sr / RESOLUTION_HZ)
    if len(seg) < n:
        return None
    window = np.hanning(n)
    frames = [seg[i : i + n] * window for i in range(0, len(seg) - n + 1, n // 2)]
    power = np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)
    return np.fft.rfftfreq(n, 1 / sr), power


def band_excess_db(
    audio: npt.NDArray[np.float64], sr: int, df_hz: float, bw_hz: float, t0_s: float, t1_s: float
) -> float | None:
    """Energy in [df, df+bw] over [t0, t1], in dB above the median of its neighbours."""
    if t0_s < 0 or t1_s * sr > len(audio) or df_hz - 300 < 0 or df_hz + bw_hz + 300 > sr / 2:
        return None
    spec = _spectrum(audio[int(t0_s * sr) : int(t1_s * sr)], sr)
    if spec is None:
        return None
    f, p = spec
    signal = p[(f >= df_hz - 3) & (f <= df_hz + bw_hz + 3)]
    near = ((f >= df_hz - 300) & (f < df_hz - 60)) | (
        (f > df_hz + bw_hz + 60) & (f <= df_hz + bw_hz + 300)
    )
    reference = float(np.median(p[near]))
    if not len(signal) or reference <= 0:
        return None
    return float(10 * np.log10(float(signal.mean()) / reference))


def passband_ratio_db(audio: npt.NDArray[np.float64], sr: int) -> float | None:
    """Energy at 300-2700 Hz over energy at 4-12 kHz; None if the sample rate can't show it.

    Averaged over three 10 s slices (start, middle, end) so long chunks stay cheap.
    """
    high_top = min(12_000.0, sr / 2 - 500)
    if high_top <= 5_000:
        return None
    width = min(len(audio), 10 * sr)
    starts = sorted({0, (len(audio) - width) // 2, len(audio) - width})
    spectra = [_spectrum(audio[i : i + width], sr) for i in starts]
    usable = [s for s in spectra if s is not None]
    if not usable:
        return None
    f = usable[0][0]
    p = np.mean([s[1] for s in usable], axis=0)
    low = float(p[(f >= 300) & (f <= 2700)].sum())
    high = float(p[(f >= 4000) & (f <= high_top)].sum())
    if low <= 0:
        return None
    return float(10 * np.log10(low / max(high, low * 1e-12)))


def judge(
    *,
    mode_known: bool,
    require_decoder: bool,
    checked: int,
    visible: int,
    passband_db: float | None,
) -> tuple[bool, list[str], list[str]]:
    """(eligible, reasons, warnings) from the three measurements."""
    reasons: list[str] = []
    warnings: list[str] = []
    if not mode_known:
        message = "no decoder (e.g. WSJT-X) was running, so nothing shows this is radio audio"
        (reasons if require_decoder else warnings).append(message)
    microphone_like = passband_db is not None and passband_db < PASSBAND_WARN_DB
    if microphone_like:
        warnings.append(
            f"lots of energy above 4 kHz ({passband_db:.0f} dB below the voice band); "
            "receiver audio usually has very little, a microphone has plenty"
        )
    if checked >= MIN_DECODES_TO_JUDGE:
        if visible < MIN_VISIBLE_FRACTION * checked:
            reasons.append(
                f"only {visible} of {checked} decoded signals are present in this audio: "
                "the recorder is probably on a different input than the decoder"
            )
    elif mode_known:
        warnings.append("too few decodes to confirm the audio matches the decoder")
        if microphone_like:
            reasons.append("couldn't confirm radio audio, and it sounds like a microphone")
    return not reasons, reasons, warnings


def _load(path: Path) -> tuple[npt.NDArray[np.float64], int]:
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return data.mean(axis=1), int(sr)


def _decodes(session_dir: Path) -> list[dict[str, Any]]:
    lines = []
    for log in sorted(session_dir.glob("labels/*/decodes.jsonl")):
        for raw in log.read_text("utf-8").splitlines():
            if raw.strip():
                line = json.loads(raw)
                if not line.get("off_air") and line.get("decoder_time_ns") is not None:
                    lines.append(line)
    return lines


def screen_chunk(
    meta: dict[str, Any],
    audio: npt.NDArray[np.float64],
    sr: int,
    decodes: list[dict[str, Any]],
    registry: ModeRegistry,
    *,
    require_decoder: bool = True,
) -> ChunkScreen:
    mode_id = meta["mode"]["mode_id"]["value"]
    first_ns = meta["time"]["first_sample_ns"]
    checked = visible = 0
    timing = registry.get(mode_id).signal if mode_id else None
    bandwidth = registry.get(mode_id).nominal_bw_hz if mode_id else None
    contiguous = not meta["audio"]["gaps"]
    if timing and bandwidth and first_ns is not None and contiguous:
        end_ns = first_ns + len(audio) * NS // sr
        for d in decodes:
            if d.get("mode_id") != mode_id or not first_ns <= d["decoder_time_ns"] < end_ns:
                continue
            if (d.get("snr_db") or -99) < MIN_DECODE_SNR_DB or d.get("df_hz") is None:
                continue
            start = (d["decoder_time_ns"] - first_ns) / NS + timing.start_s + (d.get("dt_s") or 0)
            excess = band_excess_db(
                audio, sr, d["df_hz"], bandwidth,
                start + 0.2 * timing.duration_s, start + 0.8 * timing.duration_s,
            )  # fmt: skip
            if excess is None:
                continue
            checked += 1
            visible += excess >= VISIBLE_DB
    ratio = passband_ratio_db(audio, sr)
    eligible, reasons, warnings = judge(
        mode_known=mode_id is not None,
        require_decoder=require_decoder,
        checked=checked,
        visible=visible,
        passband_db=ratio,
    )
    return ChunkScreen(meta["chunk_id"], eligible, reasons, warnings, checked, visible,
                       None if ratio is None else round(ratio, 1))  # fmt: skip


def screen_session(
    session_dir: Path, registry: ModeRegistry, *, require_decoder: bool = True
) -> list[ChunkScreen]:
    session = json.loads((session_dir / "session.json").read_text("utf-8"))
    decodes = _decodes(session_dir)
    results = []
    for chunk_id in session["chunks"]:
        meta_path = session_dir / "recordings" / f"{chunk_id}.meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        flac = meta["audio"]["flac"]
        if not flac:
            results.append(ChunkScreen(chunk_id, False, ["no audio was written for this chunk"]))
            continue
        try:
            audio, sr = _load(session_dir / "recordings" / flac["file"])
        except Exception as exc:  # corrupt or missing: preflight reports the details
            results.append(ChunkScreen(chunk_id, False, [f"the audio can't be read ({exc})"]))
            continue
        results.append(
            screen_chunk(meta, audio, sr, decodes, registry, require_decoder=require_decoder)
        )
    return results


def save(session_dir: Path, results: list[ChunkScreen]) -> None:
    (session_dir / "local").mkdir(exist_ok=True)
    data = [asdict(r) for r in results]
    (session_dir / "local" / "screening.json").write_text(json.dumps(data, indent=2), "utf-8")


def format_screens(results: list[ChunkScreen]) -> list[str]:
    """The radio-audio verdicts as lines of text (for `review` and the review window)."""
    lines = ["  Radio-audio checks:"]
    for s in results:
        mark = "ok      " if s.eligible else "KEEP BACK"
        evidence = f"{s.decodes_visible}/{s.decodes_checked} decodes found in the audio"
        lines.append(f"    {mark} {s.chunk_id}  ({evidence})")
        lines.extend(f"             - {note}" for note in s.reasons + s.warnings)
    return lines
