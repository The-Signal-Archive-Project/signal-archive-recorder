# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Build the APT repository (published on GitHub Pages) from the GitHub releases.

Two channels ("suites"), one component ("main"), amd64:
    stable   the newest full release
    beta     the newest release or beta, whichever is newer (testers opt in)
Only the newest package of each channel is kept: GitHub Pages sites are limited to
1 GB, and each .deb is about 110 MB.

    python installer/apt/build_repo.py --site _site         # then sign: see sign()

Standard library only: it reads the .deb files itself and writes the Packages and
Release files apt needs, so it runs (and is tested) anywhere. Signing uses gpg.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.github.com/repos/The-Signal-Archive-Project/signal-archive-recorder/releases"
SITE_URL = "https://the-signal-archive-project.github.io/signal-archive-recorder"
POOL = "pool/main/s/signal-archive-recorder"
CHANNELS = ("stable", "beta")
HASHES = (("MD5Sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256"))


@dataclass(frozen=True)
class Deb:
    tag: str
    name: str
    url: str
    prerelease: bool  # by the version, not GitHub's flag (a beta may be shown as Latest)
    sort_key: tuple[int, ...]


def version_key(tag: str) -> tuple[tuple[int, ...], bool]:
    """v0.3.0-beta.2 -> ((0, 3, 0, 0, 2), True); v0.3.0 -> ((0, 3, 0, 1, 0), False)."""
    core, _, pre = tag.removeprefix("v").partition("-")
    numbers = tuple(int(x) for x in core.split("."))
    if not pre:
        return (*numbers, 9, 0), False  # a release sorts after all its pre-releases
    kind, _, n = pre.partition(".")
    rank = {"alpha": 0, "beta": 1, "rc": 2}.get(kind, 0)
    return (*numbers, rank, int(n or 0)), True


def debs_in(releases: list[dict[str, object]]) -> list[Deb]:
    found = []
    for r in releases:
        if r.get("draft"):
            continue
        tag = str(r["tag_name"])
        key, pre = version_key(tag)
        for asset in r.get("assets", []):  # type: ignore[union-attr]
            name = str(asset["name"])  # type: ignore[index]
            if name.endswith("_amd64.deb"):
                url = str(asset["browser_download_url"])  # type: ignore[index]
                found.append(Deb(tag, name, url, pre, key))
    return found


def choose(debs: list[Deb]) -> dict[str, Deb | None]:
    """The package each channel serves (None: nothing yet)."""
    stable = [d for d in debs if not d.prerelease]
    return {
        "stable": max(stable, key=lambda d: d.sort_key, default=None),
        "beta": max(debs, key=lambda d: d.sort_key, default=None),
    }


def control_fields(deb: bytes) -> str:
    """The control file of a .deb (an ar archive holding control.tar.*)."""
    if not deb.startswith(b"!<arch>\n"):
        raise ValueError("not a .deb")
    i = 8
    while i < len(deb):
        name = deb[i : i + 16].decode().strip().rstrip("/")
        size = int(deb[i + 48 : i + 58])
        body = deb[i + 60 : i + 60 + size]
        if name.startswith("control.tar"):
            with tarfile.open(fileobj=io.BytesIO(body)) as t:
                member = t.extractfile("./control") or t.extractfile("control")
                assert member is not None
                return member.read().decode().strip() + "\n"
        i += 60 + size + (size % 2)
    raise ValueError("no control.tar in the .deb")


def packages_entry(deb_path: Path, site: Path) -> str:
    data = deb_path.read_bytes()
    lines = control_fields(data).rstrip("\n")
    extra = [
        f"Filename: {deb_path.relative_to(site).as_posix()}",
        f"Size: {len(data)}",
        f"MD5sum: {hashlib.md5(data).hexdigest()}",
        f"SHA1: {hashlib.sha1(data).hexdigest()}",
        f"SHA256: {hashlib.sha256(data).hexdigest()}",
    ]
    # Filename etc. go before Description, as dpkg-scanpackages writes them.
    head, sep, description = lines.partition("\nDescription:")
    return f"{head}\n" + "\n".join(extra) + (f"{sep}{description}" if sep else "") + "\n"


