# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Command line for Signal Archive Recorder.

    signal-archive-recorder                                 first run: setup; then: record
    signal-archive-recorder setup                           run first-run setup again
    signal-archive-recorder init                            create a starter configuration
    signal-archive-recorder record                          record until Ctrl-C
    signal-archive-recorder devices                         list audio inputs
    signal-archive-recorder consent                         read and accept the terms
    signal-archive-recorder login                           store a Hugging Face token
    signal-archive-recorder review [SESSION]                what an upload would share
    signal-archive-recorder remove-chunk SESSION CHUNK      leave a chunk out
    signal-archive-recorder upload [SESSION ...]            one pull request per session
    signal-archive-recorder upload --dry-run                what would be sent, sending nothing
    signal-archive-recorder status                          follow up open pull requests
    signal-archive-recorder requeue SESSION                 upload a session again

Exit codes: 0 fine, 2 configuration, 3 audio device, 4 Hugging Face login,
5 consent needed, 6 some uploads didn't go through.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import getpass
import logging
import os
import platform
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from signal_archive_recorder import __version__
from signal_archive_recorder.audio.device import (
    DeviceInfo,
    DeviceUnavailableError,
    SoundDeviceBackend,
)
from signal_archive_recorder.clockmon.monitor import ntplib_probe
from signal_archive_recorder.config import ConfigError, RecorderConfig, load_config
from signal_archive_recorder.core.clock import Clock, SystemClock
from signal_archive_recorder.firstrun.devices import rank
from signal_archive_recorder.firstrun.wizard import SetupCancelled, TerminalPrompter, Wizard
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.paths import config_dir, default_config_file, example_config
from signal_archive_recorder.recorder import Recorder
from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.upload.consent import CONSENT_TEXT, ConsentStore, NoConsentError
from signal_archive_recorder.upload.hub import DEFAULT_REPO, HfHub, Hub, HubError, check_token
from signal_archive_recorder.upload.queue import NotLoggedInError, Uploader, UploadState
from signal_archive_recorder.upload.review import ReviewError, format_review, review
from signal_archive_recorder.upload.screening import screen_session
from signal_archive_recorder.upload.token import KeyringUnavailableError, Token, TokenStore

log = logging.getLogger("signal_archive_recorder")

# Swapped out by tests: no real Hugging Face, keyring or clock.
make_hub: Callable[[], Hub] = HfHub
make_token_store: Callable[[], TokenStore] = TokenStore
make_clock: Callable[[], Clock] = SystemClock

EXIT_CONFIG, EXIT_DEVICE, EXIT_LOGIN, EXIT_CONSENT, EXIT_UPLOAD = 2, 3, 4, 5, 6


def _hub() -> Hub:
    return make_hub()


def _tokens() -> TokenStore:
    return make_token_store()


def _clock() -> Clock:
    return make_clock()


def _consent() -> ConsentStore:
    return ConsentStore(config_dir() / "consent.json")


def _config(path: Path | None) -> RecorderConfig:
    path = path or default_config_file()
    if not path.exists():
        raise ConfigError(
            f"no configuration at {path}. Create one with: signal-archive-recorder init"
        )
    return load_config(path)


def _uploader(config: RecorderConfig) -> Uploader:
    return Uploader(
        SessionStorage(config.storage_root),
        _hub(),
        _tokens(),
        _consent(),
        _clock(),
        repo_id=config.upload.repo,
        require_decoder=config.upload.require_decoder,
    )


def _session_dir(config: RecorderConfig, session_id: str) -> Path:
    path = SessionStorage(config.storage_root).sessions / session_id
    if not (path / "session.json").exists():
        raise ReviewError(f"no session {session_id} under {path.parent}")
    return path


