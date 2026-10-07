# Beta testing Signal Archive Recorder

Thank you for helping! Signal Archive Recorder records exactly what your receiver hears while WSJT-X is running and, with your consent, contributes it to an open dataset of real radio signals, for building and testing better decoders and detectors. Before we invite everyone, we need to know it works on many different stations.

**You'll need:** a computer running Windows 10/11, Debian 12+, Ubuntu 22.04+ or Arch Linux; WSJT-X (or JTDX) set up for FT8 or FT4; and about 15 minutes. A free [Hugging Face](https://huggingface.co) account is needed for uploading; setup explains how to make one.

**During the beta, your recordings go to a test dataset** (`signal-archive-intake-test`). It proves that uploading works, but it may be wiped, so beta recordings won't necessarily end up in the final archive. Once the full release is out, the recorder will tell you, and recordings will go to the real archive from then on.

## 1. Install

Get the newest beta from the [releases page](https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases) (it's marked **Pre-release**).

- **Windows:** download `SignalArchiveRecorder-…-Setup.exe` and run it. The beta isn't code-signed yet, so Windows may say "Windows protected your PC": choose **More info → Run anyway**. If your antivirus objects, please tell us which antivirus it is.
- **Debian / Ubuntu:** download the `.deb` file, then run `sudo apt install ./signal-archive-recorder_*_amd64.deb` in the folder you saved it to. Start it from your applications menu.
- **Arch Linux:** `yay -S signal-archive-recorder`, or use another AUR helper.

## 2. Set it up

The first start opens a setup window:
1. **The contribution terms.** Please read them: they explain what's shared and the CC BY 4.0 license.
2. **Hugging Face login.** Make a **Write** token at <https://huggingface.co/settings/tokens> and paste it in. The token is kept in your system's keyring, never in a file.
3. **Audio input.** Choose your radio's receive audio. The recommended inputs are listed first. Press **Test level** with the radio receiving. If your interface sends the same audio on both stereo channels, it suggests recording just one, which loses nothing and halves the size.
4. **Your station** (optional). Choose whether to share your callsign and how much of your grid to share.
5. **WSJT-X and clock checks.** Start WSJT-X first if you can.

After that the recorder lives in the **system tray**. Its icon colour tells you what it's doing:

| Colour | Meaning |
|---|---|
| 🔵 blue | ready: waiting for WSJT-X, sound card closed, nothing recorded |
| 🟢 green | recording, all good |
| 🟡 yellow | recording, but something needs a look (click the icon for details) |
| 🔴 red | can't record (the window says why) |
| ⚪ grey | paused by you |

## 3. Use it as you normally would

Run WSJT-X as usual for an evening or two: change bands, call CQ, answer stations. The recorder starts recording when WSJT-X starts and saves the session when WSJT-X closes. You don't need to do anything.

Things worth trying, if you have time:
- **Review & upload** (in the window): check what would be shared, then upload a session.
- Close WSJT-X and reopen it later: you should get a new session each time.
- If you use **GridTracker** or **JTAlert**: do they and the recorder both still get WSJT-X's data? (If not, the window explains how to share it using multicast.)
- Restart your computer: does the recorder come back on its own, in standby?

## 4. Tell us how it went

Reports that it **worked** are just as useful as reports of problems: we're building up a picture of which radios, sound interfaces and computers are covered.

- **[Send a test report](https://github.com/The-Signal-Archive-Project/signal-archive-recorder/issues/new?template=test-report.yml)** (a short form on GitHub).
- **No GitHub account?** Post to the group at <https://groups.io/g/signal-archive> (you can also email `main@signal-archive.groups.io`), and we'll take it from there.
- **Quick questions or a chat:** our Discord. The join link is in the [README](https://github.com/The-Signal-Archive-Project/signal-archive-recorder#community).

If something went wrong, choose **Report a problem…** in the tray menu or the window. It saves a **diagnostics file** and opens these links. The file has no audio and no passwords, and your computer's name, user name, callsign and grid are blanked out. Open the zip and check it if you like, then attach it to your report.

## Known issues in the beta

- **Windows SmartScreen warning:** the installer isn't code-signed yet (see Install above).
- **Linux needs a keyring** to remember your Hugging Face login: GNOME Keyring, KWallet or KeePassXC. Most desktops have one.
- **Only one program can receive WSJT-X's data** on the default setting. If GridTracker or JTAlert already uses it, switch WSJT-X to multicast (the recorder's window explains how).
- **macOS** isn't packaged yet: it works from source with `pipx`, but hasn't been tested.

## Uninstalling

- **Windows:** Settings → Apps → Signal Archive Recorder → Uninstall. It keeps your recordings and settings unless you tick **Remove everything** (which warns you first).
- **Debian / Ubuntu:** `sudo apt remove signal-archive-recorder`. **Arch:** `sudo pacman -R signal-archive-recorder`.
- On Linux, to also delete recordings and settings, run `signal-archive-recorder forget --everything` *before* uninstalling.

Thank you, and 73!
