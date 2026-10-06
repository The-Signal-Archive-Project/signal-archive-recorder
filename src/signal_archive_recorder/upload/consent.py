# SPDX-License-Identifier: Apache-2.0
"""What a contributor agrees to before anything is uploaded, and the record of it.

The text and license are versioned: when either changes, the stored acceptance no
longer counts and the contributor is asked again. Nothing is uploaded without a
current acceptance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from signal_archive_recorder.metadata.rights import LICENSE_ID, LICENSE_URL
from signal_archive_recorder.metadata.settings import Consent
from signal_archive_recorder.session.storage import write_json_atomic

CONSENT_VERSION = "1"

CONSENT_TEXT = f"""\
Signal Archive Project: contributing your recordings (consent version {CONSENT_VERSION})

The Signal Archive Project builds an open reference library of real radio signals
so that people can develop and test better decoders and detectors.

By contributing, you agree that:

1. Your recordings and the metadata shown in the review step are published openly
   on Hugging Face under the Creative Commons Attribution 4.0 International
   license ({LICENSE_ID}, {LICENSE_URL}). Anyone may download, use, change and
   share them, including commercially, as long as they credit the source.

2. Publication is public and long-lasting. Copies other people made before any
   removal can't be recalled, and released versions of the dataset may be given
   a permanent DOI.

3. The recordings are receive audio from your own station, and you have the right
   to contribute them.

4. Recordings capture public amateur radio transmissions, including other
   stations' callsigns as they were sent on air.

5. Your own callsign is published only if you chose to share it, and your location
   only at the grid precision you chose. You'll see exactly what is shared before
   each upload, and can remove any chunk.

6. You can stop contributing at any time, and ask for your sessions to be removed
   from the intake repository and from future dataset versions by opening a
   discussion on the repository.
"""


class NoConsentError(RuntimeError):
    """The contributor hasn't accepted the current consent text and license."""


@dataclass(frozen=True)
class ConsentRecord:
    accepted_ns: int
    license_id: str
    consent_version: str

    def is_current(self) -> bool:
        return self.license_id == LICENSE_ID and self.consent_version == CONSENT_VERSION

    def as_consent(self) -> Consent:
        return Consent(accepted_ns=self.accepted_ns, license_id=self.license_id)


class ConsentStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ConsentRecord | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text("utf-8"))
        return ConsentRecord(data["accepted_ns"], data["license_id"], data["consent_version"])

    def accept(self, now_ns: int) -> ConsentRecord:
        record = ConsentRecord(now_ns, LICENSE_ID, CONSENT_VERSION)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, record.__dict__)
        return record

    def withdraw(self) -> None:
        self.path.unlink(missing_ok=True)

    def require(self) -> ConsentRecord:
        record = self.load()
        if record is None:
            raise NoConsentError(
                "You haven't agreed to the contribution terms yet. "
                "Run: signal-archive-recorder consent"
            )
        if not record.is_current():
            raise NoConsentError(
                "The contribution terms have changed since you agreed to them. "
                "Please review them again: signal-archive-recorder consent"
            )
        return record
