# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The Linux packages agree with the app (version, commands, desktop entry).

The packages themselves are built, installed and smoke-tested in CI
(.github/workflows/linux-packages.yml); these checks run everywhere.
"""

import configparser
import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PKGBUILD = (ROOT / "installer" / "aur" / "PKGBUILD").read_text(encoding="utf-8")


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_deb = load("build_deb", ROOT / "installer" / "linux" / "build_deb.py")
aur = load("aur_update", ROOT / "installer" / "aur" / "update.py")


def test_debian_versions_sort_betas_first() -> None:
    assert build_deb.deb_version("0.2.0") == "0.2.0"
    assert build_deb.deb_version("0.3.0b1") == "0.3.0~beta1"
    assert build_deb.deb_version("0.3.0rc2") == "0.3.0~rc2"


def test_deb_control() -> None:
    control = build_deb.control("0.3.0b1", 300_000)
    assert "Version: 0.3.0~beta1\n" in control and "Architecture: amd64\n" in control
    depends = next(line for line in control.splitlines() if line.startswith("Depends:"))
    assert "libportaudio2" in depends and "libxcb-cursor0" in depends
    assert all(line.startswith(" ") for line in control.split("Description:")[1].splitlines()[1:])


def test_pkgbuild_follows_the_version() -> None:
    version = PYPROJECT["project"]["version"]
    from packaging.version import Version

    v = Version(version)
    names = {"b": "beta", "rc": "rc"}
    suffix = f"-{names[v.pre[0]]}.{v.pre[1]}" if v.pre else ""
    assert aur.field(PKGBUILD, "pkgver") == version
    assert aur.field(PKGBUILD, "_tag") == f"v{v.base_version}{suffix}"  # the SemVer tag
    assert aur.field(PKGBUILD, "pkgrel") == "1"


def test_pkgbuild_dependencies_cover_the_runtime_ones() -> None:
    names = {
        re.split(r"[<>=\[ ]", d)[0].lower().replace("_", "-")
        for d in PYPROJECT["project"]["dependencies"]
    }
    arch = {"huggingface-hub": "python-huggingface-hub"}
    for name in names:
        assert f"'{arch.get(name, 'python-' + name)}'" in PKGBUILD, name
    assert "'pyside6'" in PKGBUILD  # the desktop app is the main experience


def test_aur_checksum_and_url() -> None:
    out = aur.with_checksum(PKGBUILD, "ab" * 32)
    assert f"sha256sums=('{'ab' * 32}')" in out and "SKIP" not in out
    assert "Template" not in out  # the AUR copy drops our template notes
    url = aur.source_url(PKGBUILD)
    assert url.endswith(
        f"/releases/download/{aur.field(PKGBUILD, '_tag')}/"
        f"signal_archive_recorder-{aur.field(PKGBUILD, 'pkgver')}.tar.gz"
    )


def test_desktop_entry() -> None:
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str  # type: ignore[assignment,method-assign]
    entry.read(ROOT / "installer" / "linux" / "signal-archive-recorder.desktop", encoding="utf-8")
    d = entry["Desktop Entry"]
    assert d["Exec"] in PYPROJECT["project"]["gui-scripts"]  # the command both packages install
    assert d["Icon"] == "signal-archive-recorder" and d["Terminal"] == "false"
    assert "HamRadio" in d["Categories"].split(";")
