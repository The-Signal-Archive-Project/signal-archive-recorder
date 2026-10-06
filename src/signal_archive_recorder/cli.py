# SPDX-License-Identifier: Apache-2.0
"""Command line: signal-archive-recorder --headless --config recorder.toml

Records until Ctrl-C or SIGTERM (Ctrl-Break on Windows), then finishes the current
chunk and the session cleanly. Exit codes: 0 after a clean stop, 2 for a bad
configuration, 3 when the audio device can't be used.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from types import FrameType

from signal_archive_recorder import __version__
from signal_archive_recorder.audio.device import DeviceUnavailableError, SoundDeviceBackend
from signal_archive_recorder.config import ConfigError, load_config
from signal_archive_recorder.recorder import Recorder

log = logging.getLogger("signal_archive_recorder")


def _install_stop_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, stopping", signal.Signals(signum).name)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl-Break on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)


def _list_devices() -> int:
    try:
        devices = SoundDeviceBackend().input_devices()
    except OSError as exc:
        print(f"audio system unavailable: {exc}", file=sys.stderr)
        return 3
    for d in devices:
        print(f"{d.name}  [{d.host_api}, {d.max_input_channels} ch, {d.default_sample_rate} Hz]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signal-archive-recorder", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run without a window")
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("--list-devices", action="store_true", help="list audio inputs and exit")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.list_devices:
        return _list_devices()
    if not args.headless:
        parser.error("only --headless is available so far (the window arrives in v0.3)")
    if args.config is None:
        parser.error("--config is required")

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()
    _install_stop_handlers(stop)
    recorder = Recorder(config)
    try:
        session = recorder.start()
    except DeviceUnavailableError as exc:
        print(f"audio device: {exc}", file=sys.stderr)
        return 3
    print(f"recording to {session.path} (Ctrl-C to stop)", flush=True)
    while not stop.wait(0.5):
        pass
    summary = recorder.stop(reason="signal")
    print(
        f"saved {len(summary.chunks)} chunks to {summary.session.path} "
        f"({summary.capture.lost_frames} frames lost)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
