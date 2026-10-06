# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""License and attribution: in each chunk's FLAC tags and in its metadata.

Recordings are contributed under CC BY 4.0. The contributor is credited by callsign
only when they chose to share it; otherwise as an anonymous project contributor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from signal_archive_recorder import __version__
from signal_archive_recorder.metadata.settings import StationSettings

LICENSE_ID = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
PROJECT = "Signal Archive Project"
ANONYMOUS = f"{PROJECT} contributor"
COMMENT = (
    f"Raw receive audio recorded by Signal Archive Recorder for the {PROJECT}. "
    "Reuse requires attribution under CC BY 4.0."
)


def attribution(settings: StationSettings) -> str:
    return settings.callsign if settings.callsign and settings.share_callsign else ANONYMOUS


def rights(settings: StationSettings) -> dict[str, Any]:
    """The `rights` section of a chunk's metadata."""
    return {"license": LICENSE_ID, "license_url": LICENSE_URL, "attribution": attribution(settings)}


def flac_tags(
    settings: StationSettings, *, session_id: str, chunk_id: str, first_sample_ns: int
) -> dict[str, str]:
    """Vorbis comments, by libsndfile's names (stored as TITLE=, LICENSE=, ...)."""
    when = datetime.fromtimestamp(first_sample_ns // 1_000_000_000, UTC)
    who = attribution(settings)
    return {
        "title": chunk_id,
        "album": f"{PROJECT} session {session_id}",
        "artist": who,
        "copyright": f"© {when.year} {who}, licensed CC BY 4.0",
        "license": f"{LICENSE_ID} {LICENSE_URL}",
        "comment": COMMENT,
        "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "software": f"signal-archive-recorder {__version__}",
    }
