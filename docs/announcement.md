# Announcement texts

Ready-to-post texts for the beta. Swap in the newest version and links before posting. For a new beta, update the version in the subject and heading; the links all point at the latest release, so they stay correct.

## groups.io: beta announcement (posted for 0.3.0-beta.2)

Post from the group's web page or by email to `main@signal-archive.groups.io`, then make it Sticky so new members see it first. Paste the quoted text without the leading `> `.

**Subject:** Beta testers wanted: Signal Archive Recorder 0.3.0-beta.2

> Hello everyone, and welcome to the Signal Archive group!
>
> I'm Alistair, KQ4YDE, an undergraduate aerospace engineering student in Eastern Kentucky. I've been into ham radio for a while, I'm on the development team for the CXBN-3 satellite, and I love working with digital RF signals and writing code. This project is where those meet, and I'd love your help with it.
>
> **Why this project exists**
>
> The decoders we rely on for FT8, FT4 and the other digital modes were built and tuned without a large, shared collection of real signals. Weak signals at the noise floor, fading, QRM and crowded bands, as they really arrive at stations around the world, mostly go unrecorded. The Signal Archive Project is building an open library of exactly that audio, free for anyone to use for research and for building better decoders and detectors.
>
> **What Signal Archive Recorder does**
>
> It's a small, free, open-source program that runs beside WSJT-X:
>
> - It records exactly what your receiver hears, and only while WSJT-X is running. The rest of the time it waits quietly in the system tray.
> - It labels each recording with the band, mode and timing WSJT-X reports.
> - It never transmits and never controls your radio.
> - You review what would be shared before anything is uploaded. Sharing your callsign is your choice, and your grid is shared to 4 characters by default.
>
> **Why we need you now**
>
> Before the full launch, we want to know it works on as many different stations as possible: different radios, sound interfaces (rig USB, SignaLink, Digirig…), computers and WSJT-X setups. Reports that "it just worked" are every bit as useful as bug reports.
>
> **How to take part**
>
> 1. Read the tester guide (about 15 minutes to get going):
>    https://github.com/The-Signal-Archive-Project/signal-archive-recorder/blob/main/TESTING.md
>
> 2. Install the beta:
>    - Windows: download the installer from https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases/latest. The beta isn't code-signed yet, so if Windows says "Windows protected your PC", choose More info, then Run anyway.
>    - Debian 12+ / Ubuntu 22.04+: add our package repository's beta channel, so new betas arrive with your normal updates. The steps are at https://the-signal-archive-project.github.io/signal-archive-recorder/
>    - Arch Linux: the AUR package is waiting for AUR registrations to reopen. Until then, the tester guide has a pipx command.
>
> 3. Run WSJT-X as you normally would for an evening or two. The recorder starts and stops with WSJT-X by itself.
>
> 4. Tell us how it went:
>    - With a GitHub account: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/issues/new?template=test-report.yml
>    - Without one: just reply here, or post to the group. Please include your OS, radio, sound interface and WSJT-X version.
>
> If something goes wrong, choose "Report a problem…" from the recorder's tray menu. It saves a diagnostics file containing no audio and no passwords, with your computer name, user name, callsign and grid blanked out. Attach it to your report.
>
> **Good to know**
>
> During the beta, recordings go to a test dataset that may be wiped, so beta uploads won't necessarily end up in the final archive. When the full release is out, the recorder will tell you to update, and from then on recordings go to the real archive. Recordings are shared under CC BY 4.0, with credit to your station if you wish.
>
> For quick questions and chat, there's also our Discord: https://discord.gg/R8g5F8BcAC
>
> Thank you for helping!
>
> 73,
> Alistair, KQ4YDE

## Discord: beta announcement for #announcements (posted for 0.3.0-beta.2)

Discord allows 2,000 characters per message; this is about 1,740. The `<…>` around links stops Discord adding a preview card under each one. Post it in `#announcements`, pin it, and click **Publish** if the channel is an Announcement channel.

