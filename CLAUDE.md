# Signal Archive Recorder

Signal Archive Recorder is the station-side app of the **Signal Archive Project**. It records bit-exact receive audio from a ham station, collects every bit of metadata it can get automatically, and uploads sessions as pull requests to the project's Hugging Face intake dataset.

- **Spec (source of truth):** "Signal Archive Recorder: Developer Reference" at https://claude.ai/artifact/3rtXJiU8buDYtBUx4J6izq. If this file and the spec disagree, follow the spec and update this file. Use "Signal Archive Recorder" for the app and "Signal Archive" for the wider project in all code, docs and UI text.
- **Repo:** https://github.com/The-Signal-Archive-Project/signal-archive-recorder (org: The-Signal-Archive-Project). Work on branches and open PRs into the default branch; never push to it directly. Every commit needs a DCO `Signed-off-by` line (`git commit -s`; see CONTRIBUTING.md). The sign-off is the human contributor certifying the change, so only commit after they have asked for it.
- **License: Apache-2.0.** See "Licensing" below before adding any dependency or protocol code.

## Licensing (Apache-2.0)

Everything in this repo is Apache-2.0, so no GPL code may enter it. WSJT-X, JTDX, JS8Call, fldigi, Gpredict and Hamlib are all GPL or LGPL programs. We interoperate with them **only over their network interfaces** (UDP, TCP, XML-RPC) as a separate process, and we never copy, port, link or vendor their code.

- **Don't read or copy their source code.** Don't port code, comments, field tables, or enum and constant definitions out of their source files. Don't paste their headers or code into prompts, docs or tests.
- **Implement protocols from our own docs.** Each protocol we read is documented in `docs/protocols/<source>.md`, written by us in our own words, from the program's user-facing docs and from datagrams we capture on our own station. Implement only from that doc. Note the evidence for each field there (which capture, which program version).
- **Fixtures are our own captures.** UDP fixtures are datagrams recorded from our own station with `tools/capture_udp.py`, plus synthetic packets we build. They are data a program emitted, not code from it.
- **Write fakes independently.** `fake_wsjtx_emitter.py` and the other fakes are built from our protocol docs, never adapted from the real programs' code or bundled sample tools.
- **Dependencies must be permissive** (Apache, MIT, BSD, ISC, PSF) or LGPL used only as a separately installed or dynamically loaded library, such as PySide6 or libsndfile. GPL and AGPL dependencies are not allowed. CI enforces this with a license check, and any LGPL dependency is listed in `NOTICE` along with how it is used.
- **Every source file starts with** `# SPDX-License-Identifier: Apache-2.0`.
- **Third-party program names** (WSJT-X, JTDX and so on) appear only to describe compatibility. They never appear in the app name, icons or branding.

## Scope: FT8 first, many modes soon

v0.1 targets **FT8 through WSJT-X**, but we'll add more modes quickly: FT4, WSPR, JT65, Q65, MSK144, JS8, and later PSK31, RTTY, Olivia and CW through fldigi, plus satellites and SDR/IQ. **Design every stage so that adding a mode means adding data and an adapter, not editing core code.** In practice:

1. **No mode names in core code.** The session manager, chunker, metadata builder and uploader never test `if mode == "FT8"`. Mode-specific behaviour comes from the mode registry (`modes.json`) or from a source adapter.
2. **The mode registry is data.** Each entry in `src/signal_archive_recorder/modes/modes.json` gives:
   - `id` (stable, lowercase, such as `ft8`) and `family` (`wsjt`, `fldigi`, `js8`, `cw`, `analog`, …)
   - `timing`: `{"kind": "slotted", "period_s": 15, "allowed_periods_s": [15], "anchor": "utc_midnight"}` or `{"kind": "async"}`. Modes with variable slot lengths (Q65, MSK144, JS8) list them all, and the source reports the period in use.
   - `nominal_bw_hz` and `sideband`
   - `aliases`: per decoder program, the raw strings it reports (WSJT-X says `"FT8"`, fldigi says `"BPSK31"`, …)
   - `params_schema`: the name of a JSON sub-schema for the mode's `mode_params`
   - **Only decoder programs identify the mode** (WSJT-X, JTDX, JS8Call, fldigi). A rig's operating mode (Hamlib/flrig `PKTUSB`, `USB`, `CW`, …) describes how the rig is set up, not which signal is being received. Rig sources publish it as `SettingChanged(name="rig_mode")` and never as a mode. The schema only accepts aliases from decoder sources, and aliases must be unique per source.
   - With no decoder running, a chunk's `mode_id` is `null` with reason `source_unavailable`, and its `rig_mode` is still recorded. Never infer the mode from the rig mode or the frequency.
   - `registry.schema.json` validates the file, and `ModeRegistry` adds cross-checks (unique ids and aliases, periods that divide a day, the `unknown` fallback).
