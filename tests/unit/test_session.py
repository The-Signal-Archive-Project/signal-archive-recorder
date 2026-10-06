# SPDX-License-Identifier: Apache-2.0
"""Stage 5: session manager and chunker, driven by a fake sound card and fake clock."""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import soundfile as sf

from signal_archive_recorder.audio.flac_writer import array_to_raw, verify_flac
from signal_archive_recorder.audio.timeline import StreamTimeline
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import (
    Decode,
    FreqChanged,
    ModeChanged,
    SourceUp,
    Stamped,
    TxEnded,
    TxStarted,
)
from signal_archive_recorder.session.manager import SessionManager
from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.sources.base import SupervisedSource
from tests.fakes.fake_audio import noise
from tests.fakes.session_rig import FMT, REGISTRY, Rig, S, frames, utc_ns


def stream_seconds(chunk: dict[str, Any], key: str = "start_frame") -> float:
    return chunk["audio"][key] / FMT.sample_rate


def read_raw(folder: Path, chunk: dict[str, Any]) -> bytes:
    data, _ = sf.read(folder / chunk["audio"]["flac"]["file"], dtype="int16", always_2d=True)
    return array_to_raw(data, FMT)


def assert_all_verified(rig: Rig, chunks: list[dict[str, Any]]) -> None:
    for c in chunks:
        assert c["audio"]["flac"]["verified"], c["chunk_id"]
        assert verify_flac(
            rig.session.recordings / c["audio"]["flac"]["file"], FMT, c["audio"]["flac"]["pcm_md5"]
        ).ok
    joined = b"".join(read_raw(rig.session.recordings, c) for c in chunks)
    assert joined == rig.data, "chunks don't concatenate to the original stream"


def wsjtx_up(rig: Rig, mode: str = "ft8", dial: int = 14_074_000) -> None:
    rig.publish(SourceUp(source="wsjtx", detail="WSJT-X test"))
    rig.publish(FreqChanged(source="wsjtx", dial_hz=dial))
    rig.publish(ModeChanged(source="wsjtx", mode_id=mode, raw_mode=mode.upper()))


# -- exit tests ---------------------------------------------------------------


