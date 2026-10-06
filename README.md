# Signal Archive Recorder

Station-side recorder for the **Signal Archive Project**. It captures bit-exact receive audio from your rig, collects metadata automatically from software you already run (starting with WSJT-X for FT8), and uploads sessions to the project's open dataset.

> **Status:** pre-alpha. Nothing records yet. Follow the build stages in [CLAUDE.md](CLAUDE.md).

## Principles

- **Zero interference:** it only listens. It never takes exclusive control of your audio device, serial port or CAT, and never sends commands to WSJT-X or your rig.
- **Bit-exact audio:** lossless FLAC with no resampling, gain or processing.
- **Automatic metadata:** if software can know it, you're never asked for it.
- **Consent first:** nothing is uploaded without your consent, and you review every upload.

FT8 comes first. Other digital modes (FT4, WSPR, JS8, Q65, PSK31, RTTY and more) are planned soon after.

## Running it

```bash
pip install -e .
signal-archive-recorder devices                        # find your rig's sound card
cp examples/recorder.toml recorder.toml                # set [audio] device, [station], [wsjtx]
signal-archive-recorder record --config recorder.toml  # Ctrl-C to stop
```

To contribute recordings to the Signal Archive Project:

```bash
signal-archive-recorder consent                     # read and accept the terms (CC BY 4.0)
signal-archive-recorder login                       # paste a Hugging Face write token; kept in your OS keyring
signal-archive-recorder review --config recorder.toml        # see exactly what would be shared
signal-archive-recorder remove-chunk SESSION CHUNK --config recorder.toml   # optional
signal-archive-recorder upload --config recorder.toml        # one pull request per session
signal-archive-recorder status --config recorder.toml        # follow up the pull requests
```

`record` runs until Ctrl-C, then finishes the current chunk. Each session is saved to `~/SignalArchive/sessions/<UTC start>/`:

- `recordings/`: verified FLAC chunks and their metadata
- `labels/wsjtx/`: what WSJT-X decoded, kept separate from the recordings
- `session.json`: the session's details

No radio? Set `[audio] file = "something.wav"` to play a 16- or 24-bit WAV as if it were a sound card, and run `python tools/fake_wsjtx_emitter.py` to stand in for WSJT-X.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not hardware"   # offline test suite, no radio needed
ruff check && ruff format --check && mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. All commits must be signed off (`git commit -s`) under the [DCO](https://developercertificate.org/).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Signal Archive Recorder is an independent project. It works with WSJT-X and other programs only by reading their network output, and contains none of their code.
