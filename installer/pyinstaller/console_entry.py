# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""signal-archive-recorder.exe: the command line, exactly as `pipx` installs it."""

import sys

from signal_archive_recorder import cli

if __name__ == "__main__":
    sys.exit(cli.main())
