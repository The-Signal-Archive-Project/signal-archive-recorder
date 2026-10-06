# Signal Archive Recorder

Station-side recorder for the **Signal Archive Project**. It captures bit-exact receive audio from your rig, collects metadata automatically from software you already run (starting with WSJT-X for FT8), and uploads sessions to the project's open dataset.

> **Status:** pre-alpha. It records and can upload, but expect changes before the first release. The build plan is in [CLAUDE.md](CLAUDE.md).

## Principles

- **Zero interference:** it only listens. It never takes exclusive control of your audio device, serial port or CAT, and never sends commands to WSJT-X or your rig.
- **Bit-exact audio:** lossless FLAC with no resampling, gain or processing.
- **Automatic metadata:** if software can know it, you're never asked for it.
- **Consent first:** nothing is uploaded without your consent, and you review every upload.

FT8 comes first. Other digital modes (FT4, WSPR, JS8, Q65, PSK31, RTTY and more) are planned soon after.

## Install

You need **Python 3.11 or newer**. The recorder installs straight from this repository; [pipx](https://pipx.pypa.io/) keeps it in its own environment:

```bash
pipx install git+https://github.com/The-Signal-Archive-Project/signal-archive-recorder
```

To update later: `pipx upgrade signal-archive-recorder`. (Plain `pip install git+https://…` works too.)

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
signal-archive-recorder init       # creates your config and asks which audio input is the radio
signal-archive-recorder record     # records until Ctrl-C
```

`init` writes the config to your user settings folder:
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
signal-archive-recorder upload           # one pull request per session
signal-archive-recorder status           # follow up the pull requests
```

### No radio?

Set `[audio] file = "something.wav"` to play a 16- or 24-bit WAV as if it were a sound card. With a clone of this repository, `python tools/fake_wsjtx_emitter.py` stands in for WSJT-X.

## Development

```bash
git clone https://github.com/The-Signal-Archive-Project/signal-archive-recorder
cd signal-archive-recorder
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not hardware"   # offline test suite, no radio needed
ruff check && ruff format --check && mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. All commits must be signed off (`git commit -s`) under the [DCO](https://developercertificate.org/).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Signal Archive Recorder is an independent project. It works with WSJT-X and other programs only by reading their network output, and contains none of their code.
