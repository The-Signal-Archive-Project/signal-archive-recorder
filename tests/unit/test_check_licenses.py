# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_licenses", Path(__file__).resolve().parents[2] / "tools" / "check_licenses.py"
)
assert _spec and _spec.loader
check_licenses = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_licenses)


@pytest.mark.parametrize(
    "text",
    [
        "GPL-3.0-only",
        "GPL-2.0-or-later",
        "AGPL-3.0",
        "GPLv3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "MIT OR GPL-3.0",
    ],
)
def test_forbidden(text: str) -> None:
    assert check_licenses.is_forbidden(text)


@pytest.mark.parametrize(
    "text",
    [
        "Apache-2.0",
        "MPL-2.0",
        "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)",
        "MIT",
        "BSD-3-Clause",
        "LGPL-3.0-or-later",
        "LGPLv3",
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "License :: OSI Approved :: Apache Software License",
    ],
)
def test_allowed(text: str) -> None:
    assert not check_licenses.is_forbidden(text)


def test_current_environment_passes() -> None:
    assert check_licenses.main() == 0
