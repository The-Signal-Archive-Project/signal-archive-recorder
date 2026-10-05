# SPDX-License-Identifier: Apache-2.0
"""Real-hardware tests live here and run only with --hardware."""

import pytest


@pytest.mark.hardware
def test_hardware_marker_is_skipped_by_default() -> None:
    """Placeholder until Stage 2 adds real capture tests."""
