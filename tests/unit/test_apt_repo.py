# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""installer/apt/build_repo.py: channels, package index and Release files.

The signed repository is built, installed from with apt on four distributions and
published by .github/workflows/apt-repo.yml; these checks run everywhere.
"""

import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_repo", ROOT / "installer/apt/build_repo.py")
assert _spec and _spec.loader
repo = importlib.util.module_from_spec(_spec)
sys.modules["build_repo"] = repo
_spec.loader.exec_module(repo)

CONTROL = """Package: signal-archive-recorder
Version: {version}
Architecture: amd64
Maintainer: Test <t@example.org>
Description: records ham radio receive audio
 A second line.
"""


def make_deb(version: str) -> bytes:
    """A minimal .deb: an ar archive with debian-binary and control.tar.gz."""
    control = io.BytesIO()
    with tarfile.open(fileobj=control, mode="w:gz") as t:
        data = CONTROL.format(version=version).encode()
        info = tarfile.TarInfo("./control")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))

    def member(name: str, body: bytes) -> bytes:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(body):<10}`\n".encode()
        return header + body + (b"\n" if len(body) % 2 else b"")

    return (
        b"!<arch>\n"
        + member("debian-binary", b"2.0\n")
        + member("control.tar.gz", control.getvalue())
    )


def release(tag: str, *, draft: bool = False, deb: bool = True) -> dict[str, Any]:
    name = f"signal-archive-recorder_{tag.removeprefix('v').replace('-', '.')}_amd64.deb"
    assets = [{"name": name, "browser_download_url": f"https://x/{tag}/{name}"}] if deb else []
    return {"tag_name": tag, "draft": draft, "assets": assets}


def test_versions_sort_betas_before_their_release() -> None:
    tags = ["v0.3.0", "v0.3.0-beta.2", "v0.3.0-rc.1", "v0.3.0-beta.10", "v0.2.0", "v1.0.0-beta.1"]
    ordered = sorted(tags, key=lambda t: repo.version_key(t)[0])
    assert ordered == [
        "v0.2.0",
        "v0.3.0-beta.2",
        "v0.3.0-beta.10",
        "v0.3.0-rc.1",
        "v0.3.0",
        "v1.0.0-beta.1",
    ]
    assert repo.version_key("v0.3.0-beta.2")[1] and not repo.version_key("v0.3.0")[1]


@pytest.mark.parametrize(
    ("tags", "stable", "beta"),
    [
        (["v0.2.0", "v0.3.0-beta.2"], "v0.2.0", "v0.3.0-beta.2"),
        (["v0.3.0-beta.2", "v0.3.0"], "v0.3.0", "v0.3.0"),  # the release overtakes the betas
        (["v0.3.0", "v0.4.0-beta.1"], "v0.3.0", "v0.4.0-beta.1"),
        (["v0.3.0-beta.1"], None, "v0.3.0-beta.1"),
    ],
)
def test_channels(tags: list[str], stable: str | None, beta: str) -> None:
    chosen = repo.choose(repo.debs_in([release(t) for t in tags]))
    assert (chosen["stable"].tag if chosen["stable"] else None) == stable
    assert chosen["beta"].tag == beta


def test_drafts_and_releases_without_a_deb_are_skipped() -> None:
    debs = repo.debs_in(
        [release("v0.9.0", draft=True), release("v0.2.0", deb=False), release("v0.3.0-beta.2")]
    )
    assert [d.tag for d in debs] == ["v0.3.0-beta.2"]


def test_package_entry(tmp_path: Path) -> None:
    deb = make_deb("0.3.0~beta2")
    path = tmp_path / repo.POOL / "x_0.3.0.beta2_amd64.deb"
    path.parent.mkdir(parents=True)
    path.write_bytes(deb)
    entry = repo.packages_entry(path, tmp_path)
    assert entry.startswith("Package: signal-archive-recorder\nVersion: 0.3.0~beta2\n")
    assert f"Filename: {repo.POOL}/x_0.3.0.beta2_amd64.deb\n" in entry
    assert f"SHA256: {hashlib.sha256(deb).hexdigest()}\n" in entry
    assert entry.index("SHA256:") < entry.index("Description:")  # as dpkg-scanpackages orders
    assert entry.endswith(" A second line.\n")


def test_full_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    debs = {"v0.3.0-beta.2": make_deb("0.3.0~beta2"), "v0.2.0": make_deb("0.2.0")}
    monkeypatch.setattr(
        repo.urllib.request,
        "urlretrieve",
        lambda url, path: Path(path).write_bytes(debs[url.split("/")[3]]),
    )
    site = tmp_path / "site"
    chosen = repo.build(site, [release("v0.3.0-beta.2"), release("v0.2.0")])
    assert chosen["beta"].tag == "v0.3.0-beta.2" and chosen["stable"].tag == "v0.2.0"
    for suite, version in (("beta", "0.3.0~beta2"), ("stable", "0.2.0")):
        dist = site / "dists" / suite
        packages = (dist / "main/binary-amd64/Packages").read_bytes()
        assert f"Version: {version}\n".encode() in packages
        assert gzip.decompress((dist / "main/binary-amd64/Packages.gz").read_bytes()) == packages
        text = (dist / "Release").read_text(encoding="utf-8")
        assert f"Suite: {suite}\n" in text and "Components: main\n" in text
        digest = hashlib.sha256(packages).hexdigest()
        assert f" {digest} {len(packages):>8} main/binary-amd64/Packages\n" in text
    assert (
        (site / "signal-archive.asc")
        .read_text(encoding="utf-8")
        .startswith("-----BEGIN PGP PUBLIC KEY BLOCK")
    )
    page = (site / "index.html").read_text(encoding="utf-8")
    assert "{" + "site}" not in page and repo.SITE_URL in page and "v0.3.0-beta.2" in page
    assert (site / ".nojekyll").exists()


def test_an_empty_channel_is_still_valid(tmp_path: Path) -> None:
    repo.write_channel(tmp_path, "stable", None, dt.datetime(2026, 10, 7, tzinfo=dt.UTC))
    dist = tmp_path / "dists" / "stable"
    assert (dist / "main/binary-amd64/Packages").read_bytes() == b""
    assert "Date: Wed, 07 Oct 2026 00:00:00 UTC" in (dist / "Release").read_text(encoding="utf-8")


def test_published_key_is_the_project_key() -> None:
    workflow = (ROOT / ".github/workflows/apt-repo.yml").read_text(encoding="utf-8")
    fingerprint = "9EC64E0C16932FCCC381381C38FA930D4522E702"
    assert f"KEY_ID: {fingerprint}" in workflow
    page = (ROOT / "installer/apt/index.template.html").read_text(encoding="utf-8")
    assert "9EC6 4E0C 1693 2FCC C381 381C 38FA 930D 4522 E702" in page
