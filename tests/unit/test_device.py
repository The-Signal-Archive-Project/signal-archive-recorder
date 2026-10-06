# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
import pytest

from signal_archive_recorder.audio.device import DeviceInfo, DeviceUnavailableError, open_input
from tests.fakes.fake_audio import FakeBackend

USB_CODEC = DeviceInfo(
    index=3,
    name="USB Audio CODEC",
    host_api="Windows WASAPI",
    max_input_channels=2,
    default_sample_rate=48_000,
)


def _noop(*_: object) -> None:
    pass


def test_defaults_to_device_rate_without_warnings() -> None:
    backend = FakeBackend([USB_CODEC])
    opened = open_input(backend, USB_CODEC, _noop)
    assert opened.delivered.sample_rate == 48_000
    assert opened.delivered.channels == 2
    assert opened.delivered.sample_format == "int24"
    assert opened.warnings == []


def test_delivered_format_recorded() -> None:
    backend = FakeBackend([USB_CODEC], delivered_rate=44_100)
    opened = open_input(backend, USB_CODEC, _noop, sample_rate=48_000)
    assert opened.requested.sample_rate == 48_000
    assert opened.delivered.sample_rate == 44_100  # metadata records what arrived
    assert [w.code for w in opened.warnings] == ["delivered_rate_differs"]
    assert "44100 Hz" in opened.warnings[0].message


def test_non_native_rate_warns_about_os_resampling() -> None:
    backend = FakeBackend([USB_CODEC])
    opened = open_input(backend, USB_CODEC, _noop, sample_rate=44_100)
    assert [w.code for w in opened.warnings] == ["os_resampling"]
    assert "bit-exact" in opened.warnings[0].message


def test_exclusive_only_warns() -> None:
    backend = FakeBackend([USB_CODEC], refuse_shared=True)
    with pytest.raises(DeviceUnavailableError) as err:
        open_input(backend, USB_CODEC, _noop)
    message = str(err.value)
    assert "USB Audio CODEC" in message
    assert "exclusive" in message
    assert "never takes exclusive control" in message
    assert len(backend.open_calls) == 1  # no retry, and the backend has no exclusive mode


def test_mono_device() -> None:
    mono = DeviceInfo(1, "Mic", "ALSA", max_input_channels=1, default_sample_rate=44_100)
    opened = open_input(FakeBackend([mono]), mono, _noop)
    assert opened.delivered.channels == 1
    assert opened.delivered.sample_rate == 44_100
