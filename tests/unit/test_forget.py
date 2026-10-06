# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""`forget`: what uninstalling removes, and never more."""

import json
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder import cli
from signal_archive_recorder.forget import apply, describe, plan
from signal_archive_recorder.ui.autostart import Autostart
from signal_archive_recorder.upload.token import Token, TokenStore
from tests.unit.test_firstrun import TOKEN, MemoryKeyring


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An installed-looking setup: settings, logs, an archive with other files beside it."""
    config_dir, logs, root = tmp_path / "config", tmp_path / "logs", tmp_path / "archive"
    monkeypatch.setenv("SIGNAL_ARCHIVE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("SIGNAL_ARCHIVE_LOG_DIR", str(logs))
    config_dir.mkdir()
    (config_dir / "recorder.toml").write_text(
        f'[storage]\nroot = {json.dumps(str(root))}\n[audio]\ndevice = "x"\n'
    )
    (config_dir / "consent.json").write_text("{}")
    logs.mkdir()
    (logs / "recorder.log").write_text("log\n")
    for name, state in (
        ("20261005T120000Z", "validated"),
        ("20261005T130000Z", "queued"),
        ("20261005T140000Z", None),
    ):
        folder = root / "sessions" / name
        (folder / "recordings").mkdir(parents=True)
        (folder / "recordings" / "0001.flac").write_bytes(b"x" * 1000)
        if state:
            (folder / "local").mkdir()
            (folder / "local" / "upload.json").write_text(json.dumps({"state": state}))
    keyring = MemoryKeyring()
    TokenStore(keyring).set(Token(TOKEN))
    monkeypatch.setattr(cli, "make_token_store", lambda: TokenStore(keyring))
    autostart = Autostart("Linux", tmp_path / "autostart")
    autostart.enable(["sar", "tray"])
    monkeypatch.setattr("signal_archive_recorder.ui.autostart.Autostart", lambda: autostart)
    return {
        "config": config_dir,
        "logs": logs,
        "root": root,
        "keyring": keyring,
        "autostart": autostart,
        "tmp": tmp_path,
    }


def test_keep_everything_only_stops_start_at_login(home: dict[str, Any]) -> None:
    assert cli.main(["forget", "--yes"]) == 0
    assert not home["autostart"].enabled()
    assert home["keyring"].store and (home["config"] / "recorder.toml").exists()
    assert len(list((home["root"] / "sessions").iterdir())) == 3


def test_token_only(home: dict[str, Any]) -> None:
    assert cli.main(["forget", "--token", "--yes"]) == 0
    assert home["keyring"].store == {}
    assert (home["config"] / "recorder.toml").exists() and (home["root"] / "sessions").exists()


def test_everything(home: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    (home["root"] / "my-notes.txt").write_text("not ours")  # someone else's file
    assert cli.main(["forget", "--everything", "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "PERMANENTLY DELETE 3 recorded session(s)" in text
    assert "2 of them are not uploaded" in text  # queued, and never tried
    assert (home["root"] / "sessions").exists()  # a dry run changes nothing

    assert cli.main(["forget", "--everything", "--yes"]) == 0
    assert not (home["root"] / "sessions").exists()
    assert (home["root"] / "my-notes.txt").read_text() == "not ours"  # never touched
    assert not home["config"].exists() and not home["logs"].exists()
    assert home["keyring"].store == {}  # everything implies the token


def test_everything_removes_an_empty_archive_folder(home: dict[str, Any]) -> None:
    assert cli.main(["forget", "--everything", "--yes"]) == 0
    assert not home["root"].exists()


def test_needs_confirmation_without_a_terminal(home: dict[str, Any]) -> None:
    assert cli.main(["forget", "--everything"]) == cli.EXIT_CONFIG
    assert (home["root"] / "sessions").exists() and home["keyring"].store


def test_a_custom_config_folder_is_never_deleted(home: dict[str, Any], tmp_path: Path) -> None:
    project = tmp_path / "my-project"
    project.mkdir()
    custom = project / "dev.toml"
    custom.write_text((home["config"] / "recorder.toml").read_text())
    (project / "important.py").write_text("keep me")
    assert cli.main(["forget", "--everything", "--yes", "--config", str(custom)]) == 0
    assert (project / "important.py").exists()  # only the app's own settings folder goes


def test_never_deletes_home_or_a_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from signal_archive_recorder.forget import _safe_to_delete

    assert not _safe_to_delete(Path.home())
    assert not _safe_to_delete(Path(Path.home().anchor))
    assert _safe_to_delete(tmp_path / "x")


def test_describe_keep(home: dict[str, Any]) -> None:
    p = plan(None, token=False, everything=False)
    assert "are kept" in describe(p) and "token" not in describe(p)
    apply(p, tokens=TokenStore(home["keyring"]), autostart=home["autostart"])
    assert home["keyring"].store
