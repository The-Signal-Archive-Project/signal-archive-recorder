# Contributing to Signal Archive Recorder

Thanks for helping. A few rules keep the project healthy and its licensing clean.

## Workflow

- Branch from `main` and open a pull request. Don't push to `main` directly.
- CI must pass: `ruff check`, `ruff format --check`, `mypy`, `pytest -m "not hardware"`, and the dependency license check.
- The build follows the staged plan in [CLAUDE.md](CLAUDE.md). Each stage lists the tests that must pass before the next one starts.
- Tests must run without a radio, sound card, network or Hugging Face account; use the fakes in `tests/fakes/`. Real-hardware tests go in `tests/hardware/` and run with `pytest --hardware`.

## Developer Certificate of Origin (DCO)

Every commit must be signed off under the [Developer Certificate of Origin](https://developercertificate.org/). By signing off, you certify that you wrote the change, or otherwise have the right to submit it under the project's license (MPL-2.0). There's no form to sign; it's one line at the end of each commit message:

```
Signed-off-by: Your Name <you@example.com>
```

Git adds it for you with `-s`:

```bash
git commit -s -m "Add FT4 to the mode registry"
```

The name and email must match your git `user.name` and `user.email`. Use a real name, not an anonymous handle. A DCO check runs on every pull request. If it fails, add the sign-off to your existing commits and force-push your branch:

```bash
git rebase --signoff main
git push --force-with-lease
```

## Licensing rules

The software is licensed under the **Mozilla Public License 2.0** (MPL-2.0): file-level copyleft, so changes to its files come back as open source while the recorder can still be used inside closed-source products. To keep that possible, **no GPL code may enter it.**

- **Don't copy other ham programs' code.** Never copy, port or adapt code, comments or constant tables from WSJT-X, JTDX, JS8Call, fldigi, Hamlib, Gpredict or other GPL/LGPL programs, and don't use their source as a protocol reference.
- **Document protocols in our own words.** Each protocol we read is described in `docs/protocols/`, from user documentation and traffic captured on your own station. Implement only from those notes.
- **Dependencies must be permissive.** Apache, MIT, BSD, ISC, PSF or MPL-2.0 licenses are fine. LGPL is allowed only as a separately installed or dynamically loaded library, listed in `NOTICE`. GPL and AGPL are not allowed.
- **Add the license header.** Every `.py` file starts with `# SPDX-License-Identifier: MPL-2.0`, followed by the MPL notice (copy it from any existing file).

## Adding a new mode

Follow the "Adding a new mode" checklist in [CLAUDE.md](CLAUDE.md). A new mode should be data plus an adapter. If you find yourself editing core code, raise it in an issue first.