```
# 📻 Beta testers wanted: Signal Archive Recorder 0.3.0-beta.2
👋 I'm Alistair (KQ4YDE), an aerospace undergrad in Eastern KY on the CXBN-3 satellite team, and this is my project.

Better decoders for our digital modes need **real recordings**: weak signals in the noise, fading, QRM, crowded bands. Signal Archive Recorder runs beside WSJT-X and records exactly what your receiver hears, for an **open dataset** anyone can use to build better decoders. It's free and open source, never transmits, and never touches your radio.

**Before the full launch, we need it tested on as many stations as possible.** Any radio, any sound interface, FT8 or FT4.

**🧰 Get it**
🪟 **Windows:** installer on the release page (if SmartScreen complains: *More info → Run anyway*)
🐧 **Debian / Ubuntu:** add the **beta** channel: <https://the-signal-archive-project.github.io/signal-archive-recorder/>
🏔️ **Arch:** pipx for now (the AUR is closed to new accounts); the command is in the guide
➡️ Release: <https://github.com/The-Signal-Archive-Project/signal-archive-recorder/releases/latest>

**🚀 Then**
1. Follow the **tester guide** (about 15 min): <https://github.com/The-Signal-Archive-Project/signal-archive-recorder/blob/main/TESTING.md>
2. Run WSJT-X as normal for an evening or two. The recorder only records while WSJT-X is open.
3. **Tell us how it went.** "It worked!" is as useful as a bug: <https://github.com/The-Signal-Archive-Project/signal-archive-recorder/issues/new?template=test-report.yml>

Something wrong? Tray menu → **Report a problem…** saves a diagnostics file (no audio, no passwords, names blanked out).

ℹ️ Beta recordings go to a **test dataset** that may be wiped. When the full release lands, the app tells you, and recordings go to the real archive.

Questions? Ask right here, or on <https://groups.io/g/signal-archive>. Thanks, and 73! — Alistair KQ4YDE 🙏
```

## Reddit (r/amateurradio and similar)

Post from a project account (e.g. `u/KQ4YDE`), not a personal one, and **message the subreddit's moderators first**: new accounts and project links are often held for review. Use Markdown mode. Don't vote or comment on the post from another account of yours.

**Title:** I'm an undergrad building an open dataset of real FT8/FT4 signals to train better decoders, looking for beta testers and developers

**Body:**

```markdown
Hi r/amateurradio! I'm Alistair (KQ4YDE), an undergraduate aerospace engineering student in Eastern Kentucky. I'm on the development team for the CXBN-3 satellite, and I spend a lot of my time on digital RF signals and code.

**The problem:** the decoders we all rely on were built and tuned without a large, shared collection of *real* signals. Weak signals at the noise floor, QSB, QRM and crowded bands, as they actually arrive at stations around the world, mostly go unrecorded. Without that data, it's hard to build better decoders or detectors, or even to compare them fairly.

**What I built:** [Signal Archive Recorder](https://github.com/The-Signal-Archive-Project/signal-archive-recorder), a free, open-source (MPL-2.0) program that runs beside WSJT-X:

- It records exactly what your receiver hears (bit-exact FLAC, no resampling), and **only while WSJT-X is running**. Otherwise it sits idle in the tray.
- It labels every recording with band, mode, timing and clock accuracy from WSJT-X and NTP.
- It never transmits and never controls your rig. It only listens to WSJT-X's UDP output and your audio.
- Nothing is uploaded until you've reviewed it. Your callsign is only shared if you choose, and it keeps back anything that doesn't look like receiver audio.
- With your consent, recordings go to an open dataset on Hugging Face (CC BY 4.0) that anyone can use for research.

## Looking for beta testers

It's in public beta (0.3.0-beta). I need it tested on as many different stations as possible: any radio, any sound interface (rig USB, SignaLink, Digirig…), Windows or Linux. There's a Windows installer, an apt repository for Debian/Ubuntu, and pipx for everything else. Setup takes about 15 minutes; after that you just run WSJT-X as normal.

- **Tester guide:** https://github.com/The-Signal-Archive-Project/signal-archive-recorder/blob/main/TESTING.md
- "It worked on my station" reports are just as valuable as bug reports. There's a short form, or you can post to the group.
- During the beta, uploads go to a *test* dataset, so you can try everything without worrying.

## Looking for developers

It's Python (PySide6 for the UI), with a large test suite and CI that builds and tests the Windows installer and the Linux packages. Places I'd love help:

- **More modes and sources:** adapters for fldigi, JS8Call, rigctld and flrig (read-only), so it can go beyond FT8/FT4
- **macOS packaging and testing:** it runs from source, but I don't have a Mac
- **Satellites:** Doppler and TLE metadata from Gpredict and similar, close to my heart from CubeSat work
- **Using the data:** if you're into DSP or ML, I'd love to hear what you'd want from the dataset for decoder and detector research

Contributor guide: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/blob/main/CONTRIBUTING.md

**Questions and chat:** groups.io (https://groups.io/g/signal-archive) or Discord (https://discord.gg/R8g5F8BcAC). I'm also happy to answer anything in the comments: questions, feedback and criticism all welcome.

73, Alistair KQ4YDE
```