# -- commands -----------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    config = _config(args.config)
    consent = _consent().load()
    if consent is not None and consent.is_current():  # recorded with the session
        station = dataclasses.replace(config.station, consent=consent.as_consent())
        config = dataclasses.replace(config, station=station)
    stop = threading.Event()

    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, stopping", signal.Signals(signum).name)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl-Break on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)
    recorder = Recorder(config)
    try:
        session = recorder.start()
    except DeviceUnavailableError as exc:
        print(f"audio device: {exc}", file=sys.stderr)
        return EXIT_DEVICE
    print(f"recording to {session.path} (Ctrl-C to stop)", flush=True)
    while not stop.wait(0.5):
        pass
    summary = recorder.stop(reason="signal")
    print(f"saved {len(summary.chunks)} chunks to {summary.session.path} "
          f"({summary.capture.lost_frames} frames lost)", flush=True)  # fmt: skip
    return 0


def build_wizard() -> Wizard:
    """The setup wizard with the real terminal, audio system, Hugging Face and clock."""
    return Wizard(
        prompt=TerminalPrompter(),
        backend_factory=SoundDeviceBackend,
        hub_factory=make_hub,
        tokens=_tokens(),
        consent=_consent(),
        now_ns=_clock().now_ns,
        ntp_probe=ntplib_probe,
    )


