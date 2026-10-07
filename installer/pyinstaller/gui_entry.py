# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""SignalArchiveRecorder(.exe): the desktop app (windowed on Windows).

No arguments, or only options, start the tray app (the setup window the first time).
A command runs as given, so the uninstaller can call `forget` without a console.
"""

import sys

from signal_archive_recorder.gui import main

if __name__ == "__main__":
    sys.exit(main())