3. **Metadata sources are adapters.** Every source (WSJT-X, JTDX, JS8Call, rigctld, flrig, fldigi, satellite software, clock) implements one `Source` protocol and only emits normalised, timestamped events onto the session bus: `FreqChanged`, `ModeChanged`, `TxStarted`/`TxEnded`, `Decode`, `SourceUp`/`SourceDown`, `SettingChanged`. Adapters keep their raw fields in an event's `raw` dict.
4. **Chunk policy is derived from the mode's timing.**
   - Chunk length is the smallest multiple of the slot period that is ≥ the default (5 minutes).
   - Chunk boundaries fall on slot edges, anchored to UTC midnight.
   - Results: FT8, FT4 and JS8 get 5 minutes; WSPR's 2-minute slots give 6 minutes on even minutes; Q65-300 gives 5 minutes; async modes get 5 minutes aligned to the UTC minute.
   - A mode or dial-frequency change always starts a new chunk immediately.
   - Never hardcode "5 minutes aligned to the minute" anywhere except as the policy's default.
5. **The core schema is mode-agnostic.** `chunk.meta.json` has generic fields plus `mode_id` and `mode_params`, which is validated by the per-mode sub-schema. Adding a mode must never change the core schema version.
6. **Decodes are per source.** Write them as `<source>_decodes.jsonl` in the session folder (`wsjtx_decodes.jsonl`, `js8call_decodes.jsonl`, `fldigi_decodes.jsonl`). Each line has the normalised fields plus `raw`.
7. **Per-mode stats are optional.** Median DT only makes sense for slotted WSJT-family modes. For other modes it is `null` with reason `"not_applicable"`.
8. **Contract tests cover every registered mode.** `tests/contract/` is parametrised over the registry, so a new entry is tested automatically (see "Adding a new mode" below).

## Non-negotiable rules (from the spec)

- **Zero interference.** Never open audio exclusively, never open a serial port, and never send control messages to WSJT-X, the rig, rigctld or flrig except read-only queries. The UDP listener only receives.
- **Bit-exact audio.** No resampling, gain, dither, or bit-depth or channel conversion. Record the format the device *actually* delivered, and warn if the OS is resampling.
- **Audio wins.** A crashing or silent metadata source must never stop or corrupt capture. Each source runs isolated (its own thread or task, exceptions caught and logged), and its failure becomes a `SourceDown` event and a gap in the metadata.
- **Unknown is `null` with a reason, never a default.** Reasons form a closed enum: `source_unavailable`, `not_reported`, `not_applicable`, `user_withheld`. A wrong default is worse than a blank.
- **Privacy filtering happens before upload.** Strip anything not in the schema. No hostnames, file paths, OS usernames or device serials in uploaded files. Grid precision defaults to 4 characters, and 6 only with opt-in for satellite work.
- **Nothing is uploaded without stored consent.** Keep local files until the PR is confirmed created.
- **Tokens live only in the OS keyring,** never in config files, logs or exception messages.

## Stack and layout

Python ≥ 3.11. Libraries: `sounddevice`, `soundfile` (plus the `flac` CLI for verification), `numpy`, `ntplib`, `jsonschema`, `huggingface_hub`, `keyring`, `PySide6`, `sigmf` (later). Use stdlib `socket` + `struct` for QDataStream, plain TCP for rigctld, and `xmlrpc.client` for flrig and fldigi.