def test_first_chunk_aligned(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    wsjtx_up(rig)
    chunks = rig.finish()
    bounds = [(c["audio"]["start_frame"], c["audio"]["end_frame"]) for c in chunks]
    assert bounds == [
        (0, frames(113)),  # 12:03:07 -> 12:05:00
        (frames(113), frames(413)),  # 12:05 -> 12:10
        (frames(413), frames(713)),  # 12:10 -> 12:15
        (frames(713), frames(720)),  # session end at 12:15:07
    ]
    assert [c["time"]["end_reason"] for c in chunks] == ["policy"] * 3 + ["session_end"]
    assert chunks[1]["time"]["first_sample_ns"] == utc_ns("12:05:00")
    assert (
        chunks[0]["mode"]["mode_id"]["value"] == "ft8"
        and chunks[0]["radio"]["dial_hz"]["value"] == 14_074_000
    )
    assert_all_verified(rig, chunks)


def test_first_sample_utc_exact(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=400)
    chunks = rig.finish()
    one_sample = S // FMT.sample_rate
    for c in chunks:
        expected = utc_ns("12:03:07") + c["audio"]["first_sample_frame"] * S // FMT.sample_rate
        assert abs(c["time"]["first_sample_ns"] - expected) <= one_sample
        assert c["audio"]["sample_count"] == c["audio"]["end_frame"] - c["audio"]["start_frame"]


def test_split_on_freq_change(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=150)
    wsjtx_up(rig)
    rig.pump_to(70)
    rig.publish(FreqChanged(source="wsjtx", dial_hz=7_074_000))
    chunks = rig.finish()
    assert [stream_seconds(c) for c in chunks] == [0, 70, 113]
    assert [c["time"]["end_reason"] for c in chunks] == ["freq_change", "policy", "session_end"]
    assert [c["radio"]["dial_hz"]["value"] for c in chunks] == [14_074_000, 7_074_000, 7_074_000]
    assert_all_verified(rig, chunks)


def test_split_on_mode_change(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=100)
    wsjtx_up(rig)
    rig.pump_to(42.5)
    rig.publish(ModeChanged(source="wsjtx", mode_id="ft4", raw_mode="FT4"))
    chunks = rig.finish()
    assert [stream_seconds(c) for c in chunks] == [0, 42.5]
    assert [c["mode"]["mode_id"]["value"] for c in chunks] == ["ft8", "ft4"]
    assert chunks[0]["time"]["end_reason"] == "mode_change"
    assert_all_verified(rig, chunks)


def test_same_value_again_does_not_split(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=60)
    wsjtx_up(rig)
    rig.pump_to(30)
    rig.publish(FreqChanged(source="wsjtx", dial_hz=14_074_000))
    assert len(rig.finish()) == 1


def test_mode_change_changes_policy(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=600)  # 12:03:07 -> 12:13:07
    wsjtx_up(rig)
    rig.pump_to(53)  # 12:04:00
    rig.publish(ModeChanged(source="wsjtx", mode_id="wspr", raw_mode="WSPR"))
    chunks = rig.finish()
    starts = [datetime.fromtimestamp(c["time"]["first_sample_ns"] / S, UTC) for c in chunks]
    assert [t.strftime("%H:%M:%S") for t in starts] == [
        "12:03:07",
        "12:04:00",
        "12:06:00",
        "12:12:00",
    ]
    assert [c["mode"]["mode_id"]["value"] for c in chunks] == ["ft8", "wspr", "wspr", "wspr"]
    assert_all_verified(rig, chunks)


def test_reported_period_drives_policy(tmp_path: Path) -> None:
    rig = Rig(tmp_path, start="12:00:00", seconds=400)
    rig.publish(SourceUp(source="wsjtx"))
    rig.publish(ModeChanged(source="wsjtx", mode_id="q65", raw_mode="Q65", period_s=120.0))
    chunks = rig.finish()
    assert stream_seconds(chunks[1]) == 360  # Q65-120: 6-minute chunks
    assert chunks[0]["mode"]["params"]["period_s"] == 120.0


def test_tx_intervals(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=150)
    wsjtx_up(rig)
    rig.pump_to(10)
    rig.publish(TxStarted(source="wsjtx"))
    rig.pump_to(25)
    rig.publish(TxEnded(source="wsjtx"))
    rig.pump_to(100)  # 12:04:47, and TX across the 12:05:00 boundary
    rig.publish(TxStarted(source="wsjtx"))
    rig.pump_to(133)
    rig.publish(TxEnded(source="wsjtx"))
    chunks = rig.finish()
    assert chunks[0]["tx_intervals"] == [[frames(10), frames(25)], [frames(100), frames(113)]]
    assert chunks[1]["tx_intervals"] == [[0, frames(20)]]
    # Audio keeps recording through transmit.
    assert sum(c["audio"]["sample_count"] for c in chunks) == frames(150)
    assert_all_verified(rig, chunks)


def test_source_crash_isolated(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=600)
    crash_now = threading.Event()
    published = threading.Event()
    runs = {"n": 0}

    def flaky(stop: threading.Event) -> None:
        runs["n"] += 1
        if runs["n"] == 1:  # first run works, then dies on cue
            rig.bus.publish(SourceUp(source="flaky"))
            rig.bus.publish(FreqChanged(source="flaky", dial_hz=10_136_000))
            rig.bus.publish(ModeChanged(source="flaky", mode_id="ft8", raw_mode="FT8"))
            published.set()
            crash_now.wait(10)
        raise RuntimeError("decoder fell over")  # and every restart fails immediately

    source = SupervisedSource("flaky", flaky, rig.bus, backoff_s=0.01, max_backoff_s=0.05)
    source.start()
    assert published.wait(5)
    rig.bus.wait_idle()
    rig.pump_to(200)  # 12:06:27: chunk 1 started at 12:05 with the source up
    crash_now.set()
    while source.crashes == 0:
        time.sleep(0.01)
    rig.bus.wait_idle()
    chunks = rig.finish()
    source.stop()

    assert runs["n"] > 1  # the supervisor kept restarting it
    assert_all_verified(rig, chunks)  # audio is complete and verified regardless
    assert chunks[1]["mode"]["mode_id"]["value"] == "ft8"
    down = chunks[1]["sources"]["flaky"]["down"]
    assert len(down) == 1 and down[0][0] == frames(200 - 113)
    assert down[0][2].startswith("crashed: RuntimeError")
    later = chunks[2]  # started at 12:10 while the source was down
    assert later["mode"]["mode_id"] == {
        "value": None,
        "reason": "source_unavailable",
        "source": "flaky",
    }
    assert later["radio"]["dial_hz"]["reason"] == "source_unavailable"
    assert later["sources"]["flaky"]["up_at_start"] is False


def test_sample_count_for_drift(tmp_path: Path) -> None:
    """A sound card 100 ppm fast: chunk sizes follow its samples, never corrected."""
    rig = Rig(tmp_path, seconds=450)  # chunk 1 runs 113 s -> 413 s
    rig.rate_hz = FMT.sample_rate * (1 + 100e-6)
    chunks = rig.finish()
    assert chunks[1]["audio"]["sample_count"] == frames(300)  # nominal, not adjusted
    sync = chunks[1]["audio"]["sync_points"]
    assert len(sync) >= 250  # about one per second (taken at block boundaries)
    (f0, t0), (f1, t1) = sync[0], sync[-1]
    nominal_ns = (f1 - f0) * S / FMT.sample_rate
    assert (t1 - t0) / nominal_ns == pytest.approx(1 - 100e-6, abs=5e-6)  # drift is visible


def test_decode_counts_split_live_and_off_air(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=30)
    wsjtx_up(rig)
    rig.pump_to(10)
    for off_air in (False, False, True):
        rig.publish(Decode(source="wsjtx", mode_id="ft8", text="CQ TEST", off_air=off_air))
    [chunk] = rig.finish()
    stats = rig.label_stats()[chunk["chunk_id"]]
    assert stats["count"]["value"] == 2
    assert stats["off_air_count"] == 1
    assert "decodes" not in chunk  # decoder output never sits in recording metadata


def test_session_json_and_files(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=130)
    chunks = rig.finish()
    folder = rig.session.path
    assert folder == tmp_path / "sessions" / "20261005T120307Z"
    session = json.loads((folder / "session.json").read_text())
    assert session["started_ns"] == utc_ns("12:03:07")
    assert session["ended_ns"] is not None and session["end_reason"] == "stopped"
    assert session["chunks"] == [c["chunk_id"] for c in chunks]
    assert chunks[0]["chunk_id"] == "0000_20261005T120307Z"
    top = sorted(p.name for p in folder.iterdir())
    assert top == ["recordings", "session.json"]  # no labels: no decoder ran
    recordings = sorted(p.name for p in (folder / "recordings").iterdir())
    expected = [f"{c['chunk_id']}{ext}" for c in chunks for ext in (".flac", ".meta.json")]
    assert recordings == sorted(expected)
    assert session["labels"] == []


# -- direct-feed tests (no capture thread) ------------------------------------


def direct(tmp_path: Path, holdback_s: float = 0.0) -> tuple[SessionManager, StreamTimeline]:
    clock = FakeClock(utc_ns("12:00:00"))
    timeline = StreamTimeline(FMT.sample_rate)
    timeline.anchor(0, utc_ns("12:00:00"))
    manager = SessionManager(
        storage=SessionStorage(tmp_path),
        fmt=FMT,
        registry=REGISTRY,
        clock=clock,
        bus=EventBus(clock),
        timeline=timeline,
        holdback_s=holdback_s,
    )
    manager.start()
    return manager, timeline


def test_gap_spanning_boundary_keeps_schedule(tmp_path: Path) -> None:
    manager, _ = direct(tmp_path)
    block = noise(FMT, 1.0)
    manager.write(block * 290, 0)  # 12:00:00 - 12:04:50
    manager.write(block * 60, frames(320))  # 30 s lost across 12:05:00, then to 12:06:20
    chunks = sorted(manager.close(), key=lambda c: c["index"])
    assert [(stream_seconds(c), stream_seconds(c, "end_frame")) for c in chunks] == [
        (0, 300),
        (300, 380),
    ]
    assert chunks[0]["audio"]["sample_count"] == frames(290)
    assert chunks[1]["audio"]["first_sample_frame"] == frames(320)


def test_late_event_flagged(tmp_path: Path) -> None:
    manager, timeline = direct(tmp_path)
    manager.write(noise(FMT, 30), 0)  # committed at once (no holdback)
    late = Stamped(timeline.frame_to_ns(frames(10)), 1, FreqChanged(source="x", dial_hz=1))
    manager.on_event(late)
    manager.write(noise(FMT, 10), frames(30))
    chunks = sorted(manager.close(), key=lambda c: c["index"])
    assert [stream_seconds(c) for c in chunks] == [0, 30]  # split where it was noticed
    assert chunks[1]["events"][0]["late"] is True


def test_captured_wsjtx_traffic_drives_chunks(tmp_path: Path) -> None:
    """The real listener, replaying datagrams captured from WSJT-X, steers the chunker."""
    from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener

    folder = Path(__file__).resolve().parents[1] / "fixtures" / "udp" / "wsjtx-session1"
    index = [json.loads(line) for line in (folder / "index.jsonl").open()]
    datagram = {e["seq"]: (folder / e["file"]).read_bytes() for e in index}

    rig = Rig(tmp_path, seconds=120)
    decode_log = rig.session.decode_log("wsjtx")
    listener = WsjtxListener(rig.bus, REGISTRY, rig.clock, decode_log=decode_log)

    def replay(*seqs: int) -> None:
        for seq in seqs:
            listener.handle(datagram[seq])
        assert rig.bus.wait_idle()

    replay(2, 0)  # heartbeat, then status: FT8 on 20 m
    rig.pump_to(40)
    replay(33)  # band change to 40 m
    rig.pump_to(80)
    replay(59, 60)  # FT4 (mode, then its dial frequency)
    rig.pump_to(90)
    replay(*range(120, 125))  # five FT4 decodes from the sample file (off air)
    replay(144)  # Tune on
    rig.pump_to(95)
    replay(145)  # Tune off
    chunks = rig.finish()

    assert [stream_seconds(c) for c in chunks] == [0, 40, 80, 113]  # + 12:05:00 boundary
    assert [c["time"]["end_reason"] for c in chunks] == [
        "freq_change",
        "freq_change+mode_change",  # WSJT-X sent FT4 and its frequency together
        "policy",
        "session_end",
    ]
    dials = [c["radio"]["dial_hz"]["value"] for c in chunks]
    assert dials == [14_074_000, 7_074_000, 7_047_500, 7_047_500]
    assert [c["mode"]["mode_id"]["value"] for c in chunks] == ["ft8", "ft8", "ft4", "ft4"]
    stats = rig.label_stats()[chunks[2]["chunk_id"]]
    assert stats["count"]["value"] == 0
    assert stats["off_air_count"] == 5
    assert chunks[2]["tx_intervals"] == [[frames(10), frames(15)]]
    assert len(decode_log.read_text().splitlines()) == 5
    assert_all_verified(rig, chunks)


def test_labels_never_mix_with_recordings(tmp_path: Path) -> None:
    """Third-party decoder output lives under labels/, never beside or inside recordings."""
    from signal_archive_recorder.sources.wsjtx import messages as m
    from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener

    rig = Rig(tmp_path, seconds=30)
    listener = WsjtxListener(
        rig.bus, REGISTRY, rig.clock, decode_log=rig.session.decode_log("wsjtx")
    )
    wsjtx_up(rig)
    rig.pump_to(10)
    decode = m.Decode("WSJT-X", True, 1000, -7, 0.2, 1200, "~", "CQ LABEL1 FN42", False, False)
    listener.handle(m.encode(decode))
    rig.bus.wait_idle()
    chunks = rig.finish()

    recordings = sorted(p.name for p in rig.session.recordings.iterdir())
    assert all(n.endswith((".flac", ".meta.json")) for n in recordings)
    for path in rig.session.recordings.iterdir():
        assert b"LABEL1" not in path.read_bytes(), path.name
    for chunk in chunks:
        assert not {"decodes", "labels"} & set(chunk)
    labels = sorted(str(p.relative_to(rig.session.labels)) for p in rig.session.labels.rglob("*"))
    assert labels == ["wsjtx", "wsjtx/chunk_stats.jsonl", "wsjtx/decodes.jsonl"]
    assert "LABEL1" in rig.session.decode_log("wsjtx").read_text()
    session = json.loads(rig.session.session_json.read_text())
    assert session["labels"] == ["wsjtx"]
