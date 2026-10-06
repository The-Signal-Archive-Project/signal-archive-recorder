# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The first-run setup wizard.

Steps: the contribution terms, Hugging Face login (required, with a permission
check), the audio input (recommended list and a level check), station details, a
WSJT-X check and a clock check. Nothing is written until the end, so stopping
part-way (Ctrl-C) leaves no half-made configuration behind.
"""

from __future__ import annotations

import getpass
import math
import platform
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from signal_archive_recorder.audio.device import (
    AudioBackend,
    DeviceInfo,
    DeviceUnavailableError,
    open_input,
)
from signal_archive_recorder.audio.levels import LevelMeter
from signal_archive_recorder.clockmon.monitor import NtpProbe, status_for
from signal_archive_recorder.core.clock import Clock, SystemClock
from signal_archive_recorder.firstrun.config_writer import SetupChoices, write
from signal_archive_recorder.firstrun.devices import LINUX_TIP, rank
from signal_archive_recorder.metadata.settings import normalise_callsign, normalise_grid
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.upload.consent import CONSENT_TEXT, ConsentStore
from signal_archive_recorder.upload.hub import DEFAULT_REPO, Hub, HubError, check_token
from signal_archive_recorder.upload.token import KeyringUnavailableError, Token, TokenStore

TOKEN_HELP = f"""\
To upload, the recorder needs a Hugging Face access token. To make one:
  1. Sign in (or sign up, free) at https://huggingface.co
  2. Open https://huggingface.co/settings/tokens and choose "Create new token"
  3. Pick "Write" (simplest), or "Fine-grained" with write access to
     {DEFAULT_REPO}
  4. Copy the token (it starts with hf_) and paste it below.
The token is kept in your system's keyring, never in a file.
"""
WSJTX_HELP = """\
In WSJT-X: File > Settings > Reporting > UDP Server: 127.0.0.1, port 2237.
  If GridTracker or JTAlert already uses that port, set a multicast address such as
  224.0.0.1 in WSJT-X, and the same address as [wsjtx] group in the recorder's config."""


class SetupCancelled(Exception):
    """The operator stopped setup; nothing was written."""


class Prompter(Protocol):
    def say(self, text: str = "") -> None: ...

    def ask(self, question: str, default: str = "") -> str: ...

    def secret(self, question: str) -> str: ...


class TerminalPrompter:
    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{question}{suffix}: ").strip()
        return answer or default

    def secret(self, question: str) -> str:
        return getpass.getpass(f"{question}: ").strip()


def yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


@dataclass
class Station:
    callsign: str | None = None
    share_callsign: bool = False
    grid: str | None = None
    grid_precision: int = 4


@dataclass(frozen=True)
class LevelReport:
    peak_dbfs: float
    rms_dbfs: float
    clipped: int
    seconds: float

    @property
    def verdict(self) -> str:
        if math.isinf(self.peak_dbfs) or self.peak_dbfs < -70:
            return "silent: check the radio is on and its audio reaches this input"
        if self.clipped:
            return "clipping: turn the radio's USB/line audio level down"
        if self.peak_dbfs > -1:
            return "very hot: a little less radio audio level would be safer"
        return "good"


def measure_levels(backend: AudioBackend, device: DeviceInfo, seconds: float = 3.0) -> LevelReport:
    """Listen to an input briefly, in shared mode like a real recording."""
    meters: dict[str, LevelMeter] = {}
    frames = {"n": 0}

    def on_audio(data: memoryview, n: int, overflow: bool) -> None:
        meters["m"].update(bytes(data))
        frames["n"] += n

    opened = open_input(backend, device, on_audio)
    meters["m"] = LevelMeter(opened.delivered)
    opened.stream.start()
    try:
        time.sleep(seconds)
    finally:
        opened.stream.stop()
        opened.stream.close()
    snap = meters["m"].snapshot()
    return LevelReport(max(snap.peak_dbfs), max(snap.rms_dbfs), sum(snap.clipped),
                       frames["n"] / opened.delivered.sample_rate)  # fmt: skip


