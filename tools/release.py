# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Cut a release, following Semantic Versioning (see CLAUDE.md, "Versions").

    python tools/release.py next minor               # what would the next version be?
    python tools/release.py prepare minor            # 0.2.0 -> 0.3.0: branch, commit, PR
    python tools/release.py prepare minor --pre beta # 0.2.0 -> 0.3.0-beta.1
    python tools/release.py prepare pre              # 0.3.0-beta.1 -> 0.3.0-beta.2
    python tools/release.py prepare pre --pre rc     # 0.3.0-beta.2 -> 0.3.0-rc.1
    python tools/release.py prepare final            # 0.3.0-rc.1 -> 0.3.0
    python tools/release.py publish --notes notes.md # after the release PR is merged
    python tools/release.py publish --draft          # or: a draft release to edit on GitHub

`prepare` (on an up-to-date main, with a clean working tree):
  - bumps pyproject.toml (PEP 440 spelling: 0.3.0b1),
  - moves CHANGELOG's Unreleased entries under the new version, with compare links,
  - re-pins the README's install commands (final releases only: betas are for testers),
  - updates the AUR PKGBUILD's pkgver if there is one,
  - commits (signed off) on release-vX.Y.Z, pushes and opens the PR.
`publish` (on main, once that PR is merged):
  - tags the merge commit, builds the wheel and sdist from a clean checkout of it,
  - creates the GitHub release (marked as a pre-release for betas and release
    candidates) with the notes and files attached. Publishing it starts the
    installer workflows, which attach their packages.
Add --dry-run to either to see what would happen without changing anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/The-Signal-Archive-Project/signal-archive-recorder"
PRE_NAMES = {"b": "beta", "rc": "rc"}
PRE_CODES = {"beta": "b", "rc": "rc"}


class ReleaseError(Exception):
    """Something about the repository or the request prevents a release."""


# -- versions ------------------------------------------------------------------------


def semver(v: Version) -> str:
    """The SemVer spelling used for tags and the changelog: 0.3.0b1 -> 0.3.0-beta.1."""
    base = ".".join(str(x) for x in (*v.release, 0, 0, 0)[:3])
    if v.pre:
        code, number = v.pre
        return f"{base}-{PRE_NAMES[code]}.{number}"
    return base


def tag(v: Version) -> str:
    return f"v{semver(v)}"


def bump(current: Version, kind: str, pre: str | None = None) -> Version:
    """The next version. `kind` is major, minor, patch, pre or final."""
    if current.pre and current.pre[0] not in PRE_NAMES:
        raise ReleaseError(f"{current} isn't a beta or release candidate")
    if pre is not None and pre not in PRE_CODES:
        raise ReleaseError("--pre must be beta or rc")
    major, minor, patch = (*current.release, 0, 0, 0)[:3]
    if kind in ("major", "minor", "patch"):
        if current.is_prerelease:
            raise ReleaseError(
                f"{semver(current)} is a pre-release: use `pre` for the next one, or `final` "
                "to release it"
            )
        if kind == "major":
            major, minor, patch = major + 1, 0, 0
        elif kind == "minor":
            minor, patch = minor + 1, 0
        else:
            patch += 1
        suffix = f"{PRE_CODES[pre]}1" if pre else ""
        return Version(f"{major}.{minor}.{patch}{suffix}")
    if kind == "pre":
        if not current.pre:
            raise ReleaseError(
                f"{semver(current)} isn't a pre-release: start one with e.g. `minor --pre beta`"
            )
        code, number = current.pre
        wanted = PRE_CODES[pre] if pre else code
        if wanted == code:
            return Version(f"{major}.{minor}.{patch}{code}{number + 1}")
        if (code, wanted) == ("b", "rc"):
            return Version(f"{major}.{minor}.{patch}rc1")
        raise ReleaseError("a release candidate can't go back to beta")
    if kind == "final":
        if not current.pre:
            raise ReleaseError(f"{semver(current)} is already a final release")
        return Version(f"{major}.{minor}.{patch}")
    raise ReleaseError(f"unknown bump {kind!r}: use major, minor, patch, pre or final")


