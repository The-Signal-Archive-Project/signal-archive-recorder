# Signal Archive Recorder

Station-side recorder for the **Signal Archive Project**. It captures bit-exact receive audio from your rig, collects metadata automatically from software you already run (starting with WSJT-X for FT8), and uploads sessions to the project's open dataset.

> **Status:** v0.2.0 (alpha). It records, labels and uploads FT8/FT4 sessions, with a desktop app (window and tray icon) or from the terminal. Expect changes before 1.0; see the [changelog](CHANGELOG.md). The build plan is in [CLAUDE.md](CLAUDE.md).

> **Beta testers wanted!** If you run WSJT-X or JTDX, see the **[tester guide](TESTING.md)**. It covers installing, what to try and how to report back.

## Principles

- **Zero interference:** it only listens. It never takes exclusive control of your audio device, serial port or CAT, and never sends commands to WSJT-X or your rig.
- **Bit-exact audio:** lossless FLAC with no resampling, gain or processing.
- **Automatic metadata:** if software can know it, you're never asked for it.
- **Consent first:** nothing is uploaded without your consent, and you review every upload.

FT8 comes first. Other digital modes (FT4, WSPR, JS8, Q65, PSK31, RTTY and more) are planned soon after.

## Install

### Windows (installer)

Download `SignalArchiveRecorder-<version>-Setup.exe` from the [latest release](https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases) and run it. It installs for your user only (no administrator rights needed), adds a Start-menu entry and, if you leave the box ticked, starts with Windows. On first start a setup window walks you through everything; after that the recorder lives in the system tray (lower right) and only records while WSJT-X is running.

- **"Windows protected your PC"?** The beta installer isn't code-signed yet. Click **More info**, then **Run anyway**. Signing comes before 1.0.
- **Antivirus warnings:** some antivirus tools are wary of new, unsigned programs. If yours blocks it, please tell us (with the antivirus name) so we can report the false positive.
- **Uninstalling** (Settings, Apps) keeps your recordings, settings and Hugging Face login by default. It offers to remove your token, or everything (with a clear warning, since that includes recordings not yet uploaded).

The command line is installed too, as `signal-archive-recorder.exe` in the program folder (`%LOCALAPPDATA%\Programs\Signal Archive Recorder`).

### Debian and Ubuntu

**Recommended: add our package repository**, so updates come with your usual `apt upgrade`. The steps are on **[our Debian/Ubuntu page](https://the-signal-archive-project.github.io/signal-archive-recorder/)**. In short, for the stable channel (use `beta` in place of `stable` to test betas):

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://the-signal-archive-project.github.io/signal-archive-recorder/signal-archive.asc | sudo tee /etc/apt/keyrings/signal-archive.asc > /dev/null
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/signal-archive.asc] https://the-signal-archive-project.github.io/signal-archive-recorder stable main" \
  | sudo tee /etc/apt/sources.list.d/signal-archive.list
sudo apt update && sudo apt install signal-archive-recorder
```

(The stable channel fills when 0.3.0 is released; until then, use `beta`.) Or download `signal-archive-recorder_<version>_amd64.deb` from the [latest release](https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases/latest) and run `sudo apt install ./signal-archive-recorder_*_amd64.deb`, which installs it without updates.

It brings everything it needs (its own Python), so it works on Debian 12 or newer and Ubuntu 22.04 or newer. Start **Signal Archive Recorder** from your applications menu, or run `signal-archive-recorder-gui`; `signal-archive-recorder` is the command line. To keep your Hugging Face login, you need a keyring service (GNOME Keyring, KWallet or KeePassXC); most desktops have one.

### Arch Linux

The AUR package is ready, but it's waiting for the AUR to reopen account registration (paused because of spam). Until then, install with pipx:

```bash
sudo pacman -S --needed python-pipx portaudio
pipx install "signal-archive-recorder[gui] @ git+https://github.com/The-Signal-Archive-Project/signal-archive-recorder@v0.3.0-beta.2"
signal-archive-recorder-gui
```

Removing a package keeps your recordings and settings. To remove those too, run `signal-archive-recorder forget --everything` before uninstalling.

### With pipx (Linux, macOS, or Windows from source)

You need **Python 3.11 or newer**. The recorder installs straight from this repository; [pipx](https://pipx.pypa.io/) keeps it in its own environment:

```bash
pipx install git+https://github.com/The-Signal-Archive-Project/signal-archive-recorder@v0.2.0
```

That installs the v0.2.0 release. To get a newer release later, run the same command with its tag and `--force`. To follow the latest development version instead, leave off `@v0.2.0`. (Plain `pip install git+https://…` works too.)

