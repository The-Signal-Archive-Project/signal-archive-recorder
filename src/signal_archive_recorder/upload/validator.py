# SPDX-License-Identifier: Apache-2.0
"""Reading the intake validator's verdict from a pull request comment.

The intake repository's validator posts a comment containing a fenced block:

    ```signal-archive-validator
    {"result": "pass", "validator_version": "1.0", "messages": []}
    ```

`result` is "pass" or "fail"; `messages` explains failures. The newest such
comment on the PR is the one that counts. See docs/validator-comment.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

_BLOCK = re.compile(r"```signal-archive-validator\s*\n(.*?)\n```", re.DOTALL)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    messages: tuple[str, ...]
    validator_version: str | None


def parse_comment(text: str) -> Verdict | None:
    match = _BLOCK.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("result") not in ("pass", "fail"):
        return None
    messages = data.get("messages") or []
    return Verdict(
        passed=data["result"] == "pass",
        messages=tuple(str(m) for m in messages if isinstance(m, str)),
        validator_version=data.get("validator_version"),
    )


def latest_verdict(comments: Iterable[str]) -> Verdict | None:
    verdict = None
    for comment in comments:  # oldest first
        verdict = parse_comment(comment) or verdict
    return verdict
