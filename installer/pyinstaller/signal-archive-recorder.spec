# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
# PyInstaller spec: one folder holding two programs that share every library.
#   SignalArchiveRecorder.exe    the desktop app (windowed)
#   signal-archive-recorder.exe  the command line (console)
# One folder, not one file: it starts faster, antivirus tools trust it more, and the
# LGPL libraries (Qt, libsndfile) stay separate, replaceable DLLs.
# Shared by the Windows installer (installer/windows/build.py, which also writes
# version_info.txt) and the Debian/Ubuntu package (installer/linux/build_deb.py).
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

HERE = SPECPATH  # noqa: F821 (set by PyInstaller)
WINDOWS = f"{HERE}/../windows"

datas = collect_data_files("signal_archive_recorder")  # modes, schemas, icon, example config
for dist in ("signal-archive-recorder", "huggingface_hub", "keyring", "jsonschema",
             "referencing", "numpy", "sounddevice", "soundfile", "packaging", "PySide6"):
    try:
        datas += copy_metadata(dist)  # importlib.metadata needs these at run time
    except Exception:
        pass
hiddenimports = (
    collect_submodules("signal_archive_recorder")  # the console exe may still open the tray
    + collect_submodules("keyring.backends")  # found through entry points, invisible to analysis
    + ["win32ctypes.core", "win32ctypes.pywin32.win32cred"]
)
excludes = ["tkinter", "unittest", "pydoc_data", "PySide6.QtQml", "PySide6.QtQuick",
            "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner"]


def analysis(script):
    return Analysis(  # noqa: F821
        [f"{HERE}/{script}"], datas=datas, hiddenimports=hiddenimports, excludes=excludes,
        noarchive=False,
    )


gui, console = analysis("gui_entry.py"), analysis("console_entry.py")
if sys.platform.startswith("linux"):
    # Use the system's C++ runtime, never the build machine's: it's backwards
    # compatible, and system libraries loaded later (PortAudio pulls in JACK) may
    # need a newer one than Ubuntu 22.04's. Debian 13 and Ubuntu 24.04 do.
    SYSTEM_ONLY = ("libstdc++.so", "libgcc_s.so")
    for a in (gui, console):
        a.binaries = [b for b in a.binaries if not b[0].startswith(SYSTEM_ONLY)]
common = dict(exclude_binaries=True, upx=False)
if sys.platform == "win32":  # the exe icon and version resource are Windows things
    common.update(icon=f"{WINDOWS}/icon.ico", version=f"{WINDOWS}/version_info.txt")
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], name="SignalArchiveRecorder",  # noqa: F821
              console=False, **common)
console_exe = EXE(PYZ(console.pure), console.scripts, [],  # noqa: F821
                  name="signal-archive-recorder", console=True, **common)
COLLECT(gui_exe, gui.binaries, gui.datas, console_exe, console.binaries, console.datas,  # noqa: F821
        upx=False, name="SignalArchiveRecorder")
