# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Keeping private details out of everything that may be uploaded.

Public files are built from an allow-list (the builder copies named fields only),
so this module is the second line of defence: it scrubs machine details out of
the few free-text strings that remain, and redacts the operator's own callsign
(and, if withheld, grid) from decoded messages when they chose not to share it.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import socket
from collections.abc import Iterable
from pathlib import Path

from signal_archive_recorder.metadata.settings import StationSettings

REDACTED = "<redacted>"
OWN_CALL = "<OWN_CALL>"
OWN_GRID = "<OWN_GRID>"
_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|/(?:home|Users|root|tmp|var|etc|opt|mnt|media)/)\S*"
)


def machine_secrets() -> list[str]:
    """Strings that identify this computer or its user."""
    found = {socket.gethostname(), str(Path.home())}
    with contextlib.suppress(KeyError, OSError):  # no user entry, e.g. in some containers
        found.add(getpass.getuser())
    for var in ("USER", "USERNAME", "COMPUTERNAME", "HOSTNAME", "LOGNAME"):
        if value := os.environ.get(var):
            found.add(value)
    return sorted((s for s in found if len(s) >= 3), key=len, reverse=True)


class Scrubber:
    def __init__(self, extra: Iterable[str] = ()) -> None:
        secrets = [*machine_secrets(), *(s for s in extra if s and len(s) >= 3)]
        self._secrets = sorted(set(secrets), key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(s) for s in self._secrets), re.IGNORECASE)
            if self._secrets
            else None
        )

    def text(self, value: str) -> str:
        value = _ABSOLUTE_PATH.sub(REDACTED, value)
        return self._pattern.sub(REDACTED, value) if self._pattern else value


def _call_pattern(callsign: str) -> re.Pattern[str]:
    # The call on its own or with a portable prefix/suffix (VE3/W9XYZ, W9XYZ/P).
    return re.compile(
        rf"(?<![A-Z0-9/])(?:[A-Z0-9]+/)?{re.escape(callsign)}(?:/[A-Z0-9]+)?(?![A-Z0-9/])",
        re.IGNORECASE,
    )


class DecodeRedactor:
    """Redacts the operator from decoded messages, as far as they chose not to share.

    Only messages containing the operator's own callsign are touched: their call
    becomes <OWN_CALL> unless shared, and their 4-character grid becomes <OWN_GRID>
    if the grid is withheld. Other stations' messages are never changed, even when
    they happen to be in the same grid square.
    """

    def __init__(self, settings: StationSettings) -> None:
        self._call = _call_pattern(settings.callsign) if settings.callsign else None
        self._redact_call = not settings.share_callsign
        grid4 = settings.grid[:4] if settings.grid else None
        self._grid = (
            re.compile(rf"(?<![A-Z0-9]){re.escape(grid4)}(?![A-Z0-9])", re.IGNORECASE)
            if grid4 and settings.grid_precision == 0
            else None
        )

    def text(self, message: str) -> tuple[str, bool]:
        """(text with the operator redacted, whether anything was changed)."""
        if self._call is None or not self._call.search(message):
            return message, False
        out, changed = message, 0
        if self._redact_call:
            out, changed = self._call.subn(OWN_CALL, out)
        if self._grid:
            out, n = self._grid.subn(OWN_GRID, out)
            changed += n
        return out, changed > 0
