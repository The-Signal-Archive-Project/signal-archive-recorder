# SPDX-License-Identifier: Apache-2.0
"""Command line for Signal Archive Recorder.

    signal-archive-recorder record --config recorder.toml   record until Ctrl-C
    signal-archive-recorder devices                         list audio inputs
    signal-archive-recorder consent                         read and accept the terms
    signal-archive-recorder login                           store a Hugging Face token
    signal-archive-recorder review [SESSION]                what an upload would share
    signal-archive-recorder remove-chunk SESSION CHUNK      leave a chunk out
    signal-archive-recorder upload [SESSION ...]            one pull request per session
    signal-archive-recorder status                          follow up open pull requests

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
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from signal_archive_recorder import __version__
from signal_archive_recorder.audio.device import DeviceUnavailableError, SoundDeviceBackend
from signal_archive_recorder.config import ConfigError, RecorderConfig, load_config
from signal_archive_recorder.core.clock import Clock, SystemClock
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.modes import ModeRegistry
from signal_archive_recorder.paths import config_dir, default_config_file
from signal_archive_recorder.recorder import Recorder
from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.upload.consent import CONSENT_TEXT, ConsentStore, NoConsentError
from signal_archive_recorder.upload.hub import HfHub, Hub, HubError, check_token
from signal_archive_recorder.upload.queue import NotLoggedInError, Uploader, UploadState
from signal_archive_recorder.upload.review import ReviewError, format_review, review
from signal_archive_recorder.upload.token import Token, TokenStore

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
            f"no configuration at {path}. Copy examples/recorder.toml there, or pass --config"
        )
    return load_config(path)


def _uploader(config: RecorderConfig) -> Uploader:
    return Uploader(SessionStorage(config.storage_root), _hub(), _tokens(), _consent(), _clock(),
                    repo_id=config.upload.repo)  # fmt: skip


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


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        devices = SoundDeviceBackend().input_devices()
    except OSError as exc:
        print(f"audio system unavailable: {exc}", file=sys.stderr)
        return EXIT_DEVICE
    for d in devices:
        print(f"{d.name}  [{d.host_api}, {d.max_input_channels} ch, {d.default_sample_rate} Hz]")
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
    _tokens().set(token)
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
    for session_dir in targets:
        print(format_review(review(session_dir)))
        print()
    return 0


def cmd_remove_chunk(args: argparse.Namespace) -> int:
    config = _config(args.config)
    builder = MetadataBuilder(ModeRegistry.load_default(), config.station)
    _uploader(config).remove_chunk(_session_dir(config, args.session), args.chunk, builder)
    print(f"Removed {args.chunk} from {args.session}; it's kept locally in local/removed/.")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    config = _config(args.config)
    uploader = _uploader(config)
    try:
        if args.sessions:
            results = [(d, uploader.upload(d))
                       for d in (_session_dir(config, s) for s in args.sessions)]  # fmt: skip
        else:
            results = uploader.upload_all()
    except NoConsentError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONSENT
    except (NotLoggedInError, HubError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LOGIN
    if not results:
        print("Nothing to upload.")
    trouble = False
    for session_dir, record in results:
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


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args.config)
    uploader = _uploader(config)
    with contextlib.suppress(NotLoggedInError):  # then list what's known locally
        uploader.poll_all()
    for session_dir, record in uploader.sessions():
        line = f"{session_dir.name}  {record.state.value}"
        if record.pr_url:
            line += f"  {record.pr_url}"
        print(line)
        for message in record.validator_messages + record.problems:
            print(f"    {message}")
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
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, func: object, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    add("record", cmd_record, "record until Ctrl-C")
    add("devices", cmd_devices, "list audio inputs")
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
