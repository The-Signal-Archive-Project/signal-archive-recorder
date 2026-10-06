# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""SignalArchiveRecorder.exe: the desktop app (windowed, no console).

With no arguments it starts the tray app (setup window first time). With arguments
it runs that command, so the uninstaller can call `forget` without a console.
"""

import sys

from signal_archive_recorder import cli

if __name__ == "__main__":
    sys.exit(cli.main(sys.argv[1:] or ["tray"]))
