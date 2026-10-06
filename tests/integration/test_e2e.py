# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 7: the whole recorder, end to end."""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pytest
import soundfile as sf

from signal_archive_recorder.audio.device import DeviceInfo
from signal_archive_recorder.audio.flac_writer import array_to_raw, verify_flac
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.clockmon.monitor import NtpAnswer
from signal_archive_recorder.config import AudioConfig, RecorderConfig, WsjtxConfig
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.metadata.builder import _schemas
from signal_archive_recorder.metadata.privacy import machine_secrets
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.recorder import Recorder
from signal_archive_recorder.sources.wsjtx import messages as m
from tests.fakes.fake_audio import FakeBackend, noise

S = 10**9
FMT = AudioFormat(8_000, 1, "int16")
DEVICE = DeviceInfo(0, "Fake Radio Codec SN:00AB12", "fake", 1, 8_000)
OWN_CALL = "W9XYZ"


def utc_ns(hhmmss: str) -> int:
    return int(datetime.fromisoformat(f"2026-10-05T{hhmmss}").replace(tzinfo=UTC).timestamp()) * S


def validator(name: str) -> jsonschema.Draft202012Validator:
    schemas, registry = _schemas()
    return jsonschema.Draft202012Validator(schemas[name], registry=registry)


def fake_ntp(server: str) -> NtpAnswer:
    """Tests never touch the network: a computer clock 12 ms behind NTP."""
    return NtpAnswer(offset_s=0.012, delay_s=0.020, stratum=2)


def make_config(root: Path, **audio: Any) -> RecorderConfig:
    return RecorderConfig(
        storage_root=root,
        audio=AudioConfig(device="Fake Radio", sample_format="int16", **audio),
        station=StationSettings(callsign=OWN_CALL, grid="EN52wa"),
        wsjtx=WsjtxConfig(port=0),
    )


class FakeWsjtx:
    """Sends real WSJT-X datagrams to the recorder's listener over UDP."""

    def __init__(self, recorder: Recorder) -> None:
        assert recorder.listener is not None
        self.recorder = recorder
        self.target = recorder.listener.address
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0

    def send(self, message: m.Heartbeat | m.Status | m.Decode) -> None:
        self.sock.sendto(m.encode(message), self.target)
        self.sent += 1
        listener = self.recorder.listener
        assert listener is not None
        deadline = time.monotonic() + 5
        while listener.stats.datagrams < self.sent:
            assert time.monotonic() < deadline, "listener never received the datagram"
            time.sleep(0.002)
        assert self.recorder.bus is not None and self.recorder.bus.wait_idle()

    def status(self, dial: int, transmitting: bool = False) -> m.Status:
        return m.Status(
            "WSJT-X", dial, "FT8", None, "-15", "FT8", transmitting, transmitting, False, 1500,
            1500, OWN_CALL, "EN52wa", None, False, None, False, 0, None, None, "Default",
            f"CQ {OWN_CALL} EN52" if transmitting else None,
        )  # fmt: skip