def listen_for_wsjtx(
    port: int = 2237, seconds: float = 16.0, clock: Clock | None = None
) -> str | None:
    """WSJT-X's version if it sends a heartbeat in time; None otherwise. Receive only."""
    clock = clock or SystemClock()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return "busy"  # another program is listening there already
        sock.settimeout(0.5)
        deadline = clock.monotonic_ns() + int(seconds * 1e9)
        while clock.monotonic_ns() < deadline:
            try:
                data = sock.recv(65_535)
            except TimeoutError:
                continue
            try:
                msg = m.parse(data)
            except m.ParseError:
                continue
            if isinstance(msg, m.Heartbeat):
                return f"{msg.client_id} {msg.version or ''}".strip()
            if isinstance(msg, m.Status):
                return msg.client_id
    return None


@dataclass
class Wizard:
    prompt: Prompter
    backend_factory: Callable[[], AudioBackend]
    hub_factory: Callable[[], Hub]
    tokens: TokenStore
    consent: ConsentStore
    now_ns: Callable[[], int]
    ntp_probe: NtpProbe | None = None
    wsjtx_probe: Callable[[], str | None] = listen_for_wsjtx
    level_probe: Callable[[AudioBackend, DeviceInfo], LevelReport] = measure_levels
    system: str = field(default_factory=platform.system)
    repo_id: str = DEFAULT_REPO

    def run(self, config_path: Path) -> SetupChoices:
        p = self.prompt
        p.say("Welcome to Signal Archive Recorder.")
        p.say("It records raw receive audio from your station for the Signal Archive Project,")
        p.say("an open library of real radio signals for building better decoders.")
        p.say("Setup takes a few minutes. Press Ctrl-C at any time to stop; nothing is saved")
        p.say("until the end.")
        p.say()
        self.step_consent()
        self.step_login()
        device, rate = self.step_audio()
        station = self.step_station()
        self.step_wsjtx()
        self.step_clock()
        choices = SetupChoices(
            device=device.name,
            sample_rate=rate,
            callsign=station.callsign,
            share_callsign=station.share_callsign,
            grid=station.grid,
            grid_precision=station.grid_precision,
        )
        write(config_path, choices)
        p.say()
        p.say(f"Setup complete. Your settings are in {config_path}")
        return choices

    # -- steps --------------------------------------------------------------------

    def step_consent(self) -> None:
        p = self.prompt
        current = self.consent.load()
        p.say("Step 1 of 6: contribution terms")
        if current is not None and current.is_current():
            p.say("  You've already agreed to the current terms.")
            p.say()
            return
        p.say(CONSENT_TEXT)
        answer = p.ask('Type "I agree" to accept, or anything else to stop')
        if answer.strip().lower() != "i agree":
            raise SetupCancelled("Not accepted: setup stopped and nothing was saved.")
        self.consent.accept(self.now_ns())
        p.say("  Accepted.")
        p.say()

    def step_login(self) -> None:
        p = self.prompt
        p.say("Step 2 of 6: Hugging Face login")
        hub = self.hub_factory()
        try:
            existing = self.tokens.get()
        except KeyringUnavailableError as exc:
            raise SetupCancelled(str(exc)) from None
        if existing is not None:
            try:
                identity = check_token(hub, existing, self.repo_id)
                p.say(f"  Already logged in as {identity.username}.")
                p.say()
                return
            except HubError as exc:
                p.say(f"  The stored token no longer works ({exc}). Let's add a new one.")
        p.say(TOKEN_HELP)
        while True:
            raw = p.secret("Paste your token (input is hidden; leave empty to stop)")
            if not raw:
                raise SetupCancelled("No token: setup stopped and nothing was saved.")
            try:
                token = Token(raw)
                identity = check_token(hub, token, self.repo_id)
            except (ValueError, HubError) as exc:
                p.say(f"  {exc}")
                p.say("  Please try another token.")
                continue
            try:
                self.tokens.set(token)
            except KeyringUnavailableError as exc:
                raise SetupCancelled(str(exc)) from None
            p.say(f"  Logged in as {identity.username}; the token can open pull requests.")
            p.say()
            return

    def step_audio(self) -> tuple[DeviceInfo, int | None]:
        p = self.prompt
        p.say("Step 3 of 6: audio input (the radio's receive audio)")
        try:
            backend = self.backend_factory()
            devices = backend.input_devices()
        except (DeviceUnavailableError, OSError) as exc:
            raise SetupCancelled(f"Can't use the audio system: {exc}") from None
        candidates = rank(devices, self.system)
        if not candidates:
            raise SetupCancelled("No audio inputs found. Connect the radio's audio and try again.")
        recommended = [c for c in candidates if c.recommended]
        others = [c for c in candidates if not c.recommended]
        numbered = recommended + others
        if recommended:
            p.say("  Recommended:")
        for i, c in enumerate(numbered, 1):
            if i == len(recommended) + 1:
                p.say("  Other inputs:")
            why = f"  ({'; '.join(c.reasons)})" if c.reasons else ""
            d = c.device
            p.say(f"   {i:2d}. {d.name}  [{d.host_api}, {d.default_sample_rate} Hz]{why}")
        if self.system == "Linux":
            p.say(f"  {LINUX_TIP}")
        while True:
            choice = p.ask("Which number is your radio?", "1")
            if choice.isdigit() and 1 <= int(choice) <= len(numbered):
                device = numbered[int(choice) - 1].device
            else:
                p.say("  Please enter one of the numbers above.")
                continue
            p.say(f"  Listening to {device.name} for 3 seconds...")
            try:
                report = self.level_probe(backend, device)
            except (DeviceUnavailableError, OSError) as exc:
                p.say(f"  Couldn't open it: {exc}")
                continue
            peak = "silence" if math.isinf(report.peak_dbfs) else f"{report.peak_dbfs:.1f} dBFS"
            p.say(f"  Peak level {peak}: {report.verdict}.")
            if report.verdict == "good" or yes(p.ask("Use this input anyway? (y/n)", "y")):
                p.say()
                return device, None

    def step_station(self) -> Station:
        p = self.prompt
        p.say("Step 4 of 6: your station (optional)")
        result = Station()
        while True:
            raw = p.ask("Callsign (Enter to skip)")
            if not raw:
                break
            try:
                result.callsign = normalise_callsign(raw)
            except ValueError as exc:
                p.say(f"  {exc}")
                continue
            p.say("  Sharing your callsign credits you on your recordings (it's public, and")
            p.say("  helps attribution). Not sharing keeps it out of everything uploaded.")
            result.share_callsign = yes(p.ask("Share your callsign? (y/n)", "n"))
            break
        while True:
            raw = p.ask("Grid locator, e.g. EN52 or EN52wa (Enter to skip)")
            if not raw:
                break
            try:
                result.grid = normalise_grid(raw)
            except ValueError as exc:
                p.say(f"  {exc}")
                continue
            p.say("  How much of it to share: 0 (none), 4 (about 100 km), 6 (about 5 km; helps")
            p.say("  satellite Doppler analysis) or 8.")
            precision = p.ask("Characters to share", "4")
            result.grid_precision = int(precision) if precision in ("0", "4", "6", "8") else 4
            break
        p.say()
        return result

    def step_wsjtx(self) -> None:
        p = self.prompt
        p.say("Step 5 of 6: WSJT-X (optional)")
        p.say("  If WSJT-X is running, the recorder reads its mode and frequency to label")
        p.say("  recordings. Listening for it for about 15 seconds...")
        found = self.wsjtx_probe()
        if found == "busy":
            p.say("  Another program is already using UDP port 2237 (GridTracker or JTAlert?).")
            p.say(f"  {WSJTX_HELP}")
        elif found:
            p.say(f"  Found {found}.")
        else:
            p.say("  WSJT-X wasn't heard. That's fine: start it whenever you like.")
            p.say(f"  {WSJTX_HELP}")
        p.say()

    def step_clock(self) -> None:
        p = self.prompt
        p.say("Step 6 of 6: clock check")
        if self.ntp_probe is None:
            p.say("  Skipped.")
            p.say()
            return
        try:
            answer = self.ntp_probe("pool.ntp.org")
        except Exception as exc:
            p.say(f"  Couldn't reach a time server ({exc}); the recorder will keep trying.")
            p.say()
            return
        status = status_for(answer.offset_s)
        direction = "behind" if answer.offset_s > 0 else "ahead of"
        p.say(f"  Your clock is {abs(answer.offset_s):.3f} s {direction} true time ({status}).")
        if status != "green":
            p.say("  Recording still works (the offset is recorded for correction), but fixing")
            p.say("  your computer's time sync makes the recordings more useful.")
        p.say()
