# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Fail if any installed distribution is GPL or AGPL licensed.

LGPL is allowed (as a separately installed or dynamically loaded library) but
must be listed in NOTICE. Run in CI against the project's installed environment.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import Distribution, distributions

# Matches GPL/AGPL in SPDX ids ("GPL-3.0-only", "AGPL-3.0"), classifiers
# ("GNU General Public License v3 (GPLv3)") and free text, but not LGPL.
_FORBIDDEN = re.compile(
    r"(?<!\w)A?GPL|GNU (Affero )?General Public License",
    re.IGNORECASE,
)


def license_strings(dist: Distribution) -> list[str]:
    """Every license declaration a distribution makes in its metadata."""
    meta = dist.metadata
    found = [v for key in ("License-Expression", "License") for v in meta.get_all(key) or []]
    found += [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return found


def is_forbidden(license_text: str) -> bool:
    """True for GPL/AGPL declarations; LGPL and permissive licenses pass."""
    return bool(_FORBIDDEN.search(license_text))


def main() -> int:
    bad: list[str] = []
    for dist in distributions():
        hits = [s for s in license_strings(dist) if is_forbidden(s)]
        if hits:
            bad.append(f"{dist.metadata['Name']} {dist.version}: {'; '.join(hits)}")
    if bad:
        print("GPL/AGPL-licensed distributions found (not allowed, see CLAUDE.md):")
        print("\n".join(f"  {line}" for line in sorted(bad)))
        return 1
    print("License check passed: no GPL/AGPL distributions installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