```
src/signal_archive_recorder/
  core/        events.py (event types), bus.py, clock.py (injectable Clock: never call time.time() directly elsewhere)
  modes/       modes.json, registry.py, chunk_policy.py, schemas/<mode_params>.json
  audio/       device.py, ringbuffer.py, capture.py, flac_writer.py, levels.py
  sources/     base.py (Source protocol), wsjtx/{qdatastream.py,messages.py,listener.py},
               rigctld.py, flrig.py, fldigi.py, js8call.py, satellite/
  clockmon/    ntp.py, os_sync.py, gps.py
  session/     manager.py (owns timeline), chunker.py, storage.py (session folder layout)
  metadata/    builder.py, privacy.py, schemas/{session,chunk}.schema.json
  upload/      queue.py, hf.py, consent.py
  ui/          Qt window + tray (thin: calls session/upload APIs only)
  cli.py       headless mode (config file + CLI), used on Raspberry Pi and in integration tests
tests/
  unit/  integration/  contract/  hardware/ (skipped unless --hardware)
  fakes/       fake_audio.py, fake_wsjtx.py, fake_rigctld.py, fake_flrig.py, fake_ntp.py, fake_hf.py
  fixtures/    reference WAVs (16/24-bit, 48k/44.1k, mono/stereo, includes FT8 slots),
               UDP datagrams (*.bin) captured from our own station with WSJT-X/JTDX running
docs/protocols/  our own write-ups of each wire protocol we read (wsjtx-udp.md, js8call-udp.md, rigctld.md, ...)
tools/fake_wsjtx_emitter.py   lets contributors drive the app without a radio (built from docs/protocols)
tools/capture_udp.py          records raw datagrams from a live station into fixtures/udp/
LICENSE (Apache-2.0), NOTICE
```

## Working rules for Claude

- **Build in the stage order below.** Don't start a stage until every test from earlier stages passes (`pytest -m "not hardware"`). Each stage ends with green tests, `ruff check`, `ruff format --check` and `mypy --strict src/`.
- **Write the stage's tests first** (or alongside the code), using the fakes. Tests never need a radio, sound card, network or HF account. Real-hardware checks go under `tests/hardware/`.
- **Inject time everywhere** through `core.clock.Clock`. Tests use `FakeClock` so that boundary and NTP tests are deterministic. Times are integer nanoseconds since the Unix epoch (UTC).
- **`tests/unit/test_architecture.py` enforces two rules:** no mode names (ids, display names, aliases) as string literals outside `modes/` and `sources/`, and no direct `time`/`datetime` clock reads outside `core/clock.py`.
- **UDP parser fixtures** come from datagrams captured on our own station plus hand-built ones. When the protocol is unclear, capture more traffic and update `docs/protocols/`; never consult the other program's source code (see Licensing).
- **Keep the UI thin.** If logic is creeping into `ui/`, move it into `session/` or `upload/` and test it there.

Commands:

```
pip install -e ".[dev]"
pytest -m "not hardware"            # full offline suite
pytest tests/contract               # every registered mode
pytest --hardware tests/hardware    # with a real rig/sound card attached
python tools/fake_wsjtx_emitter.py --mode FT8 --port 2237
signal-archive-recorder --headless --config dev.toml
```

---

## Build stages and their exit tests

Each stage lists **what to build** and **the tests that must pass to call it done.**

### Stage 0: Scaffold

Build:
- `pyproject.toml` (distribution `signal-archive-recorder`, import package `signal_archive_recorder`, `license = "Apache-2.0"`, src layout, `[dev]` extras)
- `LICENSE` (full Apache-2.0 text), `NOTICE`, SPDX headers
- CI license check (for example `pip-licenses --fail-on` a GPL/AGPL list) over the installed dependency tree
- ruff, mypy and pytest config, plus a `hardware` marker that is skipped by default
- CI workflow running on Windows and Linux
- Empty package with `__version__`

Exit tests:
- `pytest` runs and collects 0 failures; `ruff` and `mypy --strict` pass.
- `test_version_exposed`: `signal_archive_recorder.__version__` matches `pyproject.toml`.
- `test_spdx_headers`: every `.py` file under `src/`, `tests/` and `tools/` starts with the Apache-2.0 SPDX line.
- The CI license check passes, with no GPL or AGPL packages in the dependency tree.