**Linux needs two system pieces** (Windows and macOS have them built in):
- **PortAudio**, for audio input:
  - Debian/Ubuntu/Raspberry Pi OS: `sudo apt install libportaudio2`
  - Fedora: `sudo dnf install portaudio`
  - Arch: `sudo pacman -S portaudio`
- **A keyring service**, only for uploading, to keep your Hugging Face token safe:
  - GNOME and KDE desktops already have one.
  - On Hyprland, Sway and similar, install `gnome-keyring` and start it with `gnome-keyring-daemon --start --components=secrets`.

  The token is never written to a file.

## Quick start

```bash
signal-archive-recorder
```

The first time, this runs **setup**:
1. the contribution terms
2. your Hugging Face login, with a check that the token can upload
3. your radio's audio input, picked from a recommended list for your OS, with a quick level check
4. your callsign and grid, and how much of them to share
5. a check for WSJT-X
6. a clock check

After that, the same command runs the recorder until Ctrl-C. It **only records while WSJT-X is running**: until then it waits, ready, with the sound card closed. When WSJT-X starts, a session begins, and when WSJT-X closes (or goes quiet for 30 seconds), the session is saved and the recorder waits again. Set `[recording] start = "always"` to record from start to stop instead. Run `signal-archive-recorder setup` to change your answers later, or `signal-archive-recorder init --device NAME` to write a config without questions (for scripts).

