# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Pretend to be WSJT-X, so the recorder can be developed without a radio.

Built from docs/protocols/wsjtx-udp.md. Two ways to run it:

    # Replay a captured session with its original timing (or faster)
    python tools/fake_wsjtx_emitter.py --replay tests/fixtures/udp/wsjtx-session1 --speed 4

    # Synthetic FT8 on 20 m: heartbeat every 15 s, status, a few decodes per cycle,
    # transmitting every 4th cycle
    python tools/fake_wsjtx_emitter.py --mode FT8 --dial 14074000 --tx-every 4
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from signal_archive_recorder.sources.wsjtx.messages import (
    Close,
    Decode,
    Heartbeat,
    Status,
    encode,
)

SYMBOLS = {"FT8": "~", "FT4": "+"}
PERIODS = {"FT8": 15.0, "FT4": 7.5}
MESSAGES = [
    "CQ K1ABC FN42",
    "K1ABC W9XYZ EN52",
    "W9XYZ K1ABC -12",
    "K1ABC W9XYZ R-08",
    "CQ DX G4ABC IO91",
]


def replay(sock: socket.socket, target: tuple[str, int], folder: Path, speed: float) -> None:
    index = [json.loads(line) for line in (folder / "index.jsonl").open()]
    start = datetime.fromisoformat(index[0]["received_utc"])
    t0 = time.monotonic()
    for entry in index:
        due = (datetime.fromisoformat(entry["received_utc"]) - start).total_seconds() / speed
        time.sleep(max(0.0, due - (time.monotonic() - t0)))
        sock.sendto((folder / entry["file"]).read_bytes(), target)
    print(f"replayed {len(index)} datagrams")


def synthetic(sock: socket.socket, target: tuple[str, int], args: argparse.Namespace) -> None:
    client = "WSJT-X"
    period = PERIODS.get(args.mode, 15.0)
    rng = random.Random(args.seed)

    def status(transmitting: bool, decoding: bool) -> Status:
        return Status(
            client, args.dial, args.mode, None, "-15", args.mode, False, transmitting, decoding,
            1500, 1500, "", "", None, False, None, False, 0, None, None, "Default",
            "TUNE" if transmitting else None,
        )  # fmt: skip

    def send(message: Heartbeat | Status | Decode | Close) -> None:
        sock.sendto(encode(message), target)

    send(Heartbeat(client, 3, "fake", ""))
    send(status(False, False))
    cycle = 0
    last_heartbeat = time.monotonic()
    try:
        while args.cycles == 0 or cycle < args.cycles:
            now = time.time()
            time.sleep(period - now % period)
            transmitting = args.tx_every > 0 and cycle % args.tx_every == args.tx_every - 1
            send(status(transmitting, not transmitting))
            if not transmitting:
                slot_ms = int(time.time() // period * period * 1000) % 86_400_000
                for text in rng.sample(MESSAGES, k=rng.randint(1, len(MESSAGES))):
                    send(
                        Decode(
                            client, True, slot_ms, rng.randint(-24, 15),
                            round(rng.uniform(-0.5, 0.8), 1), rng.randint(200, 2900),
                            SYMBOLS.get(args.mode, "~"), text, False, False,
                        )
                    )  # fmt: skip
            if time.monotonic() - last_heartbeat >= 15:
                send(Heartbeat(client, 3, "fake", ""))
                last_heartbeat = time.monotonic()
            print(f"cycle {cycle}: {'TX' if transmitting else 'RX'}")
            cycle += 1
    finally:
        send(Close(client))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=2237)
    p.add_argument("--replay", type=Path, help="captured fixture directory to replay")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed-up factor")
    p.add_argument("--mode", default="FT8", choices=sorted(PERIODS))
    p.add_argument("--dial", type=int, default=14_074_000, help="dial frequency in Hz")
    p.add_argument("--tx-every", type=int, default=0, help="transmit every Nth cycle (0: never)")
    p.add_argument("--cycles", type=int, default=0, help="stop after N cycles (0: run forever)")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    target = (args.host, args.port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if args.replay:
            replay(sock, target, args.replay, args.speed)
        else:
            with contextlib.suppress(KeyboardInterrupt):
                synthetic(sock, target, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
