# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Which audio inputs are probably the radio, ranked with rules for each OS.

Radio interfaces nearly always show up as a USB codec (built into most modern rigs,
SignaLink, Digirig and similar), a CM108-style USB sound device, or a virtual
receiver output (Flex DAX, SDR software). The rules also prefer ways of opening a
device that share it with WSJT-X and keep the audio exact, and push down inputs
that are plainly something else (laptop microphones, webcams, Stereo Mix).
"""

from __future__ import annotations

from dataclasses import dataclass

from signal_archive_recorder.audio.device import DeviceInfo

RADIO_HINTS = ("usb audio codec", "codec", "signalink", "digirig", "dax", "rig", "radio",
               "transceiver", "ic-", "ft-", "ts-", "flex", "sdr", "cm108", "usb pnp sound",
               "usb audio device", "usb sound")  # fmt: skip
NOT_RADIO_HINTS = ("microphone array", "mic array", "stereo mix", "webcam", "camera", "headset",
                   "airpods", "bluetooth", "hands-free", "macbook", "built-in microphone",
                   "internal mic")  # fmt: skip
VIRTUAL_HINTS = ("blackhole", "loopback", "cable output", "vb-audio", "voicemeeter", "monitor of")
LINUX_SERVERS = ("pipewire", "pulse", "default")
LINUX_PLUGINS = ("sysdefault", "lavrate", "samplerate", "speexrate", "upmix", "vdownmix",
                 "dmix", "dsnoop", "jack", "speex", "front", "surround", "iec958", "spdif",
                 "hdmi")  # fmt: skip


@dataclass(frozen=True)
class Candidate:
    device: DeviceInfo
    score: int
    reasons: tuple[str, ...]

    @property
    def recommended(self) -> bool:
        return self.score > 0


def rank(devices: list[DeviceInfo], system: str) -> list[Candidate]:
    """All input devices, best first; hidden ALSA plugins are left out on Linux."""
    out = []
    for d in devices:
        if d.max_input_channels < 1:
            continue
        name, api = d.name.lower(), d.host_api.lower()
        score, reasons = 0, []
        if system == "Linux" and name.split(":")[0].strip() in LINUX_PLUGINS:
            continue  # ALSA plumbing, not something to record from
        if any(h in name for h in RADIO_HINTS):
            score += 50
            reasons.append("looks like a radio interface")
        if "line in" in name or "line-in" in name:
            score += 10
            reasons.append("line input: fine if the radio is wired to it")
        if any(h in name for h in NOT_RADIO_HINTS):
            score -= 40
            reasons.append("probably a microphone, not the radio")
        if any(h in name for h in VIRTUAL_HINTS):
            reasons.append("virtual input (fine for SDR software)")
        if system == "Windows":
            if "wasapi" in api:
                score += 20
                reasons.append("WASAPI: shares the device and keeps the exact format")
            elif "wdm-ks" in api:
                score -= 30
                reasons.append("WDM-KS: can take the device away from WSJT-X")
            elif "mme" in api or "directsound" in api:
                score -= 10
                reasons.append(f"{d.host_api}: may convert the audio; prefer the WASAPI entry")
        elif system == "Linux":
            if name in LINUX_SERVERS or "pulseaudio" in api:
                score += 25
                reasons.append("through the sound server: shares the device with WSJT-X")
            elif "(hw:" in name or name.startswith("hw:"):
                score -= 20
                reasons.append("raw ALSA device: may lock the card so WSJT-X can't use it")
        out.append(Candidate(d, score, tuple(reasons)))
    return sorted(out, key=lambda c: (-c.score, c.device.name.lower()))


LINUX_TIP = (
    'Tip for Linux: "pipewire" records whatever your sound server\'s default input is. '
    "Make your radio's USB sound card the default input (e.g. in pavucontrol, Input "
    "Devices), so the recorder and WSJT-X can both use it at the same time."
)
