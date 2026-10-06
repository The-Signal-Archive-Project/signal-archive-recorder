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
        self.fail_on_step: int | None = None  # fail the Nth upload step (1-based), once
        self.steps: list[list[str]] = []  # the files sent by each upload step, in order
        self.on_step: Any = None  # called with the bytes of each step (e.g. to pass time)

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

    def _send(self, token: Token, folder: Path, path_in_repo: str, files: Any) -> dict[str, bytes]:
        user = self._user(token)
        if user["auth"]["accessToken"]["role"] == "read":
            raise PermissionError("403 Forbidden: token can't write")
        # The same rule the real client applies: only allowed, non-ignored files go.
        chosen = list(filter_repo_objects(
            list(files), allow_patterns=list(ALLOW_PATTERNS), ignore_patterns=list(IGNORE_PATTERNS)
        ))  # fmt: skip
        self.steps.append(chosen)
        sent = {f"{path_in_repo}/{r}": (folder / r).read_bytes() for r in chosen}
        if self.on_step is not None:
            self.on_step(sum(len(b) for b in sent.values()))
        if self.fail_next_upload is not None:  # files sent, then the step fails
            error, self.fail_next_upload = self.fail_next_upload, None
            raise error
        if self.fail_on_step is not None and len(self.steps) == self.fail_on_step:
            self.fail_on_step = None
            raise ConnectionError(f"connection lost during upload step {len(self.steps)}")
        return sent

    def open_pr(self, token: Token, repo_id: str, folder: Path, path_in_repo: str, title: str,
                description: str, files: Any) -> PrRef:  # fmt: skip
        self.calls.append("open_pr")
        user = self._user(token)
        sent = self._send(token, folder, path_in_repo, files)
        pr = FakePr(len(self.prs) + 1, title, user["name"], sent, description)
        self.prs.append(pr)
        if self.create_pr_then_fail:
            self.create_pr_then_fail = False
            raise ConnectionError("connection reset after the PR was created")
        return PrRef(pr.num, f"https://hf.example/{repo_id}/discussions/{pr.num}")

    def add_to_pr(self, token: Token, repo_id: str, num: int, folder: Path, path_in_repo: str,
                  files: Any, message: str) -> None:  # fmt: skip
        self.calls.append("add_to_pr")
        pr = self.prs[num - 1]
        if pr.state != "open":
            raise PrNotFoundError(f"pull request #{num} isn't open")
        pr.files.update(self._send(token, folder, path_in_repo, files))

    def pr_files(self, token: Token, repo_id: str, num: int) -> set[str]:
        self.calls.append("pr_files")
        self._user(token)
        if num > len(self.prs) or self.prs[num - 1].state == "deleted":
            raise PrNotFoundError(f"pull request #{num} not found on {repo_id}")
        return set(self.prs[num - 1].files)

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