def release_file(suite: str, files: dict[str, bytes], now: dt.datetime) -> str:
    lines = [
        "Origin: The Signal Archive Project",
        "Label: Signal Archive Recorder",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {now.strftime('%a, %d %b %Y %H:%M:%S UTC')}",
        "Architectures: amd64",
        "Components: main",
        f"Description: Signal Archive Recorder ({suite} channel)",
    ]
    for field, algorithm in HASHES:
        lines.append(f"{field}:")
        for path, content in sorted(files.items()):
            digest = hashlib.new(algorithm, content).hexdigest()
            lines.append(f" {digest} {len(content):>8} {path}")
    return "\n".join(lines) + "\n"


def write_channel(site: Path, suite: str, deb: Path | None, now: dt.datetime) -> None:
    packages = packages_entry(deb, site).encode() if deb else b""
    index = {
        "main/binary-amd64/Packages": packages,
        "main/binary-amd64/Packages.gz": gzip.compress(packages, mtime=0),
    }
    dist = site / "dists" / suite
    for path, content in index.items():
        (dist / path).parent.mkdir(parents=True, exist_ok=True)
        (dist / path).write_bytes(content)
    (dist / "Release").write_text(release_file(suite, index, now))


def sign(site: Path, key_id: str) -> None:
    """InRelease (inline) and Release.gpg (detached) for each channel."""
    for suite in CHANNELS:
        dist = site / "dists" / suite
        gpg = ["gpg", "--batch", "--yes", "--local-user", key_id, "--digest-algo", "SHA512"]
        subprocess.run(
            [*gpg, "--clearsign", "-o", dist / "InRelease", dist / "Release"], check=True
        )
        subprocess.run(
            [*gpg, "--armor", "--detach-sign", "-o", dist / "Release.gpg", dist / "Release"],
            check=True,
        )


def index_html(chosen: dict[str, Deb | None]) -> str:
    template = (HERE / "index.template.html").read_text()
    for suite in CHANNELS:
        deb = chosen[suite]
        template = template.replace(f"{{{suite}}}", deb.tag if deb else "nothing yet")
    return template.replace("{site}", SITE_URL)


def fetch_releases() -> list[dict[str, object]]:
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(API, headers=headers), timeout=60) as r:
        data = json.loads(r.read())
    assert isinstance(data, list)
    return data


def build(
    site: Path, releases: list[dict[str, object]], download: bool = True
) -> dict[str, Deb | None]:
    shutil.rmtree(site, ignore_errors=True)
    (site / POOL).mkdir(parents=True)
    chosen = choose(debs_in(releases))
    now = dt.datetime.now(dt.UTC)
    for suite in CHANNELS:
        deb = chosen[suite]
        path = None
        if deb is not None:
            path = site / POOL / deb.name
            if download and not path.exists():
                print(f"{suite}: downloading {deb.name} ({deb.tag})", flush=True)
                urllib.request.urlretrieve(deb.url, path)
        write_channel(site, suite, path if path and path.exists() else None, now)
        print(f"{suite}: {deb.tag if deb else 'empty (no package yet)'}", flush=True)
    shutil.copyfile(HERE / "signal-archive.asc", site / "signal-archive.asc")
    (site / "index.html").write_text(index_html(chosen))
    (site / ".nojekyll").write_text("")  # serve dists/ and pool/ as they are
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--sign-with", help="gpg key id/fingerprint to sign the Release files")
    args = parser.parse_args()
    build(args.site, fetch_releases())
    if args.sign_with:
        sign(args.site, args.sign_with)
    return 0


if __name__ == "__main__":
    sys.exit(main())
