# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 0 exit tests: packaging, version and license hygiene."""

import tomllib
from pathlib import Path

import pytest

import signal_archive_recorder

ROOT = Path(__file__).resolve().parents[2]
SPDX_LINE = "# SPDX-License-Identifier: MPL-2.0"
MPL_NOTICE = "subject to the terms of the Mozilla Public License, v. 2.0"


def test_version_exposed() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert signal_archive_recorder.__version__ == pyproject["project"]["version"]


def _python_files() -> list[Path]:
    return sorted(p for d in ("src", "tests", "tools") for p in (ROOT / d).rglob("*.py"))


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_spdx_headers(path: Path) -> None:
    text, name = path.read_text(encoding="utf-8"), path.relative_to(ROOT)
    assert text.partition("\n")[0] == SPDX_LINE, f"{name} must start with {SPDX_LINE!r}"
    assert MPL_NOTICE in text[:400], f"{name} needs the MPL notice (Exhibit A)"


def test_license_files_present() -> None:
    assert "Mozilla Public License Version 2.0" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Signal Archive Recorder" in (ROOT / "NOTICE").read_text(encoding="utf-8")
