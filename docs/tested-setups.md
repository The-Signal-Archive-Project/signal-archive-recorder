# Tested setups

Which stations Signal Archive Recorder has been tested on, from beta test reports. Each row links to its report. ✅ works, ⚠️ works with problems (see the report), ❌ doesn't work yet.

| Result | Version | OS | Install | Radio | Audio interface | WSJT-X / JTDX | Sharing with | Report |
|---|---|---|---|---|---|---|---|---|
| ✅ | 0.3.0-beta.1 | Arch Linux (dev laptop) | from source | none (WSJT-X with Rig "None") | laptop input via PipeWire | WSJT-X 3.0.2 | none | developer test |
| ✅ | 0.3.0-beta.1 | Windows Server 2025 (CI) | Windows installer | none (fake WSJT-X) | WAV file | captured WSJT-X traffic | none | CI smoke test |
| ✅ | 0.3.0-beta.1 | Debian 12, Debian 13, Ubuntu 22.04, Ubuntu 24.04 (CI) | .deb | none (fake WSJT-X) | WAV file | captured WSJT-X traffic | none | CI smoke test |
| ✅ | 0.3.0-beta.1 | Arch Linux (CI) | AUR package | none (fake WSJT-X) | WAV file | captured WSJT-X traffic | none | CI smoke test |

## Wanted

The setups we most want reports from:
- **Rigs with built-in USB audio:** Icom IC-7300 / IC-705 / IC-7610, Yaesu FT-991A / FTDX10 / FTDX101, Kenwood TS-590SG / TS-890
- **External interfaces:** SignaLink USB, Digirig, any CM108-based interface
- **Software sharing WSJT-X's data:** GridTracker, JTAlert (needs multicast)
- **JTDX** instead of WSJT-X
- **Windows 10**, older or low-power PCs, Raspberry Pi (from source)
