# SPDX-License-Identifier: Apache-2.0
"""The few Hugging Face operations the uploader needs, behind a small interface.

`HfHub` uses huggingface_hub; tests use tests/fakes/fake_hf.py. Each session is
uploaded as one pull request to the intake dataset.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from signal_archive_recorder.upload.token import Token

DEFAULT_REPO = "signal-archive-project/signal-archive-intake"
# What a session upload may contain, relative to the session folder.
ALLOW_PATTERNS = ("session.json", "recordings/*.flac", "recordings/*.meta.json", "labels/*")
IGNORE_PATTERNS = ("local/*", "*.partial", "*.tmp", "*.corrupt", "*.crashed", "*.unrecoverable",
                   "*.invalid.json", "*.scan.flac")  # fmt: skip


class HubError(RuntimeError):
    pass


class InvalidTokenError(HubError):
    pass


class TokenPermissionError(HubError):
    pass


@dataclass(frozen=True)
class PrRef:
    num: int
    url: str


@dataclass(frozen=True)
class PrStatus:
    state: str  # open, merged, closed, draft
    comments: list[str] = field(default_factory=list)


class Hub(Protocol):
    def whoami(self, token: Token) -> dict[str, Any]: ...

    def open_pr(
        self,
        token: Token,
        repo_id: str,
        folder: Path,
        path_in_repo: str,
        title: str,
        description: str,
    ) -> PrRef: ...

    def find_open_pr(self, token: Token, repo_id: str, author: str, title: str) -> PrRef | None: ...

    def pr_status(self, token: Token, repo_id: str, num: int) -> PrStatus: ...


@dataclass(frozen=True)
class Identity:
    username: str
    role: str


def check_token(hub: Hub, token: Token, repo_id: str = DEFAULT_REPO) -> Identity:
    """Confirm the token works and can open pull requests on the intake repository."""
    try:
        info = hub.whoami(token)
    except InvalidTokenError:
        raise
    except Exception as exc:
        raise HubError(f"Couldn't reach Hugging Face to check the token: {exc}") from exc
    username = info.get("name")
    access = (info.get("auth") or {}).get("accessToken") or {}
    role = access.get("role", "unknown")
    if not username:
        raise InvalidTokenError("Hugging Face didn't recognise that token.")
    needed = (
        f'Uploading needs a token that can open pull requests on {repo_id}: either a "Write" '
        f"token, or a fine-grained token with write access to that repository. Create one at "
        f"https://huggingface.co/settings/tokens"
    )
    if role == "read":
        raise TokenPermissionError(f"This token is read-only. {needed}")
    if role == "fineGrained" and not _fine_grained_can_write(access.get("fineGrained"), repo_id):
        raise TokenPermissionError(f"This fine-grained token has no write access. {needed}")
    return Identity(username, role)


def _fine_grained_can_write(details: Any, repo_id: str) -> bool:
    """True if any scope covering the repo (or everything) grants a write permission.

    If the details can't be read, don't block: the first upload will say.
    """
    if not isinstance(details, dict):
        return True
    org = repo_id.split("/", 1)[0]
    scopes: list[Any] = list(details.get("scoped") or [])
    if details.get("global"):
        scopes.append({"entity": {"name": "*"}, "permissions": details["global"]})
    for scope in scopes:
        name = ((scope or {}).get("entity") or {}).get("name")
        permissions = (scope or {}).get("permissions") or []
        if name in ("*", repo_id, org) and any(
            "write" in p or "discussion" in p for p in permissions
        ):
            return True
    return False


class HfHub:
    """The real Hugging Face, through huggingface_hub."""

    def __init__(self) -> None:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import HfHubHTTPError

        self._api: Any = HfApi()
        self._http_error: Any = HfHubHTTPError

    def whoami(self, token: Token) -> dict[str, Any]:
        try:
            info: dict[str, Any] = self._api.whoami(token=token.reveal(), cache=False)
        except self._http_error as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 401:
                raise InvalidTokenError("Hugging Face didn't accept that token.") from None
            raise
        return info

    def open_pr(
        self,
        token: Token,
        repo_id: str,
        folder: Path,
        path_in_repo: str,
        title: str,
        description: str,
    ) -> PrRef:
        info = self._api.upload_folder(
            repo_id=repo_id,
            repo_type="dataset",
            folder_path=str(folder),
            path_in_repo=path_in_repo,
            allow_patterns=list(ALLOW_PATTERNS),
            ignore_patterns=list(IGNORE_PATTERNS),
            commit_message=title,
            commit_description=description,
            create_pr=True,
            token=token.reveal(),
        )
        if info.pr_num is None:
            raise HubError("Hugging Face accepted the files but didn't open a pull request")
        return PrRef(int(info.pr_num), str(info.pr_url))

    def find_open_pr(self, token: Token, repo_id: str, author: str, title: str) -> PrRef | None:
        for discussion in self._api.get_repo_discussions(
            repo_id=repo_id,
            repo_type="dataset",
            author=author,
            discussion_type="pull_request",
            discussion_status="open",
            token=token.reveal(),
        ):
            if discussion.title == title:
                url = f"https://huggingface.co/datasets/{repo_id}/discussions/{discussion.num}"
                return PrRef(int(discussion.num), url)
        return None

    def pr_status(self, token: Token, repo_id: str, num: int) -> PrStatus:
        details = self._api.get_discussion_details(
            repo_id=repo_id, discussion_num=num, repo_type="dataset", token=token.reveal()
        )
        comments = [e.content for e in details.events if getattr(e, "type", "") == "comment"]
        return PrStatus(str(details.status), comments)


def matches(relative: str, allow: Sequence[str] = ALLOW_PATTERNS) -> bool:
    """Whether a session-relative path would be uploaded (mirrors huggingface_hub)."""
    from fnmatch import fnmatch

    path = relative.replace("\\", "/")
    return any(fnmatch(path, p) for p in allow) and not any(
        fnmatch(path, p) for p in IGNORE_PATTERNS
    )
