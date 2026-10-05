# Signal Archive Recorder

Station-side recorder for the **Signal Archive Project**. It captures bit-exact receive audio from your rig, collects metadata automatically from software you already run (starting with WSJT-X for FT8), and uploads sessions to the project's open dataset.

> **Status:** pre-alpha. Nothing records yet. Follow the build stages in [CLAUDE.md](CLAUDE.md).

## Principles

- **Zero interference:** it only listens. It never takes exclusive control of your audio device, serial port or CAT, and never sends commands to WSJT-X or your rig.
- **Bit-exact audio:** lossless FLAC with no resampling, gain or processing.
- **Automatic metadata:** if software can know it, you're never asked for it.
- **Consent first:** nothing is uploaded without your consent, and you review every upload.

FT8 comes first. Other digital modes (FT4, WSPR, JS8, Q65, PSK31, RTTY and more) are planned soon after.

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
