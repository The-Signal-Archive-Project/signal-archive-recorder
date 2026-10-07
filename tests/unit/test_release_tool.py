# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""tools/release.py: SemVer bumps and the files a release changes."""

import datetime as dt
import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("release", ROOT / "tools" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
sys.modules["release"] = release  # dataclasses look their module up while being defined
_spec.loader.exec_module(release)

V = Version
DAY = dt.date(2026, 10, 7)


@pytest.mark.parametrize(
    ("current", "kind", "pre", "expected"),
    [
        ("0.2.0", "patch", None, "0.2.1"),
        ("0.2.0", "minor", None, "0.3.0"),
        ("0.2.3", "minor", None, "0.3.0"),
        ("0.2.3", "major", None, "1.0.0"),
        ("0.2.0", "minor", "beta", "0.3.0-beta.1"),
        ("0.2.0", "patch", "rc", "0.2.1-rc.1"),
        ("0.3.0b1", "pre", None, "0.3.0-beta.2"),
        ("0.3.0b2", "pre", "rc", "0.3.0-rc.1"),
        ("0.3.0rc1", "pre", None, "0.3.0-rc.2"),
        ("0.3.0rc2", "final", None, "0.3.0"),
        ("0.3.0b4", "final", None, "0.3.0"),
    ],
)
def test_bumps(current: str, kind: str, pre: str | None, expected: str) -> None:
    assert release.semver(release.bump(V(current), kind, pre)) == expected


@pytest.mark.parametrize(
    ("current", "kind", "pre", "error"),
    [
        ("0.3.0b1", "minor", None, "is a pre-release"),
        ("0.2.0", "pre", None, "isn't a pre-release"),
        ("0.2.0", "final", None, "already a final release"),
        ("0.3.0rc1", "pre", "beta", "can't go back to beta"),
    ],
)
def test_impossible_bumps(current: str, kind: str, pre: str | None, error: str) -> None:
    with pytest.raises(release.ReleaseError, match=error):
        release.bump(V(current), kind, pre)


def test_spellings() -> None:
    v = V("0.3.0b1")
    assert (str(v), release.semver(v), release.tag(v)) == (
        "0.3.0b1",
        "0.3.0-beta.1",
        "v0.3.0-beta.1",
    )
    assert V(release.tag(v).removeprefix("v")) == v  # the update check reads tags back


def repo_copy(tmp_path: Path, version: str = "0.2.0") -> Path:
    """Copies of the real files, rewound to a known final version (the repository's
    own version moves on with every release)."""
    for name in ("pyproject.toml", "CHANGELOG.md", "README.md", "TESTING.md"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    v = V(version)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        release.edit_pyproject(pyproject.read_text(encoding="utf-8"), v), encoding="utf-8"
    )
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        re.sub(
            r"(?m)^\[Unreleased\]: .*$",
            f"[Unreleased]: {release.REPO_URL}/compare/{release.tag(v)}...HEAD",
            changelog.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )
    readme = tmp_path / "README.md"
    readme.write_text(
        re.sub(r"@v\d+\.\d+\.\d+\b(?!-)", f"@{release.tag(v)}", readme.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    return tmp_path


def test_prepare_a_final_release(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not release.unreleased_entries(changelog):  # make sure there's something to release
        changelog = changelog.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n- x\n", 1)
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    old = release.current_version(root)
    p = release.plan(root, "minor", None, DAY)
    new = release.semver(p.new)
    text = p.files[root / "CHANGELOG.md"]
    assert f"## [Unreleased]\n\n## [{new}] - 2026-10-07\n" in text
    assert release.unreleased_entries(text) == ""
    assert f"compare/v{new}...HEAD" in text
    assert f"[{new}]: {release.REPO_URL}/compare/{release.tag(old)}...v{new}" in text
    assert f'version = "{p.new}"' in p.files[root / "pyproject.toml"]
    readme = p.files[root / "README.md"]
    assert f"@v{new}" in readme and f"@{release.tag(old)}" not in readme


def test_a_beta_leaves_the_readme_alone(tmp_path: Path) -> None:
    root = repo_copy(tmp_path)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        changelog.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n- y\n", 1),
        encoding="utf-8",
    )
    p = release.plan(root, "minor", "beta", DAY)
    assert p.files[root / "README.md"] == (root / "README.md").read_text(encoding="utf-8")
    assert f'version = "{p.new}"' in p.files[root / "pyproject.toml"]
    assert p.new.is_prerelease


def test_nothing_to_release(tmp_path: Path) -> None:
    text = "## [Unreleased]\n\n## [0.2.0] - 2026-10-06\n- old\n"
    with pytest.raises(release.ReleaseError, match="nothing to release"):
        release.edit_changelog(text, V("0.2.0"), V("0.3.0"), DAY)


def test_pkgbuild_follows() -> None:
    text = "pkgname=x\npkgver=0.2.0\npkgrel=3\n_tag=v0.2.0\n"
    out = release.edit_pkgbuild(text, V("0.3.0b1"))
    assert "pkgver=0.3.0b1\npkgrel=1\n_tag=v0.3.0-beta.1\n" in out


def test_next_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "current_version", lambda root=None: V("0.2.0"))
    assert release.main(["next", "minor", "--pre", "beta"]) == 0
    assert "-beta.1" in capsys.readouterr().out


def test_publish_latest_option(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "require_clean_main", lambda: None)
    monkeypatch.setattr(release, "current_version", lambda root=None: V("0.3.0b2"))

    def fake_sh(*args: str, **kw: object) -> str:
        calls.append(args)
        return "" if args[:2] == ("git", "tag") else "abc1234def"

    monkeypatch.setattr(release, "sh", fake_sh)
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(release.Path, "iterdir", lambda self: iter([]))
    assert release.main(["publish", "--notes", "n.md", "--latest"]) == 0
    [create] = [c for c in calls if c[:3] == ("gh", "release", "create")]
    assert "--latest" in create and "--prerelease" not in create
    calls.clear()
    assert release.main(["publish", "--notes", "n.md"]) == 0
    [create] = [c for c in calls if c[:3] == ("gh", "release", "create")]
    assert "--prerelease" in create and "--latest" not in create


def test_beta_pins_follow_each_new_version() -> None:
    old, last_final = V("0.3.0b2"), V("0.2.0")
    text = "stable @v0.2.0 and beta @v0.3.0-beta.2\n"
    nxt = release.edit_readme(text, last_final, V("0.3.0b3"), old)
    assert nxt == "stable @v0.2.0 and beta @v0.3.0-beta.3\n"  # the stable pin stays
    final = release.edit_readme(text, last_final, V("0.3.0"), old)
    assert final == "stable @v0.3.0 and beta @v0.3.0\n"  # both move to the release