def test_e2e_ft8_session(tmp_path: Path) -> None:
    clock = FakeClock(utc_ns("12:03:07"))
    data = noise(FMT, 720, seed=7)  # 12 minutes
    backend = FakeBackend(
        [DEVICE], data, on_block=lambda n: clock.advance(n * S // FMT.sample_rate)
    )
    recorder = Recorder(
        make_config(tmp_path, buffer_seconds=800), backend=backend, clock=clock, ntp_probe=fake_ntp
    )
    session = recorder.start()
    wsjtx = FakeWsjtx(recorder)
    wsjtx.send(m.Heartbeat("WSJT-X", 3, "3.0.2", ""))
    dial = 14_074_000
    wsjtx.send(wsjtx.status(dial))
    assert backend.stream is not None

    tx_cycles = {8, 28, 29}  # 15 s of TX at 2 min, 30 s at 7 min
    for cycle in range(48):  # 48 x 15 s = 12 minutes
        if cycle == 16:  # band change at 4 min (12:07:07)
            dial = 7_074_000
            wsjtx.send(wsjtx.status(dial))
        transmitting = cycle in tx_cycles
        was_transmitting = cycle - 1 in tx_cycles
        if transmitting != was_transmitting:
            wsjtx.send(wsjtx.status(dial, transmitting))
        if not transmitting:
            slot_ms = (clock.now_ns() // 1_000_000) % 86_400_000
            for text in ("CQ K1ABC FN42", f"{OWN_CALL} K1ABC -12"):
                wsjtx.send(m.Decode("WSJT-X", True, slot_ms, -10, 0.2, 1200, "~", text, False,
                                    False))  # fmt: skip
        backend.stream.pump(15 * FMT.sample_rate)
    summary = recorder.stop(reason="test")

    chunks = sorted(summary.chunks, key=lambda c: c["index"])
    assert summary.capture.lost_frames == 0
    # 12:03:07 -> 12:05 -> band change 12:07:07 -> 12:10 -> 12:15 -> end 12:15:07
    assert [c["audio"]["start_frame"] / 8000 for c in chunks] == [0, 113, 240, 413, 713]
    assert [c["time"]["end_reason"] for c in chunks] == [
        "policy", "freq_change", "policy", "policy", "session_end",
    ]  # fmt: skip
    assert chunks[0]["tx_intervals"] == []
    assert chunks[1]["tx_intervals"] == [[(120 - 113) * 8000, (135 - 113) * 8000]]
    assert chunks[2]["tx_intervals"] == []
    assert chunks[3]["tx_intervals"] == [[(420 - 413) * 8000, (450 - 413) * 8000]]

    recordings, labels = session.recordings, session.labels / "wsjtx"
    joined = b""
    for c in chunks:
        flac = c["audio"]["flac"]
        assert flac["verified"] and verify_flac(recordings / flac["file"], FMT, flac["pcm_md5"]).ok
        raw, _ = sf.read(recordings / flac["file"], dtype="int16", always_2d=True)
        joined += array_to_raw(raw, FMT)
    assert joined == data  # every sample, once, in order

    for path in recordings.glob("*.meta.json"):
        validator("chunk").validate(json.loads(path.read_text()))
    session_meta = json.loads(session.session_json.read_text())
    validator("session").validate(session_meta)
    assert session_meta["end_reason"] == "test" and session_meta["labels"] == ["wsjtx"]
    assert session_meta["software"] == {"wsjtx": "WSJT-X 3.0.2"}
    assert session_meta["clock"]["time_source"]["value"] == "ntp"
    assert session_meta["clock"]["checks"][0]["offset_s"] == 0.012
    stats = [json.loads(x) for x in (labels / "chunk_stats.jsonl").read_text().splitlines()]
    for line in stats:
        validator("label_stats").validate(line)
    decodes = [json.loads(x) for x in (labels / "decodes.jsonl").read_text().splitlines()]
    assert len(decodes) == (48 - len(tx_cycles)) * 2
    for line in decodes:
        validator("decode").validate(line)
    assert sum(s["count"]["value"] for s in stats) == len(decodes)
    assert {d["text"] for d in decodes} == {"CQ K1ABC FN42", "<OWN_CALL> K1ABC -12"}

    secrets = [*machine_secrets(), DEVICE.name, "00AB12", OWN_CALL, "EN52wa"]
    for path in session.path.rglob("*"):
        if path.is_file():
            text = path.read_bytes().decode("latin-1").lower()
            for secret in secrets:
                assert secret.lower() not in text, f"{secret!r} in {path.name}"


def _crash_copy(chunk_flac: Path) -> None:
    """Turn a finished chunk into what a crash leaves behind."""
    data = chunk_flac.read_bytes()
    chunk_flac.with_name(chunk_flac.name + ".partial").write_bytes(data[: len(data) * 6 // 10])
    chunk_flac.unlink()
    chunk_flac.with_name(chunk_flac.name.replace(".flac", ".meta.json")).unlink()


def test_restart_recovers_partial(tmp_path: Path) -> None:
    def run(seconds: int, start: str) -> Any:
        clock = FakeClock(utc_ns(start))
        backend = FakeBackend(
            [DEVICE], noise(FMT, seconds), on_block=lambda n: clock.advance(n * S // 8000)
        )
        recorder = Recorder(
            make_config(tmp_path, buffer_seconds=200),
            backend=backend,
            clock=clock,
            ntp_probe=fake_ntp,
        )
        recorder.start()
        assert backend.stream is not None
        backend.stream.pump()
        return recorder, recorder.stop()

    _, first = run(130, "12:03:07")  # two chunks
    session = first.session
    [_, last] = sorted(session.recordings.glob("*.flac"))
    _crash_copy(last)
    (session.recordings / "0009_20261005T121000Z.flac.partial").write_bytes(b"not flac")
    meta = json.loads(session.session_json.read_text())
    meta.update(ended_ns=None, ended_utc=None, end_reason=None)  # it never got to finish
    session.session_json.write_text(json.dumps(meta))

    second, _ = run(5, "13:00:00")
    [recovery_ok, recovery_bad] = sorted(second.recovered, key=lambda r: r.result is None)
    assert recovery_ok.result is not None and recovery_ok.result.recovered
    recovered_meta = json.loads(
        (session.recordings / last.name.replace(".flac", ".meta.json")).read_text()
    )
    validator("chunk").validate(recovered_meta)
    assert recovered_meta["time"]["start_reason"] == "recovered"
    assert recovered_meta["time"]["end_reason"] == "crashed"
    assert recovered_meta["audio"]["flac"]["verified"]
    assert recovery_bad.result is None
    assert sorted(p.name for p in session.recordings.iterdir() if ".flac" in p.name) == sorted(
        [p.name for p in session.recordings.glob("*.flac")]
    )  # only real recordings left; the unrecoverable file went to local/
    assert (session.local / "0009_20261005T121000Z.flac.unrecoverable").exists()
    session_meta = json.loads(session.session_json.read_text())
    validator("session").validate(session_meta)
    assert session_meta["end_reason"] == "crashed"
    assert recovered_meta["chunk_id"] in session_meta["chunks"]


def test_sigterm_mid_chunk(tmp_path: Path) -> None:
    wav = tmp_path / "reference.wav"
    pcm = np.frombuffer(noise(FMT, 60, seed=3), dtype="<i2").reshape(-1, 1)
    sf.write(wav, pcm, 8000, subtype="PCM_16")
    root = tmp_path / "archive"
    config = tmp_path / "recorder.toml"
    config.write_text(
        f"[storage]\nroot = {json.dumps(str(root))}\n"
        f'[audio]\nfile = {json.dumps(str(wav))}\nsample_format = "int16"\n'
        "[wsjtx]\nport = 0\n[clock]\nenabled = false\n"  # no network in tests
        '[recording]\nstart = "always"\n'  # a WAV file: nothing to wait for
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "signal_archive_recorder.cli",
            "record",
            "--config",
            str(config),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=flags,
    )
    assert proc.stdout is not None
    seen = []
    while not (line := proc.stdout.readline()).startswith("recording to"):  # skip log lines
        seen.append(line)
        assert line, "".join(seen)
    time.sleep(2.5)  # mid-chunk
    proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)  # type: ignore[attr-defined]
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out

    [session_dir] = (root / "sessions").iterdir()
    session = json.loads((session_dir / "session.json").read_text())
    assert session["ended_ns"] is not None and session["end_reason"] == "signal"
    # Real wall clock: a run that crosses a 5-minute boundary is split there, so allow two.
    metas = sorted((session_dir / "recordings").glob("*.meta.json"))
    chunks = [json.loads(p.read_text()) for p in metas]
    assert 1 <= len(chunks) <= 2
    assert [c["time"]["end_reason"] for c in chunks][-1] == "session_end"
    assert all(c["time"]["end_reason"] == "policy" for c in chunks[:-1])
    assert 1.5 * 8000 <= sum(c["audio"]["sample_count"] for c in chunks) <= 10 * 8000
    audio = b""
    for chunk in chunks:
        assert chunk["audio"]["flac"]["verified"]
        flac = session_dir / "recordings" / chunk["audio"]["flac"]["file"]
        assert verify_flac(flac, FMT, chunk["audio"]["flac"]["pcm_md5"]).ok
        raw, _ = sf.read(flac, dtype="int16", always_2d=True)
        audio += array_to_raw(raw, FMT)
    assert hashlib.md5(audio).digest() == hashlib.md5(pcm.tobytes()[: len(audio)]).digest()


def test_missing_device_fails_cleanly(tmp_path: Path) -> None:
    from signal_archive_recorder.audio.device import DeviceUnavailableError

    config = RecorderConfig(storage_root=tmp_path, audio=AudioConfig(device="Nope"))
    with pytest.raises(DeviceUnavailableError, match="Fake Radio Codec"):
        Recorder(config, backend=FakeBackend([DEVICE])).start()
    assert not (tmp_path / "sessions").exists() or not any((tmp_path / "sessions").iterdir())


def test_no_labels_without_a_decoder(tmp_path: Path) -> None:
    """WSJT-X listening but never running: no empty labels/ folder, no label source."""
    clock = FakeClock(utc_ns("12:03:07"))
    backend = FakeBackend([DEVICE], noise(FMT, 5), on_block=lambda n: clock.advance(n * S // 8000))
    recorder = Recorder(
        make_config(tmp_path, buffer_seconds=60), backend=backend, clock=clock, ntp_probe=fake_ntp
    )
    session = recorder.start()
    assert backend.stream is not None
    backend.stream.pump()
    recorder.stop()
    assert not session.labels.exists()
    assert json.loads(session.session_json.read_text())["labels"] == []
