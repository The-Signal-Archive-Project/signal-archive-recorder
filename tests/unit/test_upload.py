# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 8: consent, the Hugging Face token, review, and one PR per session."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder import cli
from signal_archive_recorder.config import AudioConfig, RecorderConfig
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import FreqChanged, ModeChanged, SourceUp
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.metadata.settings import StationSettings
from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.sources.wsjtx import messages as m
from signal_archive_recorder.sources.wsjtx.listener import WsjtxListener
from signal_archive_recorder.upload.consent import CONSENT_VERSION, ConsentStore, NoConsentError
from signal_archive_recorder.upload.hub import TokenPermissionError, check_token
from signal_archive_recorder.upload.queue import Uploader, UploadRecord, UploadState
from signal_archive_recorder.upload.review import review
from signal_archive_recorder.upload.token import Token, TokenStore
from tests.fakes.fake_hf import FakeHub
from tests.fakes.session_rig import REGISTRY, Rig, utc_ns

TOKEN = "hf_FAKEtokenValue1234567890abcdef"
REPO = "signal-archive-project/signal-archive-intake"
SETTINGS = StationSettings(callsign="W9XYZ", grid="EN52wa")


class MemoryKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.store[(service, username)]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Config dir, fake hub, in-memory keyring and fake clock, for the library and the CLI."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("SIGNAL_ARCHIVE_CONFIG_DIR", str(config_dir))
    hub, keyring = FakeHub(), MemoryKeyring()
    clock = FakeClock(utc_ns("13:00:00"))
    monkeypatch.setattr(cli, "make_hub", lambda: hub)
    monkeypatch.setattr(cli, "make_token_store", lambda: TokenStore(keyring))
    monkeypatch.setattr(cli, "make_clock", lambda: clock)
    root = tmp_path / "archive"
    config_file = tmp_path / "recorder.toml"
    config_file.write_text(f'[storage]\nroot = {json.dumps(str(root))}\n[audio]\ndevice = "x"\n')
    return {"hub": hub, "keyring": keyring, "clock": clock, "root": root,
            "config": config_file, "config_dir": config_dir}  # fmt: skip


def make_session(root: Path, seconds: int = 150, start: str = "12:03:07") -> Path:
    """A finished session with two chunks and some WSJT-X labels."""
    rig = Rig(root, start=start, seconds=seconds, builder=MetadataBuilder(REGISTRY, SETTINGS))
    listener = WsjtxListener(
        rig.bus, REGISTRY, rig.clock, decode_log=rig.session.decode_log("wsjtx")
    )
    rig.publish(SourceUp(source="wsjtx", detail="WSJT-X 3.0.2"))
    rig.publish(FreqChanged(source="wsjtx", dial_hz=14_074_000))
    rig.publish(ModeChanged(source="wsjtx", mode_id="ft8", raw_mode="FT8"))
    for second in (30, 130):
        rig.pump_to(second)
        decode = m.Decode("WSJT-X", True, 1000, -9, 0.1, 900, "~", f"CQ K1ABC FN42 {second}",
                          False, False)  # fmt: skip
        listener.handle(m.encode(decode))
        rig.bus.wait_idle()
    rig.finish()
    return rig.session.path


def uploader(env: dict[str, Any], *, consent: bool = True, login: bool = True) -> Uploader:
    store = ConsentStore(env["config_dir"] / "consent.json")
    if consent:
        store.accept(env["clock"].now_ns())
    tokens = TokenStore(env["keyring"])
    if login:
        tokens.set(env["hub"].add_token(TOKEN))
    return Uploader(
        SessionStorage(env["root"]), env["hub"], tokens, store, env["clock"], repo_id=REPO
    )


# -- consent ------------------------------------------------------------------------


