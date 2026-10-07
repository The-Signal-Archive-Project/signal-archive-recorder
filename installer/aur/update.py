# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Write the AUR package for a published release, ready to push.

    python installer/aur/update.py --to ~/aur/signal-archive-recorder

Takes installer/aur/PKGBUILD (whose pkgver tools/release.py keeps current),
downloads that version's source package from the GitHub release, fills in its
sha256, and writes PKGBUILD and .SRCINFO (via `makepkg --printsrcinfo`) into the
AUR repository clone given by --to. Then review, commit and push there:

    cd ~/aur/signal-archive-recorder && git add PKGBUILD .SRCINFO \\
        && git commit -m "Update to <version>" && git push

First time: create the AUR account, add your SSH key, and
`git clone ssh://aur@aur.archlinux.org/signal-archive-recorder.git` (an empty
repository becomes the package on first push).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def field(pkgbuild: str, name: str) -> str:
    m = re.search(rf"(?m)^{name}=(.*)$", pkgbuild)
    if not m:
        raise SystemExit(f"PKGBUILD has no {name}")
    return m.group(1).strip("'\"")


def source_url(pkgbuild: str) -> str:
    url, tag, ver = field(pkgbuild, "url"), field(pkgbuild, "_tag"), field(pkgbuild, "pkgver")
    return f"{url}/releases/download/{tag}/signal_archive_recorder-{ver}.tar.gz"


def with_checksum(pkgbuild: str, sha256: str) -> str:
    out, n = re.subn(r"(?m)^sha256sums=\(.*\)$", f"sha256sums=('{sha256}')", pkgbuild)
    if n != 1:
        raise SystemExit("PKGBUILD has no sha256sums line")
    # The AUR copy is the package itself, not our template: drop the template notes.
    return re.sub(r"(?m)^# (Template|tools/release|installer/aur|writes the).*\n", "", out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", type=Path, required=True, help="your clone of the AUR repo")
    parser.add_argument("--pkgbuild", type=Path, default=HERE / "PKGBUILD")
    args = parser.parse_args()
    template = args.pkgbuild.read_text(encoding="utf-8")
    url = source_url(template)
    print(f"downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        sha256 = hashlib.sha256(response.read()).hexdigest()
    pkgbuild = with_checksum(template, sha256)
    args.to.mkdir(parents=True, exist_ok=True)
    (args.to / "PKGBUILD").write_text(pkgbuild, encoding="utf-8")
    if not shutil.which("makepkg"):
        raise SystemExit("makepkg not found: run this on Arch to write .SRCINFO")
    srcinfo = subprocess.run(
        ["makepkg", "--printsrcinfo"], cwd=args.to, check=True, capture_output=True, text=True
    ).stdout
    (args.to / ".SRCINFO").write_text(srcinfo, encoding="utf-8")
    print(f"wrote PKGBUILD and .SRCINFO for {field(template, 'pkgver')} (sha256 {sha256[:12]}…)")
    print(f"review, then: cd {args.to} && git add PKGBUILD .SRCINFO && git commit && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
