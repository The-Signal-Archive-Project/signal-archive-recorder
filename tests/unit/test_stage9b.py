# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Stage 9b: resumable uploads, backoff, bandwidth cap, schedules and disk limits."""

import json
import time
from datetime import UTC, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder.config import ConfigError, parse_config
from signal_archive_recorder.core.bus import EventBus
from signal_archive_recorder.core.clock import FakeClock
from signal_archive_recorder.core.events import CaptureWarning, Event
from signal_archive_recorder.session.cleanup import cleanup, folder_size
from signal_archive_recorder.session.storage import SessionStorage
from signal_archive_recorder.upload.queue import (
    BACKOFF_MAX_S,
    UploadRecord,
    UploadState,
    plan_steps,
)
from signal_archive_recorder.upload.service import UploadService, Window, allowed_now
from tests.unit.test_upload import env, make_session, uploader  # noqa: F401

S = 1_000_000_000


# -- resumable uploads and backoff -------------------------------------------------


def test_steps_put_session_json_last(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"])
    steps = plan_steps(folder)
    first, second = json.loads((folder / "session.json").read_text())["chunks"]
    assert steps[0] == [f"recordings/{first}.flac", f"recordings/{first}.meta.json"]
    assert steps[1] == [f"recordings/{second}.flac", f"recordings/{second}.meta.json"]
    assert steps[-1][-1] == "session.json"  # its arrival means "complete"
    assert set(steps[-1]) == {
        "labels/wsjtx/chunk_stats.jsonl",
        "labels/wsjtx/decodes.jsonl",
        "session.json",
    }


def test_retry_backoff_and_resume(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"])
    up = uploader(env)
    hub = env["hub"]
    hub.fail_on_step = 2  # the PR opens with the first chunk, then the line drops

    record = up.upload(folder)
    assert record.state is UploadState.QUEUED and record.pr_num == 1  # the PR is kept
    assert record.failures == 1
    assert record.next_attempt_ns == env["clock"].now_ns() + 60 * S  # first backoff: 1 minute
    assert up.upload_all() == []  # automatic retries wait for the backoff

    env["clock"].advance(61 * S)
    sent_before = [f for step in hub.steps for f in step]
    [(_, record)] = up.upload_all()
    assert record.state is UploadState.PR_OPENED and record.failures == 0
    resumed = [f for step in hub.steps for f in step][len(sent_before) :]
    first_chunk = hub.steps[0]
    assert not set(first_chunk) & set(resumed)  # finished files weren't sent again
    assert len(hub.prs) == 1
    pr_files = {name.split("/", 3)[3] for name in hub.prs[0].files}
    assert pr_files == {f for step in plan_steps(folder) for f in step}  # complete
    assert sorted(record.sent_files) == sorted(pr_files)


def test_backoff_doubles_and_is_bounded(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"])
    up = uploader(env)
    delays = []
    for _ in range(12):
        env["hub"].fail_next_upload = TimeoutError("still offline")
        record = up.upload(folder)  # an explicit upload ignores the backoff
        delays.append((record.next_attempt_ns - env["clock"].now_ns()) / S)
    assert delays[:4] == [60, 120, 240, 480]
    assert max(delays) == BACKOFF_MAX_S == 6 * 3600


def test_resume_after_a_crash_mid_upload(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"])
    up = uploader(env)
    env["hub"].fail_on_step = 2
    up.upload(folder)
    record = UploadRecord.load(folder)
    record.state = UploadState.UPLOADING  # as if the process died during step 2
    record.pr_num = record.pr_url = None  # ...before it even saved the PR number
    record.save(folder, 0)
    assert up.upload(folder).state is UploadState.PR_OPENED
    assert len(env["hub"].prs) == 1  # found and continued, not duplicated


# -- bandwidth cap ------------------------------------------------------------------


def test_bandwidth_cap(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"], seconds=300)
    clock = env["clock"]
    link_bytes_per_s = 2_000_000  # the line itself is fast...
    env["hub"].on_step = lambda n: clock.advance(int(n / link_bytes_per_s * S))
    up = uploader(env)
    up.max_bytes_per_s = 100_000  # ...but the operator asks for 0.8 Mbit/s on average
    up.sleep = lambda seconds: clock.advance(int(seconds * S))
    started = clock.monotonic_ns()
    assert up.upload(folder).state is UploadState.PR_OPENED
    elapsed = (clock.monotonic_ns() - started) / S
    sent = sum(len(b) for b in env["hub"].prs[0].files.values())
    assert sent / elapsed <= 100_000 * 1.10
    assert sent / elapsed >= 100_000 * 0.90  # and not needlessly slower


def test_no_cap_means_no_waiting(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"])
    up = uploader(env)
    waits: list[float] = []
    up.sleep = waits.append
    up.upload(folder)
    assert waits == []


# -- disk limits ----------------------------------------------------------------------


def confirm(folder: Path, when_ns: int) -> None:
    record = UploadRecord(state=UploadState.VALIDATED)
    record.save(folder, when_ns)


def test_disk_limit(env: dict[str, Any]) -> None:  # noqa: F811
    root = env["root"]
    a = make_session(root, start="10:00:00")  # oldest
    b = make_session(root, start="11:00:00")
    c = make_session(root, start="12:00:00")
    d = make_session(root, start="13:00:00")  # never uploaded
    confirm(a, 1)
    confirm(c, 1)
    UploadRecord(state=UploadState.PR_OPENED).save(b, 1)  # uploaded, not yet confirmed
    storage = SessionStorage(root)
    size = folder_size(a)

    report = cleanup(storage, now_ns=10 * S, max_bytes=int(3.5 * size))
    assert report.deleted == [a.name]  # oldest confirmed first, and only as much as needed
    assert b.exists() and c.exists() and d.exists() and report.warning is None

    report = cleanup(storage, now_ns=10 * S, max_bytes=int(1.5 * size))
    assert report.deleted == [c.name]  # the only other confirmed session
    assert b.exists() and d.exists()  # unconfirmed sessions are never deleted
    assert report.warning and "nothing else is safe to delete" in report.warning


def test_delete_after_days_and_dry_run(env: dict[str, Any]) -> None:  # noqa: F811
    root = env["root"]
    old = make_session(root, start="10:00:00")
    new = make_session(root, start="11:00:00")
    confirm(old, 0)
    confirm(new, 25 * 86_400 * S)
    now = 31 * 86_400 * S
    storage = SessionStorage(root)
    planned = cleanup(storage, now_ns=now, delete_after_days=30, dry_run=True)
    assert planned.deleted == [old.name] and old.exists()  # dry run: nothing deleted
    done = cleanup(storage, now_ns=now, delete_after_days=30)
    assert done.deleted == [old.name] and not old.exists() and new.exists()


# -- schedules ---------------------------------------------------------------------


def at(hhmm: str) -> int:
    return int(datetime.fromisoformat(f"2026-10-06T{hhmm}:00").replace(tzinfo=UTC).timestamp()) * S


def test_windows() -> None:
    night = Window.parse("01:00-06:00")
    assert night.contains(clock_time(3, 0)) and not night.contains(clock_time(6, 0))
    late = Window.parse("22:00-06:00")  # wraps midnight
    assert late.contains(clock_time(23, 30)) and late.contains(clock_time(2, 0))
    assert not late.contains(clock_time(12, 0))
    with pytest.raises(ValueError):
        Window.parse("25:00-06:00")


def test_schedules_decide_when_to_upload() -> None:
    night = Window.parse("01:00-06:00")
    assert allowed_now("while_recording", night, at("12:00"), UTC)
    assert allowed_now("overnight", night, at("03:00"), UTC)
    assert not allowed_now("overnight", night, at("12:00"), UTC)
    assert not allowed_now("manual", night, at("03:00"), UTC)


def test_service_uploads_on_schedule_and_tidies(env: dict[str, Any]) -> None:  # noqa: F811
    folder = make_session(env["root"])
    up = uploader(env)
    bus = EventBus(FakeClock())
    events: list[Event] = []
    bus.subscribe(lambda s: events.append(s.event))
    env["clock"].set_wall_ns(at("12:00"))
    service = UploadService(
        up,
        bus,
        env["clock"],
        schedule="overnight",
        window=Window.parse("01:00-06:00"),
        tz=UTC,
        max_bytes=1,
    )
    service.run_once()
    assert env["hub"].prs == []  # midday: not yet
    env["clock"].set_wall_ns(at("02:00"))
    service.run_once()
    assert UploadRecord.load(folder).state is UploadState.PR_OPENED
    bus.close()
    warnings = [e for e in events if isinstance(e, CaptureWarning)]
    assert warnings and warnings[0].code == "disk_limit"  # unconfirmed: can't make room


def test_service_problems_are_reported_once(
    env: dict[str, Any],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    make_session(env["root"])
    up = uploader(env, login=False)
    service = UploadService(
        up,
        EventBus(FakeClock()),
        env["clock"],
        schedule="while_recording",
        window=Window.parse("01:00-06:00"),
    )
    for _ in range(3):
        service.run_once()
    assert caplog.text.count("background upload paused") == 1


def test_service_thread_stops_promptly(env: dict[str, Any]) -> None:  # noqa: F811
    up = uploader(env)
    service = UploadService(
        up,
        EventBus(FakeClock()),
        env["clock"],
        schedule="while_recording",
        window=Window.parse("01:00-06:00"),
        interval_s=0.05,
    )
    service.start()
    time.sleep(0.2)
    started = time.monotonic()
    service.stop()
    assert time.monotonic() - started < 2 and service.rounds >= 2


def test_upload_config() -> None:
    config = parse_config(
        {
            "audio": {"device": "x"},
            "storage": {"max_gb": 50},
            "upload": {"schedule": "overnight", "max_mbps": 8},
        }
    )
    assert config.upload.max_bytes_per_s == 1_000_000 and config.max_gb == 50
    for bad in ({"schedule": "hourly"}, {"overnight_window": "late"}, {"max_mbps": -1}):
        with pytest.raises(ConfigError):
            parse_config({"audio": {"device": "x"}, "upload": bad})
