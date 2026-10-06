# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://mozilla.org/MPL/2.0/.
"""CC BY 4.0 license and attribution in FLAC tags and chunk metadata."""

import json
from pathlib import Path

import pytest
import soundfile as sf

from signal_archive_recorder.audio.flac_recovery import recover_partials
from signal_archive_recorder.audio.flac_writer import FlacWriter
from signal_archive_recorder.audio.format import AudioFormat
from signal_archive_recorder.metadata import rights
from signal_archive_recorder.metadata.builder import MetadataBuilder
from signal_archive_recorder.metadata.settings import StationSettings
from tests.fakes.fake_audio import noise
from tests.fakes.session_rig import REGISTRY, Rig, utc_ns

FMT = AudioFormat(8_000, 1, "int16")
SHARED = StationSettings(callsign="W9XYZ", share_callsign=True)
HIDDEN = StationSettings(callsign="W9XYZ")


def tags_for(settings: StationSettings) -> dict[str, str]:
    return rights.flac_tags(
        settings,
        session_id="20261005T120307Z",
        chunk_id="0000_20261005T120307Z",
        first_sample_ns=utc_ns("12:03:07"),
    )


def write(path: Path, tags: dict[str, str] | None, data: bytes):  # type: ignore[no-untyped-def]
    writer = FlacWriter(path, FMT, tags=tags)
    writer.write(data)
    return writer.close()


def test_tags_written_and_audio_untouched(tmp_path: Path) -> None:
    data = noise(FMT, 2.0)
    tagged = write(tmp_path / "a.flac", tags_for(SHARED), data)
    plain = write(tmp_path / "b.flac", None, data)
    assert tagged.verified and plain.verified
    assert tagged.pcm_md5 == plain.pcm_md5  # tags never touch the audio
    assert tagged.sha256 != plain.sha256  # ...but are covered by the file hash
    with sf.SoundFile(tmp_path / "a.flac") as f:
        assert f.license == "CC-BY-4.0 https://creativecommons.org/licenses/by/4.0/"
        assert f.artist == "W9XYZ"
        assert f.copyright == "© 2026 W9XYZ, licensed CC BY 4.0"
        assert f.title == "0000_20261005T120307Z"
        assert f.date == "2026-10-05T12:03:07Z"
        assert "attribution" in f.comment


def test_callsign_only_when_shared() -> None:
    hidden = tags_for(HIDDEN)
    assert "W9XYZ" not in json.dumps(hidden)
    assert hidden["artist"] == "Signal Archive Project contributor"
    assert rights.rights(HIDDEN)["attribution"] == "Signal Archive Project contributor"
    assert rights.rights(SHARED) == {
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "W9XYZ",
    }


def test_unsupported_tag_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isn't supported"):
        FlacWriter(tmp_path / "x.flac", FMT, tags={"hostname": "nope"})


def test_session_chunks_carry_rights(tmp_path: Path) -> None:
    rig = Rig(tmp_path, seconds=20, builder=MetadataBuilder(REGISTRY, SHARED))
    [chunk] = rig.finish()
    assert chunk["rights"]["attribution"] == "W9XYZ"
    with sf.SoundFile(rig.session.recordings / chunk["audio"]["flac"]["file"]) as f:
        assert f.artist == "W9XYZ" and f.license.startswith("CC-BY-4.0")
        assert f.title == chunk["chunk_id"]


def test_recovery_keeps_original_credit(tmp_path: Path) -> None:
    done = write(tmp_path / "chunk.flac", tags_for(SHARED), noise(FMT, 3.0))
    cut = done.path.read_bytes()
    done.path.unlink()
    (tmp_path / "chunk.flac.partial").write_bytes(cut[: len(cut) * 6 // 10])
    [recovery] = recover_partials(tmp_path)
    assert recovery.result is not None
    with sf.SoundFile(recovery.result.path) as f:
        assert f.artist == "W9XYZ"
        assert f.license.startswith("CC-BY-4.0")
        assert f.software.count("libsndfile") <= 1  # no pile-up across re-encodes
