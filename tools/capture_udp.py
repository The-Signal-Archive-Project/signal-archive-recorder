# SPDX-License-Identifier: Apache-2.0
"""Record raw UDP datagrams from a live program into test fixtures.

Receive only: this tool never sends anything. Each datagram is saved as
<out>/<seq>.bin, with one line per datagram in <out>/index.jsonl (receive time,
size, sender). Stop with Ctrl-C.

    python tools/capture_udp.py --out tests/fixtures/udp/wsjtx-3.0.2 --port 2237
    python tools/capture_udp.py --out ... --group 224.0.0.1   # multicast
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path


def open_socket(port: int, group: str | None, bind: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if group:
        sock.bind(("", port))
        membership = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    else:
        sock.bind((bind, port))
    return sock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="fixture directory to create")
    parser.add_argument("--port", type=int, default=2237)
    parser.add_argument("--group", help="multicast group to join instead of unicast")
    parser.add_argument("--bind", default="127.0.0.1", help="unicast address to bind")
    parser.add_argument("--note", default="", help="free text saved in capture.json")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "capture.json").write_text(
        json.dumps(
            {
                "started_utc": datetime.now(UTC).isoformat(),
                "port": args.port,
                "group": args.group,
                "note": args.note,
            },
            indent=2,
        )
        + "\n"
    )
    sock = open_socket(args.port, args.group, args.bind)
    where = f"group {args.group}" if args.group else args.bind
    print(f"listening on {where}:{args.port}, saving to {args.out} (Ctrl-C to stop)")

    seq = 0
    with (args.out / "index.jsonl").open("a", encoding="utf-8") as index:
        try:
            while True:
                data, (host, port) = sock.recvfrom(65_535)
                name = f"{seq:05d}.bin"
                (args.out / name).write_bytes(data)
                entry = {
                    "seq": seq,
                    "file": name,
                    "received_utc": datetime.now(UTC).isoformat(),
                    "size": len(data),
                    "from_port": port,
                    "from_loopback": host.startswith("127."),
                }
                index.write(json.dumps(entry) + "\n")
                index.flush()
                print(f"{seq:5d}  {len(data):5d} B  {data[:16].hex(' ')}")
                seq += 1
        except KeyboardInterrupt:
            print(f"\nsaved {seq} datagrams")
    return 0


if __name__ == "__main__":
    sys.exit(main())
