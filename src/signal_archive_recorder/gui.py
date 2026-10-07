# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""The desktop app's entry point (`signal-archive-recorder-gui`, and the packaged app).

With no arguments, or only options (`--config FILE`), it is `signal-archive-recorder
tray`. A command (`forget --yes`, `--version`) runs as given, so the Windows
uninstaller can call the windowed program without a console appearing.
"""

from __future__ import annotations

import sys

from signal_archive_recorder import cli


def desktop_args(argv: list[str]) -> list[str]:
    if argv and (argv[0] in cli.command_names() or argv[0] in ("--version", "-h", "--help")):
        return argv
    return ["tray", *argv]


def main() -> int:
    return cli.main(desktop_args(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
