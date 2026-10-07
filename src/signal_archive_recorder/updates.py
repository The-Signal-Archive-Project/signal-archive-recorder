# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Tell the operator when a newer version is out. Never downloads or installs anything.

Asks GitHub for the project's releases at start and once a day. Someone running a
beta also hears about newer betas; someone on a stable version only about stable
releases. The request carries nothing about the station (GitHub sees the computer's
IP address, as for any web request); `[updates] check = false` turns it off.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from signal_archive_recorder import __version__

log = logging.getLogger(__name__)

RELEASES_URL = (
    "https://api.github.com/repos/The-Signal-Archive-Project/signal-archive-recorder/releases"
    "?per_page=30"
)
DAY_S = 86_400.0
FIRST_CHECK_S = 15.0  # let start-up finish first

Fetch = Callable[[], list[dict[str, Any]]]
Level = Literal["info", "recommended", "important"]


@dataclass(frozen=True)
class Release:
    version: Version
    tag: str
    url: str
    prerelease: bool

    @property
    def label(self) -> str:
        return self.tag.removeprefix("v")


@dataclass(frozen=True)
class Notice:
    """What to tell the operator about a newer version, worded for where they are."""

    level: Level  # important: please update now; recommended: fixes; info: new features
    title: str
    text: str


def _label(v: Version) -> str:
    base = v.base_version
    if v.pre:
        name = {"a": "alpha", "b": "beta", "rc": "rc"}[v.pre[0]]
        return f"{base}-{name}.{v.pre[1]}"
    return base


def display_version(version: str = __version__) -> str:
    """The version as people see it: 0.3.0b2 -> 0.3.0-beta.2."""
    return _label(Version(version))


def notice(release: Release, current: str = __version__) -> Notice:
    mine = Version(current)
    new = release.version
    if mine.is_prerelease and not new.is_prerelease:
        return Notice(
            "important",
            f"Version {release.label} is out: please switch from the beta",
            f"You're running a beta ({_label(mine)}). The finished release {release.label} is "
            "out, and betas aren't supported once it is, so please update now. From the "
            "release on, your recordings go to the real archive.",
        )
    if mine.is_prerelease:
        return Notice(
            "recommended",
            f"Beta {release.label} is available",
            f"A newer beta than yours ({_label(mine)}) is out with fixes. Please update, so "
            "that what you test and report is the latest build.",
        )
    if (new.major, new.minor) == (mine.major, mine.minor):
        return Notice(
            "recommended",
            f"Version {release.label} fixes bugs",
            f"It fixes problems found in {_label(mine)}. Updating is recommended.",
        )
    return Notice(
        "info",
        f"Version {release.label} is available",
        f"It has new features since {_label(mine)}. See what's new on the download page.",
    )


def fetch_releases(timeout_s: float = 10.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"signal-archive-recorder/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("unexpected answer from GitHub")
    return data


def newest(releases: list[dict[str, Any]], current: str = __version__) -> Release | None:
    """The newest release worth telling this installation about, if it's newer."""
    mine = Version(current)
    best: Release | None = None
    for r in releases:
        if r.get("draft"):
            continue
        tag = str(r.get("tag_name", ""))
        try:
            version = Version(tag.removeprefix("v"))
        except InvalidVersion:
            continue
        if version.is_prerelease and not mine.is_prerelease:
            continue  # betas are only offered to people already testing
        if version > mine and (best is None or version > best.version):
            url = str(r.get("html_url") or "")
            best = Release(version, tag, url, bool(r.get("prerelease")))
    return best


class UpdateChecker:
    """Checks in the background; `on_new` is called once per newer version found."""

    def __init__(
        self,
        on_new: Callable[[Release], None],
        *,
        fetch: Fetch = fetch_releases,
        current: str = __version__,
        first_check_s: float = FIRST_CHECK_S,
        interval_s: float = DAY_S,
    ) -> None:
        self._on_new = on_new
        self._fetch = fetch
        self._current = current
        self._first = first_check_s
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.announced: Release | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

    def check_now(self) -> Release | None:
        try:
            release = newest(self._fetch(), self._current)
        except Exception as exc:  # offline, rate-limited...: try again tomorrow
            log.info("update check failed: %s", exc)
            return None
        if release is not None and release != self.announced:
            self.announced = release
            log.info("a newer version is available: %s (%s)", release.label, release.url)
            self._on_new(release)
        return release

    def _run(self) -> None:
        if self._stop.wait(self._first):
            return
        while True:
            self.check_now()
            if self._stop.wait(self._interval):
                return