### Stage 1: Core events, clock, mode registry, chunk policy

Build:
- Event dataclasses and the bus (thread-safe, with timestamps from `Clock`)
- `Clock`/`FakeClock`
- `modes.json` with at least `ft8`, `ft4`, `wspr`, `jt65`, `q65`, `msk144`, `js8`, `psk31`, `rtty`, `cw`, plus `unknown`
- `registry.py` (lookup by id and by `(source, raw_string)`)
- `chunk_policy.py`

Exit tests:
- `test_registry_loads_and_validates`: every entry matches the registry schema, ids are unique, and aliases are unique per source.
- `test_alias_lookup`: `("wsjtx","FT8") → ft8`, and an unknown raw string returns `unknown` with `raw` kept and `needs_mapping=True`.
- `test_rig_control_sources_cannot_alias`: a `hamlib` alias in modes.json is rejected by the schema.
- `test_chunk_policy_ft8`: 5-minute chunks; the boundaries are exact multiples of 300 s since UTC midnight and therefore fall on 15 s slot edges.
- `test_chunk_policy_wspr`: 6-minute chunks on even minutes.
- `test_chunk_policy_async`: 5-minute chunks on minute boundaries.
- `test_chunk_policy_property` (hypothesis): for every slotted mode in the registry, the chunk length is a multiple of the slot period and every boundary is a slot edge.
- `test_bus_ordering`: events from multiple threads come out in timestamp order, and none are lost under 10k events/s.

### Stage 2: Audio capture (bit-exact) with ring buffer

Build:
- `device.py` (enumerate devices, open in shared mode, report the *delivered* format and detect OS resampling)
- `ringbuffer.py`
- `capture.py` (callback → ring buffer → writer thread)
- `levels.py` (peak, RMS, clipped count)
- A `FakeAudioDevice` that replays fixture WAVs through the same callback interface and can stall or jitter

Exit tests:
- `test_bit_exact_passthrough[int16|int24|float32 × mono|stereo × 44100|48000]`: samples arriving at the writer are byte-identical to the fixture.
- `test_delivered_format_recorded`: if the fake reports 44.1 kHz when 48 kHz was requested, metadata says 44100 and a resampling warning is raised.
- `test_writer_stall_no_drop`: a 2 s writer stall with a buffer sized for ≥ 5 s loses no samples.
- `test_overrun_annotated`: a stall longer than the buffer produces an `AudioGap` event with the exact count of lost samples and a start timestamp. Total samples written plus lost equals total delivered.
- `test_levels`: known sine and full-scale fixtures give the expected peak/RMS (±0.01 dB), and the clipped count is exact.
- `test_exclusive_only_warns` (fake): a device that refuses shared mode raises a clear user-facing warning and never grabs the device exclusively.
- Hardware (`tests/hardware/test_capture_real.py`): records 30 s from a real device while WSJT-X keeps decoding.

### Stage 3: FLAC writer and verification

Build:
- `flac_writer.py`: streaming FLAC at maximum compression, with the MD5 in STREAMINFO
- On close: verify by decoding and comparing MD5 (or using `flac -t`), then compute the file's SHA-256
- Atomic finalisation (`.partial` → rename)

Exit tests:
- `test_flac_roundtrip_exact` (all formats from Stage 2): decoding gives identical samples, and the STREAMINFO MD5 equals the MD5 of the source PCM.
- `test_flac_verify_detects_corruption`: flipping a byte in a finished file makes verification fail and the chunk is flagged, not uploaded.
- `test_partial_file_on_crash`: killing the writer mid-chunk leaves a `.partial`, which startup recovery either finalises (if decodable) or flags.
- `test_sha256_matches_file`.

### Stage 4: WSJT-X / JTDX UDP listener (first Source adapter)

