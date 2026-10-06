# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""A fake Hugging Face Hub: tokens, PRs, comments, and injectable failures.

File selection uses huggingface_hub's own pattern filter, so what this fake
"receives" is exactly what the real service would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from huggingface_hub.utils import filter_repo_objects

from signal_archive_recorder.upload.hub import (
    ALLOW_PATTERNS,
    IGNORE_PATTERNS,
    InvalidTokenError,
    PrNotFoundError,
    PrRef,
    PrStatus,
)
from signal_archive_recorder.upload.token import Token


@dataclass
class FakePr:
    num: int
    title: str
    author: str
    files: dict[str, bytes]
    description: str
    state: str = "open"
    comments: list[str] = field(default_factory=list)


class FakeHub:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.prs: list[FakePr] = []
        self.calls: list[str] = []
        self.fail_next_upload: Exception | None = None
        self.create_pr_then_fail = False  # PR made, then the connection drops
        self.offline = False  # every status check fails, like a dropped connection

    def add_token(self, value: str, user: str = "volunteer", role: str = "write",
                  fine_grained: Any = None) -> Token:  # fmt: skip
        access: dict[str, Any] = {"role": role}
        if fine_grained is not None:
            access["fineGrained"] = fine_grained
        self.users[value] = {"name": user, "auth": {"type": "access_token", "accessToken": access}}
        return Token(value)

    def _user(self, token: Token) -> dict[str, Any]:
        info = self.users.get(token.reveal())
        if info is None:
            raise InvalidTokenError("Hugging Face didn't accept that token.")
        return info

    def whoami(self, token: Token) -> dict[str, Any]:
        self.calls.append("whoami")
        return self._user(token)

    def open_pr(self, token: Token, repo_id: str, folder: Path, path_in_repo: str, title: str,
                description: str) -> PrRef:  # fmt: skip
        self.calls.append("open_pr")
        user = self._user(token)
        if user["auth"]["accessToken"]["role"] == "read":
            raise PermissionError("403 Forbidden: token can't write")
        relative = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()]
        chosen = filter_repo_objects(
            relative, allow_patterns=list(ALLOW_PATTERNS), ignore_patterns=list(IGNORE_PATTERNS)
        )
        files = {f"{path_in_repo}/{r}": (folder / r).read_bytes() for r in chosen}
        if self.fail_next_upload is not None:  # files sent, then failure before any PR
            error, self.fail_next_upload = self.fail_next_upload, None
            raise error
        pr = FakePr(len(self.prs) + 1, title, user["name"], files, description)
        self.prs.append(pr)
        if self.create_pr_then_fail:
            self.create_pr_then_fail = False
            raise ConnectionError("connection reset after the PR was created")
        return PrRef(pr.num, f"https://hf.example/{repo_id}/discussions/{pr.num}")

    def find_open_pr(self, token: Token, repo_id: str, author: str, title: str) -> PrRef | None:
        self.calls.append("find_open_pr")
        for pr in self.prs:
            if pr.state == "open" and pr.author == author and pr.title == title:
                return PrRef(pr.num, f"https://hf.example/{repo_id}/discussions/{pr.num}")
        return None

    def pr_status(self, token: Token, repo_id: str, num: int) -> PrStatus:
        self.calls.append("pr_status")
        self._user(token)
        if self.offline:
            raise ConnectionError("network is unreachable")
        if num > len(self.prs) or self.prs[num - 1].state == "deleted":
            raise PrNotFoundError(f"pull request #{num} not found on {repo_id}")
        pr = self.prs[num - 1]
        return PrStatus(pr.state, list(pr.comments))