# -- file edits (pure: text in, text out) ---------------------------------------------


def edit_pyproject(text: str, new: Version) -> str:
    out, n = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{new}"', text, count=1)
    if n != 1:
        raise ReleaseError("no version line in pyproject.toml")
    return out


def unreleased_entries(changelog: str) -> str:
    m = re.search(r"(?ms)^## \[Unreleased\]\n(.*?)(?=^## \[)", changelog)
    return m.group(1).strip() if m else ""


def edit_changelog(text: str, old: Version, new: Version, date: dt.date) -> str:
    if not unreleased_entries(text):
        raise ReleaseError("CHANGELOG.md has nothing under [Unreleased]: nothing to release")
    heading = f"## [{semver(new)}] - {date.isoformat()}"
    text = text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{heading}\n", 1)
    text = re.sub(r"\n{3,}(?=## \[)", "\n\n", text)  # tidy the blank lines between headings
    old_link = f"[Unreleased]: {REPO_URL}/compare/{tag(old)}...HEAD"
    if old_link not in text:
        raise ReleaseError(f"CHANGELOG.md has no link line: {old_link}")
    return text.replace(
        old_link,
        f"[Unreleased]: {REPO_URL}/compare/{tag(new)}...HEAD\n"
        f"[{semver(new)}]: {REPO_URL}/compare/{tag(old)}...{tag(new)}",
    )


def edit_readme(text: str, last_final: Version, new: Version) -> str:
    """Point the README at a new final release (pre-releases leave it alone)."""
    if new.is_prerelease:
        return text
    return text.replace(tag(last_final), tag(new))


def edit_pkgbuild(text: str, new: Version) -> str:
    text = re.sub(r"(?m)^pkgver=.*$", f"pkgver={new}", text, count=1)
    text = re.sub(r"(?m)^pkgrel=.*$", "pkgrel=1", text, count=1)
    return re.sub(r"(?m)^_tag=.*$", f"_tag={tag(new)}", text, count=1)


@dataclass
class Plan:
    old: Version
    new: Version
    files: dict[Path, str]  # path -> new content


def current_version(root: Path = ROOT) -> Version:
    m = re.search(r'(?m)^version = "([^"]+)"$', (root / "pyproject.toml").read_text())
    if not m:
        raise ReleaseError("no version in pyproject.toml")
    return Version(m.group(1))


def last_final(root: Path, current: Version) -> Version:
    """The version the README currently points at (the last final release)."""
    m = re.search(r"@v(\d+\.\d+\.\d+)\b(?!-)", (root / "README.md").read_text())
    return Version(m.group(1)) if m else current


def plan(root: Path, kind: str, pre: str | None, date: dt.date) -> Plan:
    old = current_version(root)
    new = bump(old, kind, pre)
    files = {
        root / "pyproject.toml": edit_pyproject((root / "pyproject.toml").read_text(), new),
        root / "CHANGELOG.md": edit_changelog((root / "CHANGELOG.md").read_text(), old, new, date),
        root / "README.md": edit_readme(
            (root / "README.md").read_text(), last_final(root, old), new
        ),
    }
    pkgbuild = root / "installer" / "aur" / "PKGBUILD"
    if pkgbuild.exists():
        files[pkgbuild] = edit_pkgbuild(pkgbuild.read_text(), new)
    return Plan(old, new, files)


# -- git and GitHub --------------------------------------------------------------------


def sh(*args: str, capture: bool = True, cwd: Path = ROOT) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=capture)
    if result.returncode != 0:
        raise ReleaseError(f"{' '.join(args)} failed:\n{result.stderr or result.stdout}")
    return result.stdout.strip() if capture else ""