Build:
- `tools/capture_udp.py` (receive-only), then capture a live session into `tests/fixtures/udp/<name>/`: one `.bin` per datagram, plus `index.jsonl` (receive times) and `actions.jsonl` (what the operator did, and when). WSJT-X needs no radio: use Rig "None", and File → Open on sample WAVs to produce decodes.
- `docs/protocols/wsjtx-udp.md`: our own description of every field, each with its evidence from the captures, and marked verified or unverified. WSJT-X's user guide points to its GPL source for the protocol, so captures are the only reference.
- `qdatastream.py`: Qt `QDataStream` reader and writer, from Qt's public serialization docs.
- `messages.py`: header (magic `0xADBCCBDA`, schema, type, client id) and Heartbeat (0), Status (1), Decode (2) and Close (6), each with `encode()`. Other types are ignored and counted.
- `listener.py`: receive-only unicast or multicast socket, per-client state, and the events `SourceUp`/`SourceDown` (30 s timeout, or Close), `FreqChanged`, `ModeChanged` (with the Status T/R period), `TxStarted`/`TxEnded` and `Decode`. Each decode is also written to `wsjtx_decodes.jsonl`. Decode mode symbols (`~` FT8, `+` FT4) are registry aliases.
- **Off-air decodes** (WSJT-X decoding a WAV file, not the radio) are logged with `off_air: true`, have no absolute time, and must never count in chunk statistics.
- `tools/fake_wsjtx_emitter.py`: replays a captured session (`--replay DIR --speed N`) or generates synthetic traffic.

Exit tests:
- `test_parse_fixture_session1`: all captured datagrams parse, with hand-checked spot values (dial frequencies, modes, Q65 T/R period, Tune, the decode time matching the sample file name, the version).
- `test_encoder_reproduces_capture_exactly`: `encode(parse(d)) == d` for every captured datagram, so the fake emitter sends real WSJT-X bytes.
- `test_null_and_empty_strings`: null and empty text are distinguished.
- `test_truncated_and_garbage`, `test_random_bytes_never_raise` (hypothesis), `test_unknown_type_counted`: bad input is counted and dropped, never raised.
- `test_status_to_events`: replaying the capture gives exactly the expected `SourceUp` → band, mode, Q65-period and TX events → `SourceDown("closed")` sequence.
- `test_decode_event`: 40 decodes mapped to ft8/ft4, all flagged off-air, and logged to `wsjtx_decodes.jsonl`. Also `test_live_decode_gets_utc_date`, `test_live_decode_just_before_midnight`, `test_unmapped_symbol_uses_status_mode` and `test_unknown_mode_flagged`.
- `test_heartbeat_timeout`: `SourceDown` after 30 s (not before), closing an open TX interval, then `SourceUp` when the client returns.
- `test_listener_never_sends`: any `send*` on the socket is recorded and must stay empty.
- `test_multicast_shared`: two listeners in one multicast group both receive everything.
- **Still to capture:** WSPR, JTDX and other WSJT-X versions. Add them as new fixture folders and extend the protocol doc.

### Stage 5: Session manager and chunker

Build:
- `manager.py`, which owns the timeline: subscribes to the bus and holds per-chunk event windows
- `chunker.py`, which applies the chunk policy and splits on mode or frequency change
- TX interval tracking
- `storage.py` session folder layout: `sessions/<session_id>/{session.json, <chunk_id>.flac, <chunk_id>.meta.json, *_decodes.jsonl}`

Exit tests (all with FakeClock + FakeAudioDevice + fake emitter):
- `test_first_chunk_aligned`: starting at 12:03:07 gives a short first chunk ending at 12:05:00, then full 5-minute chunks.
- `test_first_sample_utc_exact`: each chunk's recorded first-sample time equals the session start plus the cumulative sample count divided by the rate (±1 sample).
- `test_split_on_freq_change` and `test_split_on_mode_change`: chunks split at the event timestamp, the concatenated samples equal the original stream, and no sample is duplicated or lost.
- `test_mode_change_changes_policy`: switching FT8 → WSPR mid-session moves later boundaries to even-minute 6-minute chunks.
- `test_tx_intervals`: TX on/off events show up as intervals in the right chunk(s), and audio keeps recording through TX.
- `test_source_crash_isolated`: a source that raises in a loop still leaves complete, verified audio chunks, and the metadata shows `source_unavailable` for the affected window.
- `test_sample_count_for_drift`: each chunk stores its actual sample count, and the app never corrects drift.

### Stage 6: Metadata generation, schemas and privacy

