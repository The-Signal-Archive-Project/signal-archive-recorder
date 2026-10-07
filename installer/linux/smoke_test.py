# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Check an installed Linux package (.deb or AUR) really works. Standard library only,
so it runs in a bare container next to the installed app.

    python3 installer/linux/smoke_test.py --expect-version 0.3.0

WSJT-X is played by replaying datagrams we captured from it (tests/fixtures/udp).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave
import zipfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / "tests" / "fixtures" / "udp" / "wsjtx-session1"
HEARTBEAT, STATUS, CLOSE = 0, 1, 6
CHILDREN: list[subprocess.Popen[bytes]] = []  # stopped however the test ends


def start(args: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(args, env=env)
    CHILDREN.append(proc)
    return proc


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        raise SystemExit(f"FAILED: {what}")


def wait_for(condition: Callable[[], object], what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise SystemExit(f"FAILED: timed out waiting for {what}")
        time.sleep(0.25)


def first_datagram(kind: int) -> bytes:
    for f in sorted(CAPTURE.glob("*.bin")):
        data = f.read_bytes()
        if struct.unpack(">I", data[8:12])[0] == kind:
            return data
    raise SystemExit(f"no datagram of type {kind} in {CAPTURE}")


def write_wav(path: Path, seconds: int = 20, rate: int = 48_000) -> None:
    frames = bytearray()
    for i in range(seconds * rate):
        frames += struct.pack("<h", int(6000 * math.sin(2 * math.pi * 1500 * i / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def config(work: Path, name: str, port: int, start: str) -> Path:
    path = work / f"{name}.toml"
    path.write_text(
        f"[storage]\nroot = {json.dumps(str(work / name))}\n"
        f"[audio]\nfile = {json.dumps(str(work / 'rx.wav'))}\nfile_loop = true\n"
        'sample_format = "int16"\n'
        f"[wsjtx]\nport = {port}\n[clock]\nenabled = false\n[updates]\ncheck = false\n"
        f'[recording]\nstart = "{start}"\n'
    )
    return path


def sessions(archive: Path) -> list[dict[str, object]]:
    return [json.loads(p.read_text()) for p in sorted(archive.glob("sessions/*/session.json"))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", default="signal-archive-recorder")
    parser.add_argument("--gui", default="signal-archive-recorder-gui")
    parser.add_argument("--expect-version", required=True)
    args = parser.parse_args()
    cli, gui = shutil.which(args.cli), shutil.which(args.gui)
    check(bool(cli and gui), f"both commands installed ({cli}, {gui})")
    assert cli and gui
    work = Path(tempfile.mkdtemp(prefix="sar-smoke-"))
    env = {
        **os.environ,
        "SIGNAL_ARCHIVE_CONFIG_DIR": str(work / "config"),
        "SIGNAL_ARCHIVE_LOG_DIR": str(work / "logs"),
        "QT_QPA_PLATFORM": "offscreen",
    }

    step("version")
    out = subprocess.run([cli, "--version"], env=env, capture_output=True, text=True)
    check(out.stdout.strip() == args.expect_version, f"version {out.stdout.strip()}")

    step("diagnostics: the libraries the app needs at run time")
    subprocess.run([cli, "diagnostics", "--out", str(work / "d.zip")], env=env, check=True)
    with zipfile.ZipFile(work / "d.zip") as z:
        about = z.read("about.txt").decode()
    print(about)
    check("pyside6 6." in about, "Qt for Python loads")
    check("sounddevice 0." in about, "PortAudio loads")
    check("soundfile 0." in about, "libsndfile loads")

    step("record: stand by, WSJT-X appears, record, WSJT-X closes, saved")
    write_wav(work / "rx.wav")
    port = 22381
    archive = work / "standby"
    rec = start(
        [cli, "record", "--config", str(config(work, "standby", port, "with_decoder"))], env=env
    )
    time.sleep(4)
    check(rec.poll() is None and not sessions(archive), "standing by, nothing recorded")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for kind in (HEARTBEAT, STATUS):
        sock.sendto(first_datagram(kind), ("127.0.0.1", port))
    wait_for(lambda: sessions(archive), "recording to start")
    for _ in range(12):  # keep "WSJT-X" alive for a while
        time.sleep(0.5)
        sock.sendto(first_datagram(STATUS), ("127.0.0.1", port))
    sock.sendto(first_datagram(CLOSE), ("127.0.0.1", port))
    wait_for(lambda: sessions(archive) and sessions(archive)[0].get("ended_utc"), "saving")
    rec.send_signal(signal.SIGTERM)
    check(rec.wait(30) == 0, "record exits cleanly on SIGTERM")
    session = sessions(archive)[0]
    check(session["end_reason"] == "decoder_closed", "session ended when WSJT-X closed")
    metas = [json.loads(p.read_text()) for p in archive.glob("sessions/*/recordings/*.meta.json")]
    check(bool(metas) and all(m["audio"]["flac"]["verified"] for m in metas), "FLACs verified")
    check(metas[0]["radio"]["dial_hz"]["value"] is not None, "frequency taken from WSJT-X")

    step("desktop app: records, and a second start defers to it")
    gui_env = {**env, "SIGNAL_ARCHIVE_LOG_DIR": str(work / "gui-logs")}
    gui_conf = config(work, "gui", port + 1, "always")
    app = start([gui, "--config", str(gui_conf)], env=gui_env)
    log = work / "gui-logs" / "recorder.log"
    wait_for(lambda: log.exists() and "recording started" in log.read_text(), "the app to record")
    second = subprocess.run([gui, "--config", str(gui_conf)], env=gui_env, timeout=60)
    check(second.returncode == 0 and "already running" in log.read_text(), "second start defers")
    app.send_signal(signal.SIGTERM)
    check(app.wait(30) == 0, "the desktop app exits cleanly on SIGTERM (logout)")
    check(sessions(work / "gui")[0]["end_reason"] == "signal", "and saved its session")

    step("forget (dry run)")
    out = subprocess.run(
        [cli, "forget", "--everything", "--dry-run"], env=env, capture_output=True, text=True
    )
    check("PERMANENTLY DELETE" in out.stdout, "forget explains what it would delete")
    print("\nSMOKE TEST PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for child in CHILDREN:
            if child.poll() is None:
                child.kill()
