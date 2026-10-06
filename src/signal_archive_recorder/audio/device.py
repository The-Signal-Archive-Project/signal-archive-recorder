# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""Input devices: enumerate, open in shared mode only, and report the delivered format.

The recorder never takes exclusive control of a device. If shared mode isn't
possible, opening fails with a message for the operator; it is not retried in
exclusive mode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from signal_archive_recorder.audio.format import AudioFormat, SampleFormat
from signal_archive_recorder.core.events import CaptureWarning

# data, frames, overflow: called on the audio thread for every block.
InputCallback = Callable[[memoryview, int, bool], None]

SOURCE = "audio"


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    default_sample_rate: int


class InputStream(Protocol):
    @property
    def format(self) -> AudioFormat:
        """The format the stream actually delivers, which may differ from the request."""
        ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class DeviceOpenError(RuntimeError):
    """The backend couldn't open a device in shared mode.

    device_unavailable is True when the device exists but is held by another
    application or is only available exclusively.
    """

    def __init__(self, message: str, *, device_unavailable: bool = False) -> None:
        super().__init__(message)
        self.device_unavailable = device_unavailable


class DeviceUnavailableError(RuntimeError):
    """Shown to the operator: recording can't start on this device."""


class AudioBackend(Protocol):
    def input_devices(self) -> list[DeviceInfo]: ...

    def open_shared_input(
        self, device: DeviceInfo, fmt: AudioFormat, callback: InputCallback, blocksize: int
    ) -> InputStream:
        """Open in shared mode only. Raises DeviceOpenError on failure."""
        ...


@dataclass
class OpenedInput:
    stream: InputStream
    requested: AudioFormat
    delivered: AudioFormat
    warnings: list[CaptureWarning] = field(default_factory=list)


def open_input(
    backend: AudioBackend,
    device: DeviceInfo,
    callback: InputCallback,
    *,
    sample_format: SampleFormat = "int24",
    channels: int | None = None,
    sample_rate: int | None = None,
    blocksize: int = 0,
) -> OpenedInput:
    """Open a device for recording, preferring its own rate to avoid OS resampling.

    Returns the stream, the delivered format (always the one to record in metadata)
    and any warnings for the operator.
    """
    requested = AudioFormat(
        sample_rate=sample_rate or device.default_sample_rate,
        channels=channels or min(device.max_input_channels, 2),
        sample_format=sample_format,
    )
    try:
        stream = backend.open_shared_input(device, requested, callback, blocksize)
    except DeviceOpenError as exc:
        if exc.device_unavailable:
            raise DeviceUnavailableError(
                f'"{device.name}" can\'t be opened in shared mode. Another program may have '
                "exclusive control of it. Close that program, or turn off its exclusive-mode "
                "setting (on Windows: Sound settings > device Properties > Advanced > "
                '"Allow applications to take exclusive control"), then try again. '
                "The recorder never takes exclusive control itself."
            ) from exc
        raise DeviceUnavailableError(f'Couldn\'t open "{device.name}": {exc}') from exc

    delivered = stream.format
    warnings: list[CaptureWarning] = []
    if requested.sample_rate != device.default_sample_rate:
        warnings.append(
            _resampling_warning(device, requested.sample_rate, device.default_sample_rate)
        )
    if delivered.sample_rate != requested.sample_rate:
        warnings.append(
            CaptureWarning(
                source=SOURCE,
                code="delivered_rate_differs",
                message=(
                    f"Asked for {requested.sample_rate} Hz but the device delivers "
                    f"{delivered.sample_rate} Hz; recording at {delivered.sample_rate} Hz."
                ),
            )
        )
    return OpenedInput(stream, requested, delivered, warnings)


def _resampling_warning(device: DeviceInfo, rate: int, native: int) -> CaptureWarning:
    return CaptureWarning(
        source=SOURCE,
        code="os_resampling",
        message=(
            f'"{device.name}" runs at {native} Hz; recording at {rate} Hz means the '
            f"operating system resamples the audio, so it is no longer bit-exact. "
            f"Use {native} Hz, or change the device's default format to {rate} Hz."
        ),
    )


def _portaudio_help() -> str:
    import platform

    if platform.system() == "Linux":
        return (
            "The PortAudio library isn't installed. Debian/Ubuntu/Raspberry Pi OS: sudo apt "
            "install libportaudio2. Fedora: sudo dnf install portaudio. Arch: sudo pacman -S "
            "portaudio"
        )
    return "The PortAudio library couldn't be loaded; try reinstalling signal-archive-recorder"


class SoundDeviceBackend:
    """PortAudio via the sounddevice package. Imported lazily so tests don't need it."""

    def __init__(self) -> None:
        try:
            import sounddevice
        except OSError as exc:  # the PortAudio library itself is missing
            raise DeviceUnavailableError(f"{_portaudio_help()} ({exc})") from None

        self._sd: Any = sounddevice

    def input_devices(self) -> list[DeviceInfo]:
        sd = self._sd
        return [
            DeviceInfo(
                index=d["index"],
                name=d["name"],
                host_api=sd.query_hostapis(d["hostapi"])["name"],
                max_input_channels=d["max_input_channels"],
                default_sample_rate=round(d["default_samplerate"]),
            )
            for d in sd.query_devices()
            if d["max_input_channels"] > 0
        ]

    def open_shared_input(
        self, device: DeviceInfo, fmt: AudioFormat, callback: InputCallback, blocksize: int
    ) -> InputStream:
        sd = self._sd
        extra = sd.WasapiSettings(exclusive=False) if "WASAPI" in device.host_api else None

        def on_block(indata: Any, frames: int, _time: Any, status: Any) -> None:
            callback(memoryview(indata).cast("B"), frames, bool(status.input_overflow))

        try:
            raw = sd.RawInputStream(
                samplerate=fmt.sample_rate,
                blocksize=blocksize,
                device=device.index,
                channels=fmt.channels,
                dtype=fmt.sample_format,  # sounddevice uses the same names
                extra_settings=extra,
                callback=on_block,
                dither_off=True,  # PortAudio dithers format conversions by default
                clip_off=True,
            )
        except sd.PortAudioError as exc:
            unavailable = "unavailable" in str(exc).lower() or "busy" in str(exc).lower()
            raise DeviceOpenError(str(exc), device_unavailable=unavailable) from exc
        delivered = AudioFormat(
            sample_rate=round(raw.samplerate),
            channels=raw.channels,
            sample_format=fmt.sample_format,
        )
        return _SoundDeviceStream(raw, delivered)


class _SoundDeviceStream:
    def __init__(self, raw: Any, delivered: AudioFormat) -> None:
        self._raw = raw
        self._format = delivered

    @property
    def format(self) -> AudioFormat:
        return self._format

    def start(self) -> None:
        self._raw.start()

    def stop(self) -> None:
        self._raw.stop()

    def close(self) -> None:
        self._raw.close()
