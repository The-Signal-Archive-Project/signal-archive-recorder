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
    """True for GPL/AGPL declarations; LGPL and permissive licenses pass.

    An SPDX choice ("LGPL-3.0-only OR GPL-3.0-only") passes if any alternative
    does: we use the package under that option. A combination ("X AND Y") needs
    every part to pass.
    """
    return elected(license_text) is None


_SPDX_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+\-]+")
_SPDX_LIKE = re.compile(r"^[A-Za-z0-9.+\-() ]+$")


def elected(license_text: str) -> str | None:
    """The license we use a package under, or None if every possible reading is forbidden.

    SPDX expressions are parsed properly: AND binds tighter than OR, parentheses
    group, and "X WITH exception" counts as X. Anything else (free text, trove
    classifiers) is judged as a whole.
    """
    text = license_text.strip()
    if not _SPDX_LIKE.match(text) or not re.search(r"\b(AND|OR)\b", text):
        return None if _FORBIDDEN.search(text) else text
    tokens: list[str] = _SPDX_TOKEN.findall(text)
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def expression() -> str | None:  # term (OR term)*: the first acceptable option
        options = [term()]
        while peek() == "OR":
            take()
            options.append(term())
        return next((o for o in options if o is not None), None)

    def term() -> str | None:  # factor (AND factor)*: every part must be acceptable
        parts = [factor()]
        while peek() == "AND":
            take()
            parts.append(factor())
        return None if any(p is None for p in parts) else " AND ".join(p for p in parts if p)

    def factor() -> str | None:
        if peek() == "(":
            take()
            inner = expression()
            if peek() == ")":
                take()
            return inner
        ident = take()
        if peek() == "WITH":
            take()
            take()
        return None if _FORBIDDEN.search(ident) else ident

    try:
        return expression()
    except IndexError:  # malformed: judge the text as a whole
        return None if _FORBIDDEN.search(text) else text


def main() -> int:
    bad: list[str] = []
    choices: list[str] = []
    for dist in distributions():
        declared = license_strings(dist)
        hits = [s for s in declared if is_forbidden(s)]
        if hits:
            bad.append(f"{dist.metadata['Name']} {dist.version}: {'; '.join(hits)}")
        for s in declared:
            option = elected(s)
            if " OR " in s and option is not None:
                choices.append(f"{dist.metadata['Name']} {dist.version}: used under {option}")
    if bad:
        print("GPL/AGPL-licensed distributions found (not allowed, see CLAUDE.md):")
        print("\n".join(f"  {line}" for line in sorted(bad)))
        return 1
    for line in sorted(choices):
        print(f"  {line}  (a choice of licenses; see NOTICE)")
    print("License check passed: no GPL/AGPL distributions installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