Setup writes the config to your user settings folder:
- Linux: `~/.config/signal-archive-recorder/recorder.toml`
- macOS: `~/Library/Application Support/signal-archive-recorder/`
- Windows: `%APPDATA%\signal-archive-recorder\`

Every command uses it automatically; pass `--config FILE` to use another. Edit it to set your callsign and grid, and whether to share them (sharing the callsign is opt-in; the grid defaults to 4 characters).

`record` saves each session to `~/SignalArchive/sessions/<UTC start>/`:
- `recordings/`: verified FLAC chunks and their metadata
- `labels/wsjtx/`: what WSJT-X decoded, kept apart from the recordings
- `session.json`: the session's details

### Contributing recordings

```bash
signal-archive-recorder consent          # read and accept the terms (CC BY 4.0)
signal-archive-recorder login            # paste a Hugging Face "Write" token (huggingface.co/settings/tokens)
signal-archive-recorder review           # see exactly what would be shared
signal-archive-recorder remove-chunk SESSION CHUNK   # optional: leave a chunk out
signal-archive-recorder upload --dry-run # see exactly what would be sent (and kept back), sending nothing
signal-archive-recorder upload           # one pull request per session
signal-archive-recorder status           # follow up the pull requests
signal-archive-recorder requeue SESSION  # send a session again (e.g. if its PR was deleted)
signal-archive-recorder cleanup --dry-run   # which confirmed uploads would be deleted to save space
```

**Stereo inputs:** most radio interfaces appear as stereo, but usually only one channel carries the receiver (the other is a copy, or silent). Setup checks, and then records only the channel that matters, which halves the size without losing anything. It's picked out of the stereo stream byte for byte, so it stays bit-exact. Channels that really differ (a second receiver, or I/Q) are both kept. The choice is `[audio] keep_channel` (`both`, `left` or `right`).

**New versions:** once a day the recorder asks GitHub whether a newer version is out, and says so in the window, the tray and the terminal. It never downloads or installs anything. Testers on a beta also hear about newer betas. Turn it off with `[updates] check = false`, or check by hand with `signal-archive-recorder check-update`.

**Optional settings in the config:**
- `[upload] schedule = "while_recording"` or `"overnight"`: upload in the background while `record` runs (the default is `"manual"`).
- `[upload] max_mbps`: cap the average upload speed.
- `[storage] max_gb` and `delete_after_days`: keep the archive small. Only sessions whose upload has been confirmed are ever deleted.

Uploads resume where they left off if the connection drops.

Before anything is sent, the recorder checks that each chunk really is your radio's receive audio: a decoder such as WSJT-X was running, and the signals it decoded are actually present in the recording. Chunks that fail (for example, the wrong audio input was picked) stay on your computer, in the session's `local/excluded/` folder, with the reason.

### Desktop app (window and tray icon)

```bash
pipx install --force "signal-archive-recorder[gui] @ git+https://github.com/The-Signal-Archive-Project/signal-archive-recorder@v0.2.0"
signal-archive-recorder tray      # or: signal-archive-recorder-gui
```

The first time, a setup window walks through the same steps as the terminal setup (terms, Hugging Face login, audio input with a level test, station, WSJT-X and clock checks). After that it records just like `record`, with a small window and a tray icon whose colour says how things are going: **blue** ready, waiting for WSJT-X, **green** recording and all good, **yellow** recording but something needs a look (no WSJT-X, clipping, clock off, uploads stuck), **red** not recording, **grey** paused. The window shows a checklist and an audio level meter, and lets you:
- **Mark this:** add a note ("strong QRM", "rare DX") at this moment of the recording; it's saved with the chunk.
- **Pause / Resume:** pausing ends the session cleanly and stops recording even while WSJT-X runs; resuming records again (a new session).
- **Review & upload:** see exactly what each session would share and which chunks would be kept back, then upload.
- **Start when I log in**, and open the recordings folder or the settings file.
- **Save diagnostics:** a zip to attach to a bug report. It holds no audio or token, and your computer name, user name, callsign and grid are redacted (also `signal-archive-recorder diagnostics`).

Closing the window keeps recording in the tray; **Quit** in the tray menu stops and saves. The window needs the optional `gui` extra (Qt for Python, about 230 MB); everything else works without it.

### No radio?

Set `[audio] file = "something.wav"` to play a 16- or 24-bit WAV as if it were a sound card (with `[recording] start = "always"`, or a WSJT-X stand-in so it knows when to record). With a clone of this repository, `python tools/fake_wsjtx_emitter.py` stands in for WSJT-X.

## Community

- **Questions, station reports and release news:** the groups.io group, <https://groups.io/g/signal-archive>. You can also post by email to `main@signal-archive.groups.io`.
- **Live chat:** our Discord, <https://discord.gg/R8g5F8BcAC>.
- **Bugs and test reports:** [GitHub Issues](https://github.com/The-Signal-Archive-Project/signal-archive-recorder/issues/new/choose). No GitHub account? Post to the group instead.
- **Security problems:** please report them privately ([SECURITY.md](SECURITY.md)).

## Development

```bash
git clone https://github.com/The-Signal-Archive-Project/signal-archive-recorder
cd signal-archive-recorder
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not hardware"   # offline test suite, no radio needed
ruff check && ruff format --check && mypy
```

Test uploads go to the **test** repository [`signal-archive-project/signal-archive-intake-test`](https://huggingface.co/datasets/signal-archive-project/signal-archive-intake-test), never the real intake. Point a separate dev config at it (`[upload] repo = ...`), and wipe it when needed with `python tools/reset_test_repo.py`, which refuses any repository whose name doesn't end in `-test`.

Releases are cut with `python tools/release.py` (see CLAUDE.md, "Versions"). See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. All commits must be signed off (`git commit -s`) under the [DCO](https://developercertificate.org/).

## License

The software is licensed under the [Mozilla Public License 2.0](LICENSE) (MPL-2.0). You can use it, including inside closed-source products, but if you distribute changed versions of its files, you must publish those changes under the MPL too. See also [NOTICE](NOTICE). Recordings contributed to the Signal Archive Project are licensed separately, under CC BY 4.0. Signal Archive Recorder is an independent project. It works with WSJT-X and other programs only by reading their network output, and contains none of their code.
