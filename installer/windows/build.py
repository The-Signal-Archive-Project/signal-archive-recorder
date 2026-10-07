# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Build the Windows installer: PyInstaller folder, licenses, then Inno Setup.

Run on Windows from the repository root, in an environment with the app installed
(`pip install ".[gui]" pyinstaller`) and Inno Setup 6 available:

    python installer/windows/build.py

Writes dist/SignalArchiveRecorder-<version>-Setup.exe.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import distributions, version
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUILD = ROOT / "build" / "windows"
APP = BUILD / "dist" / "SignalArchiveRecorder"
LICENSE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)


def numeric(v: str) -> tuple[int, int, int]:
    """0.3.0b1 -> (0, 3, 0): Windows file versions are four numbers."""
    parts = [int(x) for x in re.findall(r"\d+", v.split("b")[0].split("rc")[0])[:3]]
    major, minor, patch = [*parts, 0, 0, 0][:3]
    return major, minor, patch


def version_info(v: str) -> str:
    a, b, c = numeric(v)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'The Signal Archive Project'),
      StringStruct('FileDescription', 'Signal Archive Recorder'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'SignalArchiveRecorder'),
      StringStruct('LegalCopyright', 'Signal Archive Recorder contributors. MPL-2.0.'),
      StringStruct('OriginalFilename', 'SignalArchiveRecorder.exe'),
      StringStruct('ProductName', 'Signal Archive Recorder'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def collect_licenses(target: Path) -> int:
    """Every bundled Python distribution's license files, plus Python's own."""
    count = 0
    for dist in distributions():
        name = dist.metadata["Name"] or "unknown"
        for f in dist.files or []:
            if LICENSE_FILE.search(f.name) and not f.name.endswith((".py", ".pyc")):
                src = Path(str(dist.locate_file(f)))
                if src.is_file():
                    dest = target / name / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)
                    count += 1
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        (target / "Python").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(python_license, target / "Python" / "LICENSE.txt")
        count += 1
    return count


def iscc() -> str:
    for candidate in (
        shutil.which("iscc"),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("Inno Setup 6 (ISCC.exe) not found: install it from jrsoftware.org")


def main() -> int:
    v = version("signal-archive-recorder")
    print(f"building Signal Archive Recorder {v}", flush=True)
    (HERE / "version_info.txt").write_text(version_info(v), encoding="utf-8")
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
    n = collect_licenses(APP / "licenses")
    for name in ("LICENSE", "NOTICE", "README.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / name, APP / name)
    print(f"collected {n} license files", flush=True)
    a, b, c = numeric(v)
    subprocess.run(
        [
            iscc(),
            f"/DAppVersion={v}",
            f"/DNumericVersion={a}.{b}.{c}.0",
            f"/DSourceDir={APP}",
            f"/O{ROOT / 'dist'}",
            str(HERE / "signal-archive-recorder.iss"),
        ],
        check=True,
        env={**os.environ},
    )
    print(f"wrote {ROOT / 'dist' / f'SignalArchiveRecorder-{v}-Setup.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
