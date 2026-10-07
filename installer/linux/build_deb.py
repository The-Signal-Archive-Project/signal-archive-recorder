# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Build the Debian/Ubuntu package: a self-contained app under /opt.

Debian and Ubuntu ship Python versions and libraries older than the recorder needs
(Ubuntu 22.04 has Python 3.10), so the package carries its own: the same
PyInstaller folder as the Windows installer. It depends only on system libraries:
PortAudio, and what Qt needs to draw windows. Build it on the oldest supported
release (Ubuntu 22.04), so its C library is old enough for every newer one.

    python installer/linux/build_deb.py     # needs dpkg-deb, and the app + pyinstaller

Writes dist/signal-archive-recorder_<version>_amd64.deb with:
    /opt/signal-archive-recorder/            the app (both programs, libraries, licenses)
    /usr/bin/signal-archive-recorder         the command line
    /usr/bin/signal-archive-recorder-gui     the desktop app
    /usr/share/applications/…desktop, the icon, and /usr/share/doc/…/copyright
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUILD = ROOT / "build" / "linux"
NAME = "signal-archive-recorder"

DEPENDS = [
    "libc6 (>= 2.35)",
    "libportaudio2",
    # What Qt needs to show windows and the tray icon (X11 or Wayland via XWayland):
    "libegl1",
    "libgl1",
    "libfontconfig1",
    "libdbus-1-3",
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "libxcb-xinerama0",
    "libxcb-xkb1",
]
RECOMMENDS = ["gnome-keyring | kwalletmanager | keepassxc"]  # somewhere to keep the token
SUGGESTS = ["wsjtx"]


PRE_NAMES = {"a": "alpha", "b": "beta", "rc": "rc"}


def deb_version(v: str) -> str:
    """0.3.0b1 -> 0.3.0~beta1, so dpkg sorts a beta before its release."""
    m = re.fullmatch(r"(.*?\d)(a|b|rc)(\d+)", v)
    return f"{m[1]}~{PRE_NAMES[m[2]]}{m[3]}" if m else v


def control(v: str, installed_kb: int) -> str:
    return (
        f"Package: {NAME}\n"
        f"Version: {deb_version(v)}\n"
        "Section: hamradio\n"
        "Priority: optional\n"
        "Architecture: amd64\n"
        "Maintainer: The Signal Archive Project "
        "<https://github.com/The-Signal-Archive-Project>\n"
        f"Installed-Size: {installed_kb}\n"
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Recommends: {', '.join(RECOMMENDS)}\n"
        f"Suggests: {', '.join(SUGGESTS)}\n"
        "Homepage: https://github.com/The-Signal-Archive-Project/signal-archive-recorder\n"
        "Description: records ham radio receive audio for an open signal dataset\n"
        " Signal Archive Recorder runs beside WSJT-X and records exactly what your\n"
        " receiver hears, labelled with the band, mode and timing WSJT-X reports.\n"
        " With your consent, it contributes the recordings to the Signal Archive\n"
        " Project, an open dataset for research and development (CC BY 4.0).\n"
        " It only records while WSJT-X is running, and never controls the radio.\n"
    )


def copyright_file() -> str:
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: signal-archive-recorder\n"
        "Source: https://github.com/The-Signal-Archive-Project/signal-archive-recorder\n\n"
        "Files: *\n"
        "Copyright: Signal Archive Recorder contributors\n"
        "License: MPL-2.0\n"
        " The full license text is in /opt/signal-archive-recorder/LICENSE. Bundled\n"
        " third-party components and their licenses are listed in\n"
        " /opt/signal-archive-recorder/NOTICE and /opt/signal-archive-recorder/licenses/.\n"
    )


def folder_kb(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) // 1024 + 1


def main() -> int:
    if platform.machine() not in ("x86_64", "AMD64"):
        raise SystemExit("the .deb is built for amd64")
    v = version(NAME)
    print(f"building {NAME} {v} (Debian version {deb_version(v)})", flush=True)
    shutil.rmtree(BUILD, ignore_errors=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(BUILD / "dist"),
            "--workpath",
            str(BUILD / "work"),
            str(ROOT / "installer" / "pyinstaller" / "signal-archive-recorder.spec"),
        ],
        check=True,
    )
    sys.path.insert(0, str(ROOT / "installer" / "windows"))
    from build import collect_licenses  # the same license collection as Windows

    root = BUILD / "pkg"
    app = root / "opt" / NAME
    shutil.copytree(BUILD / "dist" / "SignalArchiveRecorder", app)
    collect_licenses(app / "licenses")
    for name in ("LICENSE", "NOTICE", "README.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / name, app / name)
    bindir = root / "usr" / "bin"
    bindir.mkdir(parents=True)
    os.symlink(f"/opt/{NAME}/signal-archive-recorder", bindir / NAME)
    os.symlink(f"/opt/{NAME}/SignalArchiveRecorder", bindir / f"{NAME}-gui")
    share = root / "usr" / "share"
    (share / "applications").mkdir(parents=True)
    shutil.copyfile(HERE / f"{NAME}.desktop", share / "applications" / f"{NAME}.desktop")
    icons = share / "icons" / "hicolor" / "256x256" / "apps"
    icons.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "src" / "signal_archive_recorder" / "data" / "icon.png", icons / f"{NAME}.png"
    )
    doc = share / "doc" / NAME
    doc.mkdir(parents=True)
    (doc / "copyright").write_text(copyright_file())
    for path in root.rglob("*"):  # dpkg wants tidy permissions
        if path.is_symlink():
            continue
        mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
        path.chmod(mode)
    debian = root / "DEBIAN"
    debian.mkdir()
    (debian / "control").write_text(control(v, folder_kb(root)))
    out = ROOT / "dist" / f"{NAME}_{deb_version(v)}_amd64.deb"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(out)], check=True
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