Build:
- `session.schema.json` and `chunk.schema.json` (JSON Schema 2020-12, versioned) plus per-mode `mode_params` sub-schemas
- `builder.py`, which assembles metadata from the chunk's event window
- `privacy.py`, which allow-lists fields (not deny-lists), truncates grids and scrubs strings

Exit tests:
- `test_session_and_chunk_validate`: generated files validate for every mode in the registry (contract-parametrised).
- `test_unknown_is_null_with_reason`: with no rig source, `rig_mode`, filter, AGC and NB/NR are `null` with reason `source_unavailable`, never `0`, `""` or a guessed value. With no decoder source, `mode_id` is `null` the same way.
- `test_median_dt`: the chunk's median DT and decode count are right for a fixture decode set, and are `null`/`not_applicable` for async modes.
- `test_events_list`: every freq, mode or setting change inside the chunk appears with its timestamp.
- `test_privacy_no_leaks`: the uploaded files have none of `socket.gethostname()`, `getpass.getuser()`, the home directory path, device serials or the token, checked with a recursive string scan of every output file including decodes.
- `test_grid_precision`: a 6-character station grid is stored as 4 characters by default, and as 6 only when satellite opt-in is set.
- `test_extra_fields_stripped`: fields an adapter puts in metadata that aren't in the schema are removed, and the removal is logged locally.
- `test_unmapped_mode_flagged`: an unknown raw mode string is stored raw with `needs_mapping: true` and still validates.

### Stage 7: Headless CLI, end-to-end (finishes v0.1)

Build:
- `cli.py --headless --config file.toml`
- Startup recovery of `.partial` files
- Graceful shutdown that finalises the current chunk and writes `session.json` end time

Exit tests:
- `test_e2e_ft8_session` (integration): a 12-minute simulated FT8 session (fake audio from fixtures, fake WSJT-X with decodes, one band change, two TX periods) produces the expected chunk count. Every FLAC verifies, every JSON validates, decodes are in the JSONL, and the privacy scan is clean.
- `test_sigterm_mid_chunk`: SIGTERM finalises a valid short chunk, and `session.json` has an end time.
- `test_restart_recovers_partial`.
- Hardware/manual: run against your own station for an evening and spot-check that the decodes line up with audio timestamps.

**→ Tag v0.1.**

### Stage 8: Consent, token and upload (v0.2)

Build:
- `consent.py`: license text, acceptance stored with timestamp and license ID
- Token storage through `keyring` and a setup check that the token can open PRs on the intake repo
- `queue.py` (states: queued → uploading → pr_opened → validated or failed)
- `hf.py` (`upload_folder(..., create_pr=True)`, one PR per session)
- Status polling of PR comments
- Review step: list chunks and fields, delete a chunk

Exit tests (all against `fake_hf.py`):
- `test_no_upload_without_consent`.
- `test_token_never_on_disk`: after setup, scan the config dir, logs and session folders for the token, and confirm repr/str of the config objects masks it.
- `test_token_permission_check`: a read-only token gets a clear error that names the permission needed.
- `test_one_pr_per_session`.
- `test_local_kept_until_pr_confirmed`: if the upload fails after the files are sent but before a PR exists, the files stay and the state is retryable.
- `test_deleted_chunk_not_uploaded`.
- `test_validator_status_polled`: a fake PR comment with a validator result moves the state to `validated` or `failed`.

### Stage 9: Robustness (v0.3)

Build:
- Clock monitor (NTP at start and every 10 minutes, OS sync state from `w32tm`, `timedatectl`/`chronyc` or `sntp`, optional GPS, green/yellow/red thresholds)
- Multicast setup help
- Resume and retry with backoff
- Bandwidth cap, "upload now" and "upload overnight"
- Disk limit and auto-delete of confirmed uploads after N days
- PySide6 tray and status window: checklist, level meter, "mark this" notes, pause/resume

