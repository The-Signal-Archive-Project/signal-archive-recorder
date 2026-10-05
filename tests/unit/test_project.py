# SPDX-License-Identifier: Apache-2.0
"""Stage 0 exit tests: packaging, version and license hygiene."""

import tomllib
from pathlib import Path

import pytest

import signal_archive_recorder

ROOT = Path(__file__).resolve().parents[2]
SPDX_LINE = "# SPDX-License-Identifier: Apache-2.0"


def test_version_exposed() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert signal_archive_recorder.__version__ == pyproject["project"]["version"]


def _python_files() -> list[Path]:
    return sorted(p for d in ("src", "tests", "tools") for p in (ROOT / d).rglob("*.py"))


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_spdx_headers(path: Path) -> None:
    first_line = path.read_text(encoding="utf-8").partition("\n")[0]
    assert first_line == SPDX_LINE, f"{path.relative_to(ROOT)} must start with {SPDX_LINE!r}"


def test_license_files_present() -> None:
    assert "Apache License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Signal Archive Recorder" in (ROOT / "NOTICE").read_text(encoding="utf-8")
