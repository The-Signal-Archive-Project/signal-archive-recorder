# Contributing to Signal Archive Recorder

Thanks for helping. A few rules keep the project healthy and Apache-2.0 clean.

## Workflow

- Branch from `main` and open a pull request. Don't push to `main` directly.
- CI must pass: `ruff check`, `ruff format --check`, `mypy`, `pytest -m "not hardware"`, and the dependency license check.
- The build follows the staged plan in [CLAUDE.md](CLAUDE.md). Each stage lists the tests that must pass before the next one starts.
- Tests must run without a radio, sound card, network or Hugging Face account; use the fakes in `tests/fakes/`. Real-hardware tests go in `tests/hardware/` and run with `pytest --hardware`.

## Licensing rules

The project is Apache-2.0, so **no GPL code may enter it.**

- **Don't copy other ham programs' code.** Never copy, port or adapt code, comments or constant tables from WSJT-X, JTDX, JS8Call, fldigi, Hamlib, Gpredict or other GPL/LGPL programs, and don't use their source as a protocol reference.
- **Document protocols in our own words.** Each protocol we read is described in `docs/protocols/`, from user documentation and traffic captured on your own station. Implement only from those notes.
- **Dependencies must be permissive.** Apache, MIT, BSD, ISC or PSF licenses are fine. LGPL is allowed only as a separately installed or dynamically loaded library, listed in `NOTICE`. GPL and AGPL are not allowed.
- **Add the SPDX header.** Every `.py` file starts with `# SPDX-License-Identifier: Apache-2.0`.

## Adding a new mode

Follow the "Adding a new mode" checklist in [CLAUDE.md](CLAUDE.md). A new mode should be data plus an adapter. If you find yourself editing core code, raise it in an issue first.
