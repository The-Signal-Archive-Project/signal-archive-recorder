# Changelog

All notable changes to Signal Archive Recorder. Versions follow [semantic versioning](https://semver.org/); before 1.0, minor versions may change behaviour.

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

[0.1.0]: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases/tag/v0.1.0