def require_clean_main() -> None:
    if sh("git", "status", "--porcelain"):
        raise ReleaseError("the working tree has changes: commit or stash them first")
    if sh("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise ReleaseError("switch to main first (git switch main)")
    sh("git", "fetch", "-q", "origin")
    if sh("git", "rev-parse", "HEAD") != sh("git", "rev-parse", "origin/main"):
        raise ReleaseError("main isn't the same as origin/main: git pull first")


def cmd_next(args: argparse.Namespace) -> int:
    old = current_version()
    new = bump(old, args.kind, args.pre)
    print(f"{semver(old)} -> {semver(new)}  (tag {tag(new)}, pyproject {new})")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    if not args.dry_run:
        require_clean_main()
    p = plan(ROOT, args.kind, args.pre, dt.date.today())
    print(f"Release {semver(p.old)} -> {semver(p.new)} (tag {tag(p.new)})")
    for path, content in p.files.items():
        changed = path.read_text() != content
        print(f"  {'update' if changed else 'same  '} {path.relative_to(ROOT)}")
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0
    branch = f"release-{tag(p.new)}"
    sh("git", "switch", "-q", "-c", branch)
    for path, content in p.files.items():
        path.write_text(content)
    sh("git", "add", *(str(path) for path in p.files))
    sh("git", "commit", "-q", "-s", "-m", f"Release {tag(p.new)}")
    print(f"Committed on {branch}.")
    if args.no_pr:
        print(f"Push and open the PR yourself: git push -u origin {branch}")
        return 0
    sh("git", "push", "-q", "-u", "origin", branch)
    kind = "pre-release" if p.new.is_prerelease else "release"
    body = (
        f"Version bump for the **{tag(p.new)}** {kind} ({semver(p.old)} -> {semver(p.new)}), "
        "made by `tools/release.py prepare`.\n\n"
        f"After merging: `python tools/release.py publish --notes <file>` (or `--draft`).\n"
    )
    url = sh(
        "gh", "pr", "create", "--base", "main", "--title", f"Release {tag(p.new)}", "--body", body
    )
    print(f"Opened {url}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    if not args.notes and not args.draft:
        raise ReleaseError("give --notes FILE (high-level, see CLAUDE.md) or --draft")
    if not args.dry_run:
        require_clean_main()
    v = current_version()
    t = tag(v)
    if sh("git", "tag", "--list", t):
        raise ReleaseError(f"{t} already exists: versions are never reused")
    commit = sh("git", "rev-parse", "HEAD")
    print(f"Publish {t} from {commit[:7]} ({'pre-release' if v.is_prerelease else 'release'})")
    if args.dry_run:
        print("Dry run: nothing built or published.")
        return 0
    work = Path(tempfile.mkdtemp(prefix="release-"))
    try:
        sh("git", "worktree", "add", "-q", str(work / "src"), commit)
        sh(sys.executable, "-m", "build", "--outdir", str(work / "dist"), str(work / "src"))
        files = [str(p) for p in sorted((work / "dist").iterdir())]
        notes = (
            ["--notes-file", args.notes]
            if args.notes
            else [
                "--notes",
                "DRAFT: rewrite as a high-level overview (CLAUDE.md, Release notes)."
                "\n\n" + unreleased_or_section(v),
            ]
        )
        sh(
            "gh",
            "release",
            "create",
            t,
            "--target",
            commit,
            "--title",
            f"Signal Archive Recorder {t}",
            *notes,
            *(["--prerelease"] if v.is_prerelease else []),
            *(["--draft"] if args.draft else []),
            *files,
        )
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(work / "src")], cwd=ROOT)
        shutil.rmtree(work, ignore_errors=True)
    print(f"{'Drafted' if args.draft else 'Published'} {REPO_URL}/releases/tag/{t}")
    return 0


def unreleased_or_section(v: Version) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"(?ms)^## \[{re.escape(semver(v))}\][^\n]*\n(.*?)(?=^## \[|^\[)", text)
    return m.group(1).strip() if m else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("next", cmd_next), ("prepare", cmd_prepare)):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=["major", "minor", "patch", "pre", "final"])
        p.add_argument("--pre", choices=["beta", "rc"], help="make (or move to) a pre-release")
        p.set_defaults(func=func)
        if name == "prepare":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--no-pr", action="store_true", help="commit only; don't push or PR")
    p = sub.add_parser("publish")
    p.add_argument("--notes", help="release notes file (Markdown)")
    p.add_argument("--draft", action="store_true", help="create a draft release to edit")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_publish)
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