def test_no_upload_without_consent(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    up = uploader(env, consent=False)
    with pytest.raises(NoConsentError, match="consent"):
        up.upload(session)
    assert env["hub"].calls == [] and env["hub"].prs == []
    assert UploadRecord.load(session).state is UploadState.QUEUED


def test_changed_terms_need_new_consent(env: dict[str, Any]) -> None:
    store = ConsentStore(env["config_dir"] / "consent.json")
    store.accept(1)
    data = json.loads(store.path.read_text())
    data["consent_version"] = "0"  # agreed to an older text
    store.path.write_text(json.dumps(data))
    with pytest.raises(NoConsentError, match="changed"):
        store.require()
    assert CONSENT_VERSION == "1"


def test_consent_cli(env: dict[str, Any], capsys: pytest.CaptureFixture[str],
                     monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr("builtins.input", lambda _: "no thanks")
    assert cli.main(["consent"]) == cli.EXIT_CONSENT
    assert ConsentStore(env["config_dir"] / "consent.json").load() is None
    monkeypatch.setattr("builtins.input", lambda _: "I agree")
    assert cli.main(["consent"]) == 0
    assert "CC-BY-4.0" in capsys.readouterr().out
    assert ConsentStore(env["config_dir"] / "consent.json").require().consent_version == "1"


# -- token --------------------------------------------------------------------------


def test_token_masks_itself() -> None:
    token = Token(TOKEN)
    assert TOKEN not in repr(token) and TOKEN not in str(token)
    assert TOKEN not in f"{token!r} {token}"
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(token)
    with pytest.raises(ValueError, match="hf_"):
        Token("not-a-token")


def test_token_permission_check() -> None:
    hub = FakeHub()
    read = hub.add_token("hf_readonly000000000000", role="read")
    with pytest.raises(TokenPermissionError, match=r"read-only.*Write") as err:
        check_token(hub, read, REPO)
    assert REPO in str(err.value) and "settings/tokens" in str(err.value)

    no_write = hub.add_token("hf_finegrained0000000001", role="fineGrained", fine_grained={
        "scoped": [{"entity": {"type": "org", "name": "signal-archive-project"},
                    "permissions": ["repo.content.read"]}]})  # fmt: skip
    with pytest.raises(TokenPermissionError, match="no write access"):
        check_token(hub, no_write, REPO)
    ok = hub.add_token("hf_finegrained0000000002", role="fineGrained", fine_grained={
        "scoped": [{"entity": {"type": "dataset", "name": REPO},
                    "permissions": ["repo.content.read", "repo.write"]}]})  # fmt: skip
    assert check_token(hub, ok, REPO).username == "volunteer"


def test_token_never_on_disk(env: dict[str, Any], capsys: pytest.CaptureFixture[str],
                             caplog: pytest.LogCaptureFixture,
                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    caplog.set_level(logging.DEBUG)
    env["hub"].add_token(TOKEN)
    monkeypatch.setattr("getpass.getpass", lambda _: TOKEN)
    assert cli.main(["login", "--config", str(env["config"])]) == 0
    assert env["keyring"].store  # in the keyring...
    make_session(env["root"])
    assert cli.main(["consent", "--yes"]) == 0
    env["hub"].fail_next_upload = RuntimeError(f"upstream said no to token {TOKEN}")
    assert cli.main(["upload", "--config", str(env["config"])]) == cli.EXIT_UPLOAD
    assert cli.main(["upload", "--config", str(env["config"])]) == 0
    assert cli.main(["status", "--config", str(env["config"])]) == 0

    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert TOKEN not in caplog.text
    for folder in (env["config_dir"], env["root"]):  # ...and nowhere else
        for path in folder.rglob("*"):
            if path.is_file():
                assert TOKEN.encode() not in path.read_bytes(), path
    config = RecorderConfig(audio=AudioConfig(device="x"))
    assert TOKEN not in repr(config) + repr(UploadRecord.load(next(env["root"].glob("sessions/*"))))


def test_login_rejects_read_only_token(env: dict[str, Any], capsys: pytest.CaptureFixture[str],
                                       monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    env["hub"].add_token("hf_readonly000000000000", role="read")
    monkeypatch.setattr("getpass.getpass", lambda _: "hf_readonly000000000000")
    assert cli.main(["login", "--config", str(env["config"])]) == cli.EXIT_LOGIN
    assert "read-only" in capsys.readouterr().err
    assert not env["keyring"].store


# -- uploading ----------------------------------------------------------------------


def test_one_pr_per_session(env: dict[str, Any]) -> None:
    first = make_session(env["root"])
    second = make_session(env["root"], seconds=30, start="14:00:00")
    up = uploader(env)
    results = dict(up.upload_all())
    assert {r.state for r in results.values()} == {UploadState.PR_OPENED}
    assert len(env["hub"].prs) == 2
    assert up.upload(first).pr_num == 1  # again: no second PR
    assert up.upload_all() == []
    assert len(env["hub"].prs) == 2

    pr = env["hub"].prs[0]
    prefix = f"contributions/volunteer/{first.name}/"
    sent = sorted(name.removeprefix(prefix) for name in pr.files)
    assert sent == sorted(
        ["session.json", "labels/wsjtx/chunk_stats.jsonl", "labels/wsjtx/decodes.jsonl"]
        + [p.relative_to(first).as_posix() for p in (first / "recordings").iterdir()]
    )
    assert not any("local/" in name for name in pr.files)  # upload.json etc stay home
    assert pr.title == f"Add session {prefix.rstrip('/')}"
    assert "ft8" in pr.description and "20m" in pr.description
    assert second.name in env["hub"].prs[1].title


def test_local_kept_until_pr_confirmed(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    before = sorted(p.relative_to(session) for p in session.rglob("*") if p.is_file())
    up = uploader(env)
    env["hub"].fail_next_upload = TimeoutError("connection lost after the files were sent")
    record = up.upload(session)
    assert record.state is UploadState.QUEUED  # retryable
    assert record.attempts == 1 and "TimeoutError" in (record.last_error or "")
    assert env["hub"].prs == []
    after = sorted(p.relative_to(session) for p in session.rglob("*") if p.is_file())
    assert set(before) <= set(after)  # nothing deleted (only local/upload.json added)
    record = up.upload(session)
    assert record.state is UploadState.PR_OPENED and record.attempts == 2
    assert record.last_error is None


def test_crash_after_pr_created_is_not_duplicated(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    up = uploader(env)
    env["hub"].create_pr_then_fail = True
    assert up.upload(session).state is UploadState.QUEUED
    assert len(env["hub"].prs) == 1  # the PR exists, but we never heard back
    record = up.upload(session)
    assert record.state is UploadState.PR_OPENED and record.pr_num == 1
    assert len(env["hub"].prs) == 1  # adopted, not duplicated


def test_interrupted_upload_state_is_retried(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    record = UploadRecord(state=UploadState.UPLOADING, attempts=1)
    record.save(session, 0)
    assert UploadRecord.load(session).state is UploadState.QUEUED


def test_deleted_chunk_not_uploaded(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    builder = MetadataBuilder(REGISTRY, SETTINGS)
    first, second = json.loads((session / "session.json").read_text())["chunks"]
    up = uploader(env)
    up.remove_chunk(session, second, builder)
    assert json.loads((session / "session.json").read_text())["chunks"] == [first]
    assert (session / "local" / "removed" / f"{second}.flac").exists()
    stats = (session / "labels" / "wsjtx" / "chunk_stats.jsonl").read_text()
    assert second not in stats and first in stats
    decodes = (session / "labels" / "wsjtx" / "decodes.jsonl").read_text()
    assert "FN42 30" in decodes and "FN42 130" not in decodes  # 130 s was in the removed chunk

    record = up.upload(session)
    assert record.state is UploadState.PR_OPENED, record.problems
    sent = " ".join(env["hub"].prs[0].files)
    assert second not in sent and first in sent
    with pytest.raises(RuntimeError, match="already uploaded"):
        up.remove_chunk(session, first, builder)


def test_preflight_blocks_bad_sessions(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    flac = next((session / "recordings").glob("*.flac"))
    data = bytearray(flac.read_bytes())
    data[len(data) // 2] ^= 0x10
    flac.write_bytes(bytes(data))
    up = uploader(env)
    record = up.upload(session)
    assert record.state is UploadState.BLOCKED
    assert any("checksum" in p for p in record.problems)
    assert env["hub"].prs == []


def test_unfinished_session_waits(env: dict[str, Any]) -> None:
    session = make_session(env["root"])
    meta = json.loads((session / "session.json").read_text())
    meta.update(ended_ns=None, ended_utc=None, end_reason=None)
    (session / "session.json").write_text(json.dumps(meta))
    up = uploader(env)
    assert up.upload_all() == []  # still recording: not queued for upload
    assert up.upload(session).state is UploadState.BLOCKED


# -- validator ----------------------------------------------------------------------


def verdict(result: str, *messages: str) -> str:
    body = json.dumps({"result": result, "validator_version": "1.0", "messages": list(messages)})
    return f"Automated check:\n\n```signal-archive-validator\n{body}\n```\n"


def test_validator_status_polled(env: dict[str, Any]) -> None:
    good = make_session(env["root"])
    bad = make_session(env["root"], seconds=30, start="15:00:00")
    up = uploader(env)
    up.upload_all()
    assert {r.state for _, r in up.poll_all()} == {UploadState.PR_OPENED}  # nothing yet

    env["hub"].prs[0].comments += ["Thanks!", verdict("pass")]
    env["hub"].prs[1].comments += [verdict("pass"), verdict("fail", "chunk 0000: clipped")]
    results = {d.name: r for d, r in up.poll_all()}
    assert results[good.name].state is UploadState.VALIDATED
    assert results[bad.name].state is UploadState.FAILED
    assert results[bad.name].validator_messages == ["chunk 0000: clipped"]  # newest verdict wins


def test_merged_or_closed_without_verdict(env: dict[str, Any]) -> None:
    a = make_session(env["root"])
    b = make_session(env["root"], seconds=30, start="15:00:00")
    up = uploader(env)
    up.upload_all()
    env["hub"].prs[0].state, env["hub"].prs[1].state = "merged", "closed"
    assert up.poll(a).state is UploadState.VALIDATED
    assert up.poll(b).state is UploadState.FAILED


# -- review -------------------------------------------------------------------------


def test_review_shows_what_is_shared(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    session = make_session(env["root"])
    r = review(session)
    assert r.finished and len(r.chunks) == 2
    assert r.callsign is None and r.grid == "EN52"  # callsign not shared; grid at 4 characters
    assert {c.band for c in r.chunks} == {"20m"} and {c.mode for c in r.chunks} == {"ft8"}
    assert all(not name.startswith("local/") for name, _ in r.files)
    assert cli.main(["review", session.name, "--config", str(env["config"])]) == 0
    out = capsys.readouterr().out
    assert "callsign: not shared" in out and "grid: EN52" in out and "20m" in out


# -- robustness: deleted PRs, offline checks, dry runs ----------------------------


def test_deleted_pr_is_reported_not_a_crash(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    session = make_session(env["root"])
    up = uploader(env)
    up.upload(session)
    env["hub"].prs[0].state = "deleted"  # e.g. the repository was wiped and recreated
    assert cli.main(["status", "--config", str(env["config"])]) == 0
    out = capsys.readouterr().out
    assert "pr_missing" in out and f"requeue {session.name}" in out
    assert UploadRecord.load(session).state is UploadState.PR_MISSING

    assert cli.main(["requeue", session.name, "--config", str(env["config"])]) == 0
    record = UploadRecord.load(session)
    assert record.state is UploadState.QUEUED and record.pr_num is None
    assert record.validator_messages == []
    assert up.upload(session).pr_num == 2  # a fresh PR


def test_offline_status_check_keeps_the_pr(env: dict[str, Any],
                                           capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    session = make_session(env["root"])
    up = uploader(env)
    up.upload(session)
    env["hub"].offline = True
    assert cli.main(["status", "--config", str(env["config"])]) == 0
    record = UploadRecord.load(session)
    assert record.state is UploadState.PR_OPENED  # still open; try again later
    assert "ConnectionError" in (record.last_error or "")
    assert "last attempt" in capsys.readouterr().out
    env["hub"].offline = False
    assert up.poll(session).last_error is None


def test_one_bad_session_does_not_stop_the_others(env: dict[str, Any]) -> None:
    a = make_session(env["root"])
    b = make_session(env["root"], seconds=30, start="15:00:00")
    up = uploader(env)
    up.upload_all()
    env["hub"].prs[0].state = "deleted"
    env["hub"].prs[1].comments.append(
        '```signal-archive-validator\n{"result": "pass", "messages": []}\n```'
    )
    states = {d.name: r.state for d, r in up.poll_all()}
    assert states == {a.name: UploadState.PR_MISSING, b.name: UploadState.VALIDATED}


def test_dry_run_sends_nothing(env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    session = make_session(env["root"])
    uploader(env)  # consent and login
    assert cli.main(["upload", "--dry-run", "--config", str(env["config"])]) == 0
    out = capsys.readouterr().out
    assert f"contributions/volunteer/{session.name}/" in out
    assert "recordings/" in out and "session.json" in out and "local/" not in out
    assert "Dry run: nothing was sent." in out
    assert env["hub"].prs == [] and "open_pr" not in env["hub"].calls
    assert not (session / "local" / "upload.json").exists()  # no state changed


def test_dry_run_shows_what_would_block(env: dict[str, Any],
                                        capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    session = make_session(env["root"])
    flac = next((session / "recordings").glob("*.flac"))
    data = bytearray(flac.read_bytes())
    data[len(data) // 2] ^= 0x10
    flac.write_bytes(bytes(data))
    uploader(env)
    assert cli.main(["upload", "--dry-run", session.name, "--config", str(env["config"])]) == 6
    assert "would be blocked" in capsys.readouterr().out


def test_test_repository_is_labelled(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        env["config"].read_text()
        + '[upload]\nrepo = "signal-archive-project/signal-archive-intake-test"\n'
    )
    env["config"].write_text(text)
    uploader(env)
    cli.main(["upload", "--dry-run", "--config", str(env["config"])])
    assert (
        "TEST repository signal-archive-project/signal-archive-intake-test"
        in capsys.readouterr().out
    )
