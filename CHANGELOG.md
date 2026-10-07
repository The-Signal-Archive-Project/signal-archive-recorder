# Changelog

All notable changes to Signal Archive Recorder. Versions follow [semantic versioning](https://semver.org/); before 1.0, minor versions may change behaviour.

## [Unreleased]

### Project
- **`tools/release.py`** cuts releases by SemVer: `next`, `prepare {major|minor|patch|pre|final} [--pre beta|rc]` (version, changelog, README pins, a signed-off commit and a PR) and `publish` (tag, build, GitHub release). See CLAUDE.md, "Versions".

### Windows installer
- **`SignalArchiveRecorder-<version>-Setup.exe`**: a per-user install (no administrator rights) with a Start-menu entry, optional desktop shortcut and start at login. It includes the desktop app and the command line, and asks you to close a running recorder before upgrading.
- **Uninstall** keeps recordings, settings and the Hugging Face login by default. It can remove the token, or everything (behind a warning and a second confirmation).
- Built and smoke-tested on Windows in CI for every change: it installs, checks the packaged app (keyring, audio libraries, Qt), records a session driven by a fake WSJT-X, starts the desktop app twice, and uninstalls. Attached to each GitHub release.

### Other
- **`signal-archive-recorder forget`**: what uninstalling removes (start at login; `--token`; `--everything`, which deletes recordings, settings and logs and asks for confirmation). Only the archive's `sessions` folder and the app's own folders are ever deleted.
- The app has an icon. Diagnostics now say which keyring backend is in use.

## [0.2.0] - 2026-10-06

### Uploads
- **Resumable uploads:** a session goes up in steps within its one pull request (each chunk, then labels and `session.json`). If the connection drops, the next attempt sends only what's missing.
- **Backoff:** automatic retries wait longer after each failure (1 minute, doubling, up to 6 hours).
- **Bandwidth cap:** `[upload] max_mbps` keeps the average upload speed under a limit.
- **Background uploading** while recording, using `[upload] schedule = "while_recording"` or `"overnight"` (within `overnight_window`).
- **Disk limits:** `[storage] max_gb`, `delete_after_days` and `cleanup [--dry-run]`. Only sessions whose upload has been confirmed (validated or merged) are ever deleted.

### Status window and tray
- **`signal-archive-recorder tray`:** records with a status window and a tray icon (green, yellow, red or grey), a checklist (recording, audio, WSJT-X, clock, disk, uploads) and a live audio level meter. Needs the optional `gui` extra (`pip install "signal-archive-recorder[gui]"`).
- **"Mark this" notes:** a note typed or picked in the window is saved as a `Note` event at that moment of the recording, in the chunk's metadata.
- **Pause and resume:** pausing ends the session cleanly (`end_reason: "paused"`); resuming starts a new one.
- **Upload now** from the window.

### Recording
- **Records only while WSJT-X is running.** Otherwise the recorder waits, ready, with the sound card closed. A session starts when WSJT-X appears and is saved when it closes or goes quiet for 30 s (`end_reason: "decoder_closed"`). Background uploads carry on in between. `[recording] start = "always"` records from start to stop as before (needed for WAV-file playback).
- If the sound card can't be opened (unplugged, say), the recorder says why and tries again every 30 s.
- A session starts once WSJT-X's frequency and mode have been steady for 2 s, so WSJT-X's own start-up (which briefly reports several frequencies, including 0 Hz) no longer leaves tiny chunks. A 0 Hz dial is ignored.

### Stereo inputs
- Setup's level test checks both channels of a stereo input. If one is a copy of the other, or silent, only the useful channel is recorded (`[audio] keep_channel`), halving the size with nothing lost. It's picked out of the stereo stream byte for byte. Channels that differ (a second receiver, I/Q) are both kept, and the tray warns if a dropped channel starts carrying different audio. `session.json` records the choice (`audio.channel_selection`).

### Updates
- **New-version notice:** a daily check of GitHub's releases, shown in the window banner, as a tray message and in the terminal. It never installs anything. Beta users hear about newer betas; others only about releases. `[updates] check = false` turns it off; `signal-archive-recorder check-update` checks by hand.

### Desktop app
- The tray icon is **blue** while ready and waiting for WSJT-X. Pause now also holds off recording until resumed.
- **Setup window** on first start: terms, Hugging Face login with the permission check, audio input with a level test, station, WSJT-X and clock checks. Nothing is saved until Finish.
- **Review & upload** window: what each session would share, the radio-audio checks, and upload buttons.
- **Start when I log in** (Windows and Linux), and only one copy runs at a time: starting it again shows the running one.
- **`signal-archive-recorder-gui`**, a windowed entry point (no console on Windows).
- Ctrl-C, logging out and `kill` now end the app with the session saved.

### Testing support
- A **log file**, always on and rotated (Linux: `~/.local/state/signal-archive-recorder/logs`, Windows: `%LOCALAPPDATA%\signal-archive-recorder\logs`), with tokens masked.
- **Diagnostics** zip for bug reports (`Save diagnostics...` or `signal-archive-recorder diagnostics`): versions, audio inputs, settings, session states and logs, with names redacted and no audio or token.

### Project
- The dependency license check understands SPDX expressions, so a dependency offered under a choice of licenses (such as Qt for Python's LGPL-3.0 or GPL) passes when one acceptable option exists.

## [0.1.0] - 2026-10-06

The first release. It records bit-exact receive audio from a ham station, labels it with context from WSJT-X, and contributes it to the Signal Archive Project's open dataset on Hugging Face. FT8 and FT4 are fully supported, and the mode registry already covers WSPR, JT65, Q65, MSK144, JS8, PSK31, RTTY and CW for upcoming decoder integrations.

### Recording
- **Bit-exact capture** in shared mode (it never takes the device from WSJT-X). It records at the device's native rate, including the real rate behind PipeWire and PulseAudio on Linux, with dither off and no resampling, gain or processing.
- **Verified FLAC chunks:** each is decoded back after writing, and its audio MD5 is checked against the device's bytes before it's kept. Crash recovery restores interrupted chunks as a bit-exact prefix.
- **Chunks follow the mode's timing:** 5 minutes on slot edges, 6 minutes on even minutes for WSPR, and the reported T/R period for Q65. A chunk also splits at the exact sample of a band or mode change.
- **Timing references:** the audio stream is the clock, with wall-clock sync points about once a second and NTP checks at start and every 10 minutes. The pipeline can then correct sound-card and computer-clock drift; the recorder never changes the audio.
- **WSJT-X/JTDX UDP listener** (receive only, unicast or multicast), built from captured traffic and our own protocol notes. It provides mode, frequency, TX intervals and decodes.
- **Robustness:** a busy WSJT-X port doesn't stop recording, low disk space is warned about, and a crashing source never touches audio.

### Data and privacy
- **Session layout:**
  - `recordings/`: FLAC files and schema-validated metadata
  - `labels/<source>/`: what WSJT-X decoded, kept apart from the recordings
  - `local/`: kept on this computer only, never uploaded
- **Unknown values are `null` with a reason**, never a guessed default.
- **Callsign and grid are the operator's choice:** sharing the callsign is opt-in, and the grid can be withheld or shared at 4, 6 or 8 characters. The operator's own call is redacted from decoded messages when it isn't shared, and machine details (hostname, user, paths, device serials) are scrubbed.
- **CC BY 4.0** license and attribution are written into every FLAC's tags.

### Contributing recordings
- **First-run setup:** `signal-archive-recorder` with no arguments walks through the terms, Hugging Face login (with a permission check), the audio input (ranked for the OS, with a level check), station details, a WSJT-X check and a clock check.
- **One pull request per session** to `signal-archive-project/signal-archive-intake`. The token is kept only in the OS keyring. `review`, `remove-chunk` and `upload --dry-run` are there first, and `status`/`requeue` follow up afterwards.
- **Radio-audio screening before every upload:** a decoder must have been running, and the signals it decoded must actually be present in the audio. Chunks from the wrong input (for example, a laptop microphone) stay on the computer.

### Project
- Licensed under the **Mozilla Public License 2.0**; contributed recordings are CC BY 4.0.
- No GPL dependencies (enforced in CI); DCO sign-off on every commit.
- CI on Linux and Windows with Python 3.11 and 3.14.

[Unreleased]: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases/tag/v0.1.0
