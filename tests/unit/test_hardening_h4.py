# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""H4: the way testers report back (dialog, links, issue forms, guide)."""

import re
from pathlib import Path
from typing import Any

import pytest

from signal_archive_recorder import links
from signal_archive_recorder.updates import display_version

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"


def test_display_version() -> None:
    assert display_version("0.3.0b2") == "0.3.0-beta.2"
    assert display_version("0.3.0rc1") == "0.3.0-rc.1"
    assert display_version("1.0.0") == "1.0.0"


def test_links_point_at_real_forms_and_never_at_an_expiring_invite() -> None:
    for url in (links.NEW_TEST_REPORT, links.NEW_BUG_REPORT):
        template = url.rsplit("template=", 1)[1]
        assert (TEMPLATES / template).exists(), template
    shipped = " ".join(str(v) for k, v in vars(links).items() if k.isupper())
    assert "discord.gg" not in shipped  # invites expire; the app links to the README
    assert links.COMMUNITY.endswith("#community")
    assert "## Community" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_issue_forms_are_valid() -> None:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((TEMPLATES / "config.yml").read_text(encoding="utf-8"))
    assert config["blank_issues_enabled"] is False
    assert any("groups.io" in link["url"] for link in config["contact_links"])
    for name in ("test-report.yml", "bug-report.yml", "idea.yml"):
        form = yaml.safe_load((TEMPLATES / name).read_text(encoding="utf-8"))
        assert form["name"] and form["description"] and form["body"]
        ids = [item.get("id") for item in form["body"] if item["type"] != "markdown"]
        assert len(ids) == len(set(ids)), f"duplicate ids in {name}"
        for item in form["body"]:
            if item["type"] == "dropdown":
                assert item["attributes"]["options"], f"{name}: empty dropdown"


def test_guide_links_resolve() -> None:
    text = (ROOT / "TESTING.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\((?!https?://)([^)#]+)", text):
        assert (ROOT / target).exists(), target
    assert links.GROUP in text and "test-report.yml" in text


def test_report_dialog(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from signal_archive_recorder.ui import report

    opened: list[str] = []
    monkeypatch.setattr(
        report.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
    )
    saved: list[bool] = []
    dialog = report.ReportDialog(lambda: saved.append(True))
    qtbot.addWidget(dialog)
    dialog.save.click()
    assert saved == [True]
    for key in ("test", "bug", "group", "community"):
        dialog.buttons[key].click()
    assert opened == [links.NEW_TEST_REPORT, links.NEW_BUG_REPORT, links.GROUP, links.COMMUNITY]


def test_report_dialog_without_diagnostics(qtbot: Any) -> None:
    from signal_archive_recorder.ui.report import ReportDialog

    dialog = ReportDialog(None)
    qtbot.addWidget(dialog)
    assert not dialog.save.isEnabled()


def test_window_shows_the_version_and_opens_the_report(qtbot: Any, tmp_path: Path) -> None:
    from signal_archive_recorder.ui.app import StatusWindow
    from tests.unit.test_stage9c import FakeController

    window = StatusWindow(
        FakeController(), recordings=tmp_path, settings=tmp_path / "r.toml", diagnostics=lambda p: p
    )
    qtbot.addWidget(window)
    assert display_version() in window.version.text()
    window.report_button.click()
    assert window.report is not None and window.report.isVisible()
    assert window.report.save.isEnabled()


def test_discord_announcement_fits_in_one_message() -> None:
    text = (ROOT / "docs" / "announcement.md").read_text(encoding="utf-8")
    block = re.search(r"## Discord: beta announcement.*?```\n(.*?)```", text, re.S)
    assert block is not None
    assert len(block.group(1).rstrip("\n")) <= 2000  # Discord's limit per message