Exit tests:
- `test_clock_thresholds`: offsets of 0.05, 0.3 and 0.8 s give green, yellow (recorded and flagged) and red (warned, still recording).
- `test_ntp_unreachable`: the offset is `null`/`source_unavailable` and recording continues.
- `test_os_sync_parsers`: captured output fixtures from each OS command parse correctly.
- `test_retry_backoff_and_resume`: an interrupted upload resumes without re-sending finished files, and the backoff delays are bounded.
- `test_bandwidth_cap`: measured throughput against the fake stays ≤ the cap (±10%).
- `test_disk_limit`: hitting the limit deletes only confirmed-uploaded sessions (oldest first). Unconfirmed sessions are never deleted. If nothing can be deleted, a warning is raised.
- `test_mark_note_event`: a "mark this" note lands in the right chunk's events with its timestamp.
- `test_tray_state_mapping`: health inputs map to green, yellow, red or grey (pure function, no Qt needed).
- UI smoke test with `pytest-qt`: the window opens, the status updates from a fake manager, and pause/resume works.

### Stage 10: More modes and sources (v0.4)

This is where most multi-mode work lands. Build:
- `rigctld.py` (TCP client to an already-running rigctld; polls freq, mode, passband and AGC every 1–2 s)
- `flrig.py` and `fldigi.py` (XML-RPC: active modem and its parameters)
- `js8call.py` (JS8Call JSON-over-UDP API, receive only)
- Registry aliases for each source

Exit tests:
- `test_rigctld_readonly`: the fake rigctld logs every command; only get-commands (`f`, `m`, `l AGC`, …) are allowed and any set-command fails the test.
- `test_rigctld_unreachable_or_drop`: gives `SourceDown`, recording continues, and the adapter reconnects with backoff.
- `test_rig_mode_is_a_setting`: rigctld reporting `PKTUSB` publishes `SettingChanged(name="rig_mode")` and never `ModeChanged`. With WSJT-X on FT4, the chunk has `mode_id: "ft4"` and `rig_mode: "PKTUSB"`. With no decoder running, `mode_id` is `null`/`source_unavailable` and `rig_mode` is still recorded.
- `test_source_precedence`: when WSJT-X and rigctld disagree on frequency, WSJT-X wins for WSJT modes, both values are stored, and the disagreement is flagged.
- `test_fldigi_modem_params`: modem name and parameters land in `mode_params` and validate against that mode's sub-schema.
- Contract tests pass for every new registry entry.

### Stage 11: Satellites (v0.5)

Build:
- Gpredict and SatPC32 readers (satellite name, NORAD ID, Doppler correction mode)
- TLE capture with its epoch (software cache first, then CelesTrak)
- 6-character grid opt-in with an explanation

Exit tests:
- `test_gpredict_fixture_parse`.
- `test_tle_epoch_stored` (fetch fake).
- `test_doppler_mode_enum`: one of `downlink`, `uplink`, `both`, `none`, or `null`/`source_unavailable`.
- `test_sat_path_type`: satellite chunks have `path_type: "satellite"` and the satellite block validates.
- `test_six_char_grid_only_with_optin`.

### Stage 12: Packaging and launch (v1.0)

Build:
- PyInstaller or Briefcase installers for Windows and macOS, and `pipx` for Linux (including a Raspberry Pi headless guide)
- First-run wizard covering all 7 spec steps
- Docs and Space integration

Exit tests:
- The installer smoke test in CI starts the packaged app headless against the fake emitter and produces a valid session.
- `test_wizard_flow` (pytest-qt): all 7 steps finish with fakes, and backing out leaves no partial config.

### Later: SDR/IQ

SoapySDR input writing conforming SigMF with a clip-length cap. Treat it as another capture backend behind the same `capture` interface. Tests: SigMF output passes `sigmf` validation, the cap is enforced, and the metadata carries the same core fields.

---

## Adding a new mode (checklist)

1. Add an entry to `modes/modes.json` (timing, bandwidth, sideband, aliases per source, `params_schema`).
2. Add `modes/schemas/<mode>_params.json` if the mode has parameters.
3. If no existing source reports it, add a `sources/<name>.py` adapter that emits only bus events, with a fake under `tests/fakes/`.
4. Add fixtures: a short reference WAV and, if the decoder has one, captured protocol messages.
5. Run `pytest tests/contract`. The parametrised suite checks registry validity, chunk policy properties, metadata validation and unknown-field stripping for the new mode.
6. Core code, the core schema version and existing tests must not change. If they have to, the abstraction is wrong: fix the abstraction in its own change first.
