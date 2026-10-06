# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The desktop app's entry point (`signal-archive-recorder-gui`, and the packaged app).

It is `signal-archive-recorder tray` with any extra arguments passed on. On Windows
it is installed as a windowed program, so no console window opens.
"""

from __future__ import annotations

import sys

from signal_archive_recorder import cli


def main() -> int:
    return cli.main(["tray", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