def _run_wizard(path: Path) -> int:
    wizard = build_wizard()
    try:
        wizard.run(path)
    except SetupCancelled as exc:
        wizard.prompt.say(str(exc))
        return EXIT_CONFIG
    except (KeyboardInterrupt, EOFError):
        wizard.prompt.say("\nSetup stopped; nothing was saved.")
        return EXIT_CONFIG
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """No command: set up on first run, otherwise record."""
    path: Path = args.config or default_config_file()
    if path.exists():
        return cmd_record(args)
    if not sys.stdin.isatty():
        print(
            f"No configuration yet ({path}). Run `signal-archive-recorder` in a terminal to "
            "set up, or create one with `signal-archive-recorder init --device NAME`.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    result = _run_wizard(path)
    if result != 0:
        return result
    answer = input("Start recording now? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        return cmd_record(args)
    print("Start recording any time with: signal-archive-recorder")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    path: Path = args.config or default_config_file()
    if path.exists():
        answer = input(f"{path} exists. Replace it with new settings? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Kept your existing settings.")
            return 0
    return _run_wizard(path)


def cmd_init(args: argparse.Namespace) -> int:
    path: Path = args.config or default_config_file()
    if path.exists() and not args.force:
        print(f"{path} already exists; edit it, or use --force to start again.", file=sys.stderr)
        return EXIT_CONFIG
    text = example_config()
    device = args.device
    if device is None and sys.stdin.isatty():
        try:
            names = [d.name for d in SoundDeviceBackend().input_devices()]
        except (DeviceUnavailableError, OSError) as exc:
            print(f"(couldn't list audio devices: {exc})", file=sys.stderr)
            names = []
        for i, name in enumerate(names, 1):
            print(f"  {i}. {name}")
        if names:
            choice = input("Which input is your radio? Number (Enter to set it later): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(names):
                device = names[int(choice) - 1]
    if device:
        escaped = device.replace("\\", "\\\\").replace('"', '\\"')
        text = text.replace('device = "USB Audio CODEC"', f'device = "{escaped}"', 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"Wrote {path}")
    if not device:
        print("Set [audio] device in it (see: signal-archive-recorder devices).")
    print("Next: edit [station] if you like, then: signal-archive-recorder record")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        devices = SoundDeviceBackend().input_devices()
    except (DeviceUnavailableError, OSError) as exc:
        print(f"audio system unavailable: {exc}", file=sys.stderr)
        return EXIT_DEVICE

    def line(d: DeviceInfo) -> str:
        return f"{d.name}  [{d.host_api}, {d.max_input_channels} ch, {d.default_sample_rate} Hz]"

    if args.all:
        for d in devices:
            print(line(d))
        return 0
    ranked = rank(devices, platform.system())
    groups = (
        ("Recommended:", [c for c in ranked if c.recommended]),
        ("Other inputs:", [c for c in ranked if not c.recommended]),
    )
    for heading, group in groups:
        if group:
            print(heading)
        for c in group:
            print(f"  {line(c.device)}")
            for reason in c.reasons:
                print(f"      {reason}")
    hidden = len(devices) - len(ranked)
    if hidden:
        print(f"({hidden} system-plumbing entries hidden; --all shows everything)")
    return 0


def cmd_consent(args: argparse.Namespace) -> int:
    store = _consent()
    if args.withdraw:
        store.withdraw()
        print("Consent withdrawn: nothing more will be uploaded until you agree again.")
        return 0
    print(CONSENT_TEXT)
    if not args.yes:
        answer = input('Type "I agree" to accept, or anything else to stop: ').strip().lower()
        if answer != "i agree":
            print("Not accepted. Nothing will be uploaded.")
            return EXIT_CONSENT
    record = store.accept(_clock().now_ns())
    print(f"Accepted (license {record.license_id}, consent version {record.consent_version}).")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    config = _config(args.config) if args.config or default_config_file().exists() else None
    repo = config.upload.repo if config else RecorderConfig().upload.repo
    raw = (
        os.environ.get(args.token_env, "")
        if args.token_env
        else getpass.getpass("Hugging Face access token (input hidden): ")
    )
    try:
        token = Token(raw)
        identity = check_token(_hub(), token, repo)
    except (ValueError, HubError) as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return EXIT_LOGIN
    try:
        _tokens().set(token)
    except KeyringUnavailableError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return EXIT_LOGIN
    print(f"Logged in to Hugging Face as {identity.username}. The token is in your OS keyring.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    _tokens().delete()
    print("Hugging Face token removed from the keyring.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    config = _config(args.config)
    uploader = _uploader(config)
    if args.session:
        targets = [_session_dir(config, args.session)]
    else:
        targets = [d for d, r in uploader.sessions()
                   if r.state in (UploadState.QUEUED, UploadState.BLOCKED)]  # fmt: skip
    if not targets:
        print("Nothing waiting to upload.")
    registry = ModeRegistry.load_default()
    for session_dir in targets:
        print(format_review(review(session_dir)))
        print("  Radio-audio checks:")
        for s in screen_session(
            session_dir, registry, require_decoder=config.upload.require_decoder
        ):
            mark = "ok      " if s.eligible else "KEEP BACK"
            evidence = f"{s.decodes_visible}/{s.decodes_checked} decodes found in the audio"
            print(f"    {mark} {s.chunk_id}  ({evidence})")
            for note in s.reasons + s.warnings:
                print(f"             - {note}")
        print()
    return 0


def cmd_remove_chunk(args: argparse.Namespace) -> int:
    config = _config(args.config)
    builder = MetadataBuilder(ModeRegistry.load_default(), config.station)
    _uploader(config).remove_chunk(_session_dir(config, args.session), args.chunk, builder)
    print(f"Removed {args.chunk} from {args.session}; it's kept locally in local/removed/.")
    return 0


def _repo_note(config: RecorderConfig) -> None:
    if config.upload.repo != DEFAULT_REPO:
        print(f"(uploading to the TEST repository {config.upload.repo}, not {DEFAULT_REPO})")


def cmd_upload(args: argparse.Namespace) -> int:
    config = _config(args.config)
    uploader = _uploader(config)
    _repo_note(config)
    if args.dry_run:
        return _dry_run(config, uploader, args.sessions)
    try:
        if args.sessions:
            results = [(d, uploader.upload(d))
                       for d in (_session_dir(config, s) for s in args.sessions)]  # fmt: skip
        else:
            results = uploader.upload_all()
    except NoConsentError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONSENT
    except (NotLoggedInError, HubError, KeyringUnavailableError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LOGIN
    if not results:
        print("Nothing to upload.")
    trouble = False
    for session_dir, record in results:
        for kept in record.excluded:
            print(f"{session_dir.name}: kept back {kept['chunk_id']}: {'; '.join(kept['reasons'])}")
        if record.state is UploadState.PR_OPENED:
            print(f"{session_dir.name}: pull request opened: {record.pr_url}")
        elif record.state is UploadState.BLOCKED:
            trouble = True
            print(f"{session_dir.name}: not uploaded, problems found:")
            print("".join(f"  - {p}\n" for p in record.problems), end="")
        else:
            trouble = True
            print(f"{session_dir.name}: {record.state.value} ({record.last_error}); will retry")
    return EXIT_UPLOAD if trouble else 0


def _dry_run(config: RecorderConfig, uploader: Uploader, sessions: list[str]) -> int:
    if sessions:
        targets = [_session_dir(config, s) for s in sessions]
    else:
        targets = [
            d
            for d, r in uploader.sessions()
            if r.state is UploadState.QUEUED and review(d).finished
        ]
    if not targets:
        print("Nothing to upload.")
    trouble = False
    try:
        for session_dir in targets:
            plan = uploader.dry_run(session_dir)
            total = sum(size for _, size in plan.files)
            print(f"{session_dir.name}: would open a pull request on {plan.repo_id}")
            print(f"  title: {plan.title}")
            print(f"  {len(plan.files)} files, {total / 1e6:.1f} MB, into {plan.path_in_repo}/")
            for name, size in plan.files:
                print(f"    {name}  ({size:,} bytes)")
            for chunk_id, reasons in plan.excluded:
                print(f"  {chunk_id} would be KEPT BACK (stays on this computer):")
                print("".join(f"    - {r}\n" for r in reasons), end="")
            for chunk_id, notes in plan.warnings:
                print(f"  {chunk_id}: " + "; ".join(notes))
            if plan.problems:
                trouble = True
                print("  but it would be blocked:")
                print("".join(f"    - {p}\n" for p in plan.problems), end="")
    except NoConsentError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONSENT
    except (HubError, KeyringUnavailableError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LOGIN
    print("Dry run: nothing was sent.")
    return EXIT_UPLOAD if trouble else 0


def cmd_requeue(args: argparse.Namespace) -> int:
    config = _config(args.config)
    record = _uploader(config).requeue(_session_dir(config, args.session))
    print(f"{args.session} is {record.state.value} again.")
    print("Send it with: signal-archive-recorder upload")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args.config)
    uploader = _uploader(config)
    with contextlib.suppress(NotLoggedInError):  # then list what's known locally
        uploader.poll_all()
    _repo_note(config)
    for session_dir, record in uploader.sessions():
        line = f"{session_dir.name}  {record.state.value}"
        if record.pr_url:
            line += f"  {record.pr_url}"
        print(line)
        for message in record.validator_messages + record.problems:
            print(f"    {message}")
        if record.last_error:
            print(f"    last attempt: {record.last_error}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="TOML configuration file")
    common.add_argument("-v", "--verbose", action="store_true")
    parser = argparse.ArgumentParser(
        prog="signal-archive-recorder",
        description="Record raw receive audio for the Signal Archive Project.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.set_defaults(func=cmd_start)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: object, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    add("setup", cmd_setup, "run first-run setup (again)")
    p = add("init", cmd_init, "create a starter configuration file without questions")
    p.add_argument("--device", help="audio input name (default: ask)")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    add("record", cmd_record, "record until Ctrl-C")
    p = add("devices", cmd_devices, "list audio inputs, recommended first")
    p.add_argument("--all", action="store_true", help="every entry, unranked")
    p = add("consent", cmd_consent, "read and accept the contribution terms")
    p.add_argument("--yes", action="store_true", help="accept without the prompt")
    p.add_argument("--withdraw", action="store_true", help="withdraw consent")
    p = add("login", cmd_login, "store a Hugging Face access token in the OS keyring")
    p.add_argument("--token-env", metavar="VAR", help="read the token from this variable")
    add("logout", cmd_logout, "remove the stored token")
    p = add("review", cmd_review, "show what an upload would share")
    p.add_argument("session", nargs="?")
    p = add("remove-chunk", cmd_remove_chunk, "leave a chunk out of a session's upload")
    p.add_argument("session")
    p.add_argument("chunk")
    p = add("upload", cmd_upload, "upload finished sessions, one pull request each")
    p.add_argument("sessions", nargs="*")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing")
    p = add("requeue", cmd_requeue, "let a blocked, failed or missing session upload again")
    p.add_argument("session")
    add("status", cmd_status, "follow up open pull requests")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result: int = args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (ReviewError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