**Answers worth having ready for the comments:**
- *Why not just use WSJT-X's own recordings?* WSJT-X can save WAV files, but they don't have the metadata, timing references or a shared, consented, openly licensed home. The archive also keeps the decoder's output separate from the raw audio, for fair comparisons.
- *Does it use WSJT-X's code?* No. It only reads WSJT-X's network messages, as GridTracker and JTAlert do, and contains no WSJT-X code.
- *Privacy?* The callsign is opt-in, the grid is shared to 4 characters by default, every upload is reviewed first, and diagnostics files have names and tokens removed.
- *Storage and bandwidth?* About 0.25 GB per hour of mono audio. Uploads can be capped, scheduled overnight, or left manual, and only confirmed uploads are ever deleted locally.

## Forums, reflectors and club newsletters

**Subject: Beta testers wanted: Signal Archive Recorder, an open library of real FT8/FT4 signals**

Better decoders and signal detectors for our digital modes depend on real recordings: weak signals at the noise floor, fading, QRM and crowded bands, as they really arrive at stations around the world. Today there's no large, open collection of that audio, and most development relies on synthetic signals or one developer's own station.

Signal Archive Recorder is a small, free, open-source program that runs beside WSJT-X. It records exactly what your receiver hears, but only while WSJT-X is running, and labels each recording with the band, mode and timing WSJT-X reports. With your consent, it contributes the recordings to the Signal Archive Project, an open dataset (CC BY 4.0) for anyone researching or building better decoders, with credit to your station if you wish. It never transmits, never controls your radio, and shows you exactly what will be shared before anything is uploaded.

We're looking for beta testers running WSJT-X or JTDX on Windows, Debian/Ubuntu or Arch Linux, with any radio and sound interface. Setup takes about 15 minutes, and after that it runs quietly in the system tray.

- Tester guide and downloads: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/blob/main/TESTING.md
- Questions: https://groups.io/g/signal-archive

73, and thank you!
Alistair, KQ4YDE (aerospace engineering undergrad, Eastern Kentucky; CXBN-3 satellite team)

## Short (Discord, social media, a club net)

Wanted: beta testers for Signal Archive Recorder 📻 It runs beside WSJT-X and records the real FT8/FT4 signals your station hears, for an open dataset to build better decoders. Free and open source; Windows, Debian/Ubuntu and Arch. Guide: https://github.com/The-Signal-Archive-Project/signal-archive-recorder/blob/main/TESTING.md

## Points to make in a talk or on a net

- **Why:** the decoders we use were built and tuned without a big shared collection of real signals. An open one lets anyone build and fairly compare better decoders and detectors.
- **What it does:** it records only while WSJT-X runs, never transmits or controls the radio, and keeps the audio bit-exact with the timing information.
- **Privacy:** sharing your callsign is your choice, your grid is shared at 4 characters by default, you review every upload, and nothing leaves your computer until you've agreed.
- **Credit:** recordings are CC BY 4.0, with attribution to the contributing station if you wish.
