# WSJT-X UDP messages (receive side)

Our description of the datagrams WSJT-X sends to its "UDP Server" address, written for Signal Archive Recorder. It is based **only on traffic captured from our own station** and on WSJT-X's user-facing behaviour. No WSJT-X source code was used (see the Licensing section of `CLAUDE.md`). Each field lists its evidence.

The recorder only **receives**. It never sends anything to WSJT-X.

## Evidence

| Capture | Program | Setup | Actions |
|---|---|---|---|
| `tests/fixtures/udp/wsjtx-session1` (153 datagrams) | WSJT-X 3.0.2-devel (AUR `wsjtx` 3.0.2) | Linux, Rig: None, no callsign/grid set, UDP server 127.0.0.1:2237 | `actions.jsonl`: idle on FT8 at 20 m; band change to 40 m; modes FT8 → FT4 → Q65 → FT8; decode of a sample FT8 WAV (File → Open); FT4 sample WAV; Tune for ~5 s; File → Exit |

"Verified" means the value changed in step with a known action, or matched an externally known value. "Unverified" means the field was constant across the capture; it is parsed only by position, and nothing in the recorder relies on it.

## Encoding

All values use Qt's `QDataStream` format (see `sources/wsjtx/qdatastream.py`): big-endian integers, IEEE-754 big-endian doubles, and booleans as one byte. Text is a **UTF-8 byte array**: a `u32` length followed by the bytes, where length `0xFFFFFFFF` means *null*, which is different from empty (length 0). Both occur in captures.

## Header (every datagram)

| Field | Type | Evidence |
|---|---|---|
| magic | `u32` = `0xADBCCBDA` | Constant in all 153 datagrams |
| schema | `u32` | `2` in every datagram |
| type | `u32` | `0`, `1`, `2` and `6` observed; see below |
| client id | utf8 | `"WSJT-X"` in every datagram (the program name, or the instance name when several run) |

| Type | Name | Seen | Cadence |
|---|---|---|---|
| 0 | Heartbeat | 29 | Every 15 s |
| 1 | Status | 83 | On any change, plus during decode cycles |
| 2 | Decode | 40 | One per decoded message |
| 6 | Close | 1 | Once, on exit |

A datagram with any other type is ignored and counted. A datagram with a wrong magic, or that ends mid-field, is dropped and counted; it is never fatal.

## Heartbeat (type 0)

| Field | Type | Evidence |
|---|---|---|
| max schema | `u32` | `3` (higher than the header's `2`, so this is the newest schema the sender supports) |
| version | utf8 | `"3.0.2-devel"`, which matches the installed version |
| revision | utf8 | `""` (empty) |

**Use:** liveness, and the WSJT-X version in session metadata. No heartbeat or status for 30 s means the source is down.

## Status (type 1)

The 108- and 112-byte sizes come from variable-length text.

| # | Field | Type | Evidence | Status |
|---|---|---|---|---|
| 1 | dial frequency | `u64` Hz | 14,074,000 → 7,074,000 at the band change; 7,047,500 for FT4 on 40 m | **Verified** |
| 2 | mode | utf8 | `FT8` → `FT4` → `Q65` → `FT8`, following the Mode menu | **Verified** |
| 3 | DX call | utf8 | Always null | Unverified |
| 4 | report | utf8 | Always `"-15"` | Unverified |
| 5 | TX mode | utf8 | Always equal to the mode | Unverified meaning |
| 6 | TX enabled | bool | Always false (Enable Tx was never pressed) | Unverified |
| 7 | transmitting | bool | `true` only while Tune was on (seq 144–145) | **Verified** |
| 8 | decoding | bool | `true` for about 1–3 s at the end of each 15 s FT8 cycle, and during the WAV decodes | **Verified** |
| 9 | RX audio offset | `u32` Hz | `1500` (the WSJT-X default) | Unverified |
| 10 | TX audio offset | `u32` Hz | `1500` | Unverified |
| 11 | own call | utf8 | Always empty (no callsign was configured) | Unverified |
| 12 | own grid | utf8 | Always empty (no grid was configured) | Unverified |
| 13 | DX grid | utf8 | Always null | Unverified |
| 14 | flag | bool | Always false | Unknown meaning |
| 15 | sub-mode | utf8 | Always null, even in Q65 | Unverified |
| 16 | flag | bool | Always false | Unknown meaning |
| 17 | value | `u8` | Always 0 | Unknown meaning |
| 18 | frequency tolerance | `u32` Hz, `0xFFFFFFFF` = not set | `50` only in Q65 (the Q65 "F Tol" control); unset otherwise | **Verified** |
| 19 | T/R period | `u32` s, `0xFFFFFFFF` = not set | `30` only in Q65 (Q65's 30 s sequences); unset otherwise | **Verified** |
| 20 | configuration name | utf8 | `"Default"` | Verified (default configuration name) |
| 21 | TX message | utf8 | Null, or `"TUNE"` while tuning | **Verified** |

**Use:** fields 1, 2, 7 and 19 drive `FreqChanged`, `ModeChanged` (with the T/R period for variable-period modes) and `TxStarted`/`TxEnded`. Field 8 is kept in `raw`.

**Compatibility:** bytes after field 21 are ignored, in case newer versions add fields. If a datagram ends exactly at a field boundary after field 10, the missing later fields are taken as absent. That is an assumption about older versions and is **unverified** until we capture one.

**Privacy:** fields 11–13 can hold the operator's callsign and grid. The listener keeps them out of event `raw` data, and Stage 6 only uses them through the privacy filter (with grid precision limits).

## Decode (type 2)

| # | Field | Type | Evidence | Status |
|---|---|---|---|---|
| 1 | new | bool | `true` for all 40 | Unverified meaning |
| 2 | time | `u32` ms since midnight UTC | `48,870,000` = 13:34:30, which matches the sample file `210703_133430.wav`; `2,000` = 00:00:02 matches `000000_000002.wav` | **Verified** |
| 3 | SNR | `i32` dB | −17 to +16 | Verified (range and sign) |
| 4 | DT | `double` s | −0.8 to +0.7 | Verified (range) |
| 5 | audio frequency | `u32` Hz | 244–3337, which matches the waterfall positions | Verified (range) |
| 6 | mode symbol | utf8 | `"~"` for every FT8 decode and `"+"` for every FT4 decode | **Verified** |
| 7 | message | utf8 | Decoded text, e.g. `"CQ F5RXL IN94"` | **Verified** |
| 8 | low confidence | bool | Always false | Unverified |
| 9 | off air | bool | **`true` for every decode, all of which came from a WAV file, not the radio** | **Verified** for file playback |

**Mode symbols** are registry aliases for the `wsjtx` source (`"~"` → ft8, `"+"` → ft4). An unknown symbol falls back to the mode from the latest status, with the raw symbol kept and flagged for mapping.

**Off-air decodes** come from a file being played back, so they don't describe the audio the recorder is capturing. They are kept in the decode log with `off_air: true`, and are excluded from chunk statistics (decode count, median DT).

**Date:** the time field has no date. For live decodes, the listener uses the UTC date at receipt; a time more than an hour after the receipt time is taken to be from just before midnight on the previous day. Off-air decodes get no absolute time.

## Close (type 6)

The header only. **Verified:** it was the last datagram, sent on File → Exit. **Use:** `SourceDown(reason="closed")` immediately.

## Not yet captured

- Other message types (QSO logged, WSPR decode, and others) are ignored until a capture shows them.
- Fields marked unverified, and other WSJT-X versions and JTDX: add captures under `tests/fixtures/udp/` and extend this table.
- WSPR mode: the Mode menu step was skipped in session 1.
