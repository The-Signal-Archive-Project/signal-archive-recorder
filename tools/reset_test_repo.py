# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Wipe the TEST intake repository: delete it and recreate it with the same card.

    python tools/reset_test_repo.py
    python tools/reset_test_repo.py --repo signal-archive-project/other-thing-test

Deleting (rather than deleting files) is the only way to remove uploads from the
repository's git history and pull requests. It refuses the production intake
repository and any repository whose name doesn't end in "-test", and asks you to
type the repository's name before deleting anything. Uses the token stored by
`signal-archive-recorder login`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from signal_archive_recorder.upload.hub import PRODUCTION_REPO, TEST_REPO
from signal_archive_recorder.upload.token import TokenStore


def check_target(repo: str) -> None:
    """Raise unless the repository is clearly a test repository."""
    name = repo.split("/", 1)[-1]
    if repo == PRODUCTION_REPO or not name.endswith("-test"):
        raise SystemExit(f"refusing to wipe {repo}: only repositories named *-test can be reset")


def reset(api: Any, repo: str, token: str) -> list[str]:
    """Delete and recreate `repo`, keeping its README card. Returns the files afterwards."""
    from huggingface_hub import hf_hub_download

    card_path = hf_hub_download(repo, "README.md", repo_type="dataset", token=token)
    with open(card_path, "rb") as f:
        card = f.read()
    api.delete_repo(repo, repo_type="dataset", token=token)
    api.create_repo(repo, repo_type="dataset", private=False, token=token)
    api.upload_file(
        path_or_fileobj=card,
        path_in_repo="README.md",
        repo_id=repo,
        repo_type="dataset",
        commit_message="Reset test repository",
        token=token,
    )
    files: list[str] = api.list_repo_files(repo, repo_type="dataset", token=token)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=TEST_REPO)
    args = parser.parse_args(argv)
    check_target(args.repo)

    from huggingface_hub import HfApi

    token = TokenStore().get()
    if token is None:
        print("No token: run `signal-archive-recorder login` first.", file=sys.stderr)
        return 1
    api = HfApi()
    discussions = api.get_repo_discussions(args.repo, repo_type="dataset", token=token.reveal())
    prs = [d for d in discussions if d.is_pull_request]
    files = api.list_repo_files(args.repo, repo_type="dataset", token=token.reveal())
    print(f"{args.repo}: {len(files)} files, {len(prs)} pull requests. All will be deleted.")
    if input(f"Type the repository name to confirm ({args.repo}): ").strip() != args.repo:
        print("Not confirmed; nothing changed.")
        return 1
    after = reset(api, args.repo, token.reveal())
    print(f"Reset. {args.repo} now holds: {', '.join(after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
