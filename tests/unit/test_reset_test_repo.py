# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "reset_test_repo", Path(__file__).resolve().parents[2] / "tools" / "reset_test_repo.py"
)
assert _spec and _spec.loader
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


@pytest.mark.parametrize(
    "repo",
    [
        "signal-archive-project/signal-archive-intake",  # production
        "signal-archive-project/signal-archive",
        "signal-archive-project/test-intake",  # "-test" must be the end of the name
        "someone/signal-archive-intake-test-old",
    ],
)
def test_refuses_anything_but_a_test_repo(repo: str) -> None:
    with pytest.raises(SystemExit, match="refusing"):
        tool.check_target(repo)


def test_accepts_test_repos() -> None:
    tool.check_target(tool.TEST_REPO)
    tool.check_target("someone/scratch-test")


def test_refuses_before_touching_hugging_face(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(tool, "TokenStore", lambda: called.append("token") or None)
    with pytest.raises(SystemExit):
        tool.main(["--repo", "signal-archive-project/signal-archive-intake"])
    assert called == []


def test_unconfirmed_reset_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class Api:
        deleted = False

        def get_repo_discussions(self, *a: object, **k: object) -> list[object]:
            return []

        def list_repo_files(self, *a: object, **k: object) -> list[str]:
            return ["README.md", "contributions/x/session.json"]

        def delete_repo(self, *a: object, **k: object) -> None:
            Api.deleted = True

    class Store:
        def get(self) -> object:
            from signal_archive_recorder.upload.token import Token

            return Token("hf_FAKEtokenValue1234567890")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(tool, "TokenStore", Store)
    monkeypatch.setattr("builtins.input", lambda _: "signal-archive-project/wrong-name-test")
    assert tool.main([]) == 1
    assert not Api.deleted
