# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
from pathlib import Path

import pytest

from signal_archive_recorder.config import ConfigError, load_config, parse_config

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "signal_archive_recorder"
    / "data"
    / "recorder.example.toml"
)


def test_example_config_parses() -> None:
    config = load_config(EXAMPLE)
    assert config.audio.device == "USB Audio CODEC"
    assert config.audio.sample_format == "int24"
    assert config.station.share_callsign is False
    assert config.station.grid_precision == 4
    assert config.wsjtx.port == 2237 and config.wsjtx.group is None


def test_relative_paths_and_station(tmp_path: Path) -> None:
    config = parse_config(
        {
            "storage": {"root": "archive"},
            "audio": {"file": "ref.wav", "sample_format": "int16"},
            "station": {"callsign": "w9xyz", "share_callsign": True, "grid": "en52wa"},
        },
        base=tmp_path,
    )
    assert config.storage_root == tmp_path / "archive"
    assert config.audio.file == tmp_path / "ref.wav"
    assert (config.station.callsign, config.station.grid) == ("W9XYZ", "EN52wa")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"audio": {"device": "x"}, "bogus": {}}, "unknown section"),
        ({"audio": {"device": "x", "colour": "red"}}, "unknown key"),
        ({"audio": {}}, "needs a device"),
        ({"audio": {"device": "x", "sample_format": "float32"}}, "int16 or int24"),
        ({"audio": {"device": "x"}, "station": {"grid_precision": 5}}, "precision"),
        ({"audio": {"device": "x"}, "station": {"grid": "ZZ99"}}, "grid"),
    ],
)
def test_config_errors(data: dict, message: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_bad_toml(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[audio\n")
    with pytest.raises(ConfigError, match=r"bad\.toml"):
        load_config(path)
