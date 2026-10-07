# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Install the built Setup.exe, check the installed app really works, uninstall it.

Run on Windows (CI) from the repository root, with the app also installed in the
Python environment (the fake WSJT-X emitter uses it):

    python installer/windows/smoke_test.py dist/SignalArchiveRecorder-<v>-Setup.exe

Checks the packaged app, not the source tree: the things packaging can silently
lose (the keyring backend, PortAudio and libsndfile, Qt's plugins, package data).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib.metadata import version
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Signal Archive Recorder"
GUI = APP_DIR / "SignalArchiveRecorder.exe"
CLI = APP_DIR / "signal-archive-recorder.exe"
PORT = 22371


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def run(args: list[str], env: dict[str, str], **kw: object) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120, **kw)
    print(result.stdout, result.stderr, sep="\n", flush=True)
    return result


def wait_for(condition: object, what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise SystemExit(f"FAILED: timed out waiting for {what}")
        time.sleep(0.5)


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        raise SystemExit(f"FAILED: {what}")


def main(setup: Path) -> int:
    work = Path(tempfile.mkdtemp(prefix="sar-smoke-"))
    env = {
        **os.environ,
        "SIGNAL_ARCHIVE_CONFIG_DIR": str(work / "config"),
        "SIGNAL_ARCHIVE_LOG_DIR": str(work / "logs"),
    }

    step("install (silent, per user, no start at login)")
    subprocess.run(
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/TASKS="],
        check=True,
        timeout=300,
    )
    check(GUI.exists() and CLI.exists(), f"both programs installed in {APP_DIR}")
    check((APP_DIR / "licenses").is_dir() and (APP_DIR / "NOTICE").exists(), "licenses shipped")

    step("version")
    out = run([str(CLI), "--version"], env)
    check(out.stdout.strip() == version("signal-archive-recorder"), "version matches the build")

    step("diagnostics: the libraries packaging can lose")
    zip_path = work / "diag.zip"
    run([str(CLI), "diagnostics", "--out", str(zip_path)], env)
    with zipfile.ZipFile(zip_path) as z:
        about = z.read("about.txt").decode()
        inputs = z.read("audio-inputs.txt").decode()
    check("packaged True" in about, "running as the packaged app")
    check("keyring backend WinVaultKeyring" in about, "Windows Credential Manager reachable")
    check("pyside6 6." in about, "Qt for Python bundled")
    check("sounddevice 0." in about and "soundfile 0." in about, "PortAudio and libsndfile load")
    print(inputs)

    step("record: stand by, WSJT-X appears, record, WSJT-X closes, saved")
    t = np.arange(48_000 * 30) / 48_000
    wav = work / "rx.wav"
    sf.write(wav, 0.2 * np.sin(2 * np.pi * 1500 * t), 48_000, subtype="PCM_24")
    archive = work / "archive"
    config = work / "recorder.toml"
    config.write_text(
        f"[storage]\nroot = {json.dumps(str(archive))}\n"
        f"[audio]\nfile = {json.dumps(str(wav))}\nfile_loop = true\n"
        f"[wsjtx]\nport = {PORT}\n[clock]\nenabled = false\n[updates]\ncheck = false\n",
        encoding="utf-8",
    )
    recorder = subprocess.Popen(
        [str(CLI), "record", "--config", str(config)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    time.sleep(5)
    check(recorder.poll() is None and not archive.exists(), "standing by, nothing recorded")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "fake_wsjtx_emitter.py"),
            "--port",
            str(PORT),
            "--cycles",
            "2",
        ],
        check=True,
        timeout=120,
    )
    wait_for(
        lambda: (
            list(archive.glob("sessions/*/session.json"))
            and json.loads(next(archive.glob("sessions/*/session.json")).read_text())["ended_utc"]
        ),
        "the session to be saved",
    )
    recorder.send_signal(signal.CTRL_BREAK_EVENT)
    print(recorder.communicate(timeout=60)[0])
    check(recorder.returncode == 0, "record exits cleanly on Ctrl-Break")
    session = json.loads(next(archive.glob("sessions/*/session.json")).read_text())
    check(session["end_reason"] == "decoder_closed", "session ended when WSJT-X closed")
    metas = [json.loads(p.read_text()) for p in archive.glob("sessions/*/recordings/*.meta.json")]
    check(bool(metas) and all(m["audio"]["flac"]["verified"] for m in metas), "FLACs verified")
    check(metas[0]["mode"]["mode_id"]["value"] == "ft8", "labelled FT8 from WSJT-X")

    step("desktop app: starts, records, and a second start defers to it")
    # Its own archive and log, so nothing from the step above can satisfy a check here.
    gui_archive = work / "gui-archive"
    gui_config = work / "gui.toml"
    gui_config.write_text(
        f"[storage]\nroot = {json.dumps(str(gui_archive))}\n"
        f"[audio]\nfile = {json.dumps(str(wav))}\nfile_loop = true\n"
        f"[wsjtx]\nport = {PORT + 1}\n[clock]\nenabled = false\n[updates]\ncheck = false\n"
        '[recording]\nstart = "always"\n',
        encoding="utf-8",
    )
    gui_env = {
        **env,
        "QT_QPA_PLATFORM": "offscreen",
        "SIGNAL_ARCHIVE_LOG_DIR": str(work / "gui-logs"),
    }
    gui = subprocess.Popen([str(GUI), "tray", "--config", str(gui_config)], env=gui_env)
    log = work / "gui-logs" / "recorder.log"
    wait_for(lambda: log.exists() and "recording started" in log.read_text(), "the app to record")
    second = subprocess.Popen([str(GUI), "tray", "--config", str(gui_config)], env=gui_env)
    try:
        second.wait(timeout=60)
    except subprocess.TimeoutExpired:
        second.kill()
        gui.kill()
        print(log.read_text(), flush=True)
        raise SystemExit("FAILED: the second start didn't exit (log above)") from None
    check(
        second.returncode == 0 and "already running" in log.read_text(),
        "a second start shows the running app instead",
    )
    check(gui.poll() is None, "the first is still recording")
    check(len(list(gui_archive.glob("sessions/*"))) == 1, "only one copy recorded")
    gui.kill()

    step("forget (dry run)")
    out = run([str(CLI), "forget", "--everything", "--dry-run"], env)
    check("PERMANENTLY DELETE" in out.stdout, "forget explains what it would delete")

    step("uninstall (silent: keeps everything)")
    uninstaller = APP_DIR / "unins000.exe"
    subprocess.run(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        check=True,
        timeout=300,
    )
    wait_for(lambda: not CLI.exists(), "the program files to go", timeout=60)
    check(archive.exists(), "recordings kept")
    print("\nSMOKE TEST PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
