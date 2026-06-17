"""Tests for the OpenSRE↔Radar contract-version handshake (OQ-4).

The check is intentionally soft: it warns on a major-version skew in a Radar
envelope and is silent otherwise. It must never raise — a version mismatch
should degrade to best-effort, not break an investigation.
"""

from __future__ import annotations

import logging

import pytest

from app.remote import server as remote_server


def _check(alert: dict, caplog: pytest.LogCaptureFixture) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=remote_server.logger.name):
        remote_server._check_contract_version(alert)
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_matching_major_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    assert (
        _check({"source": "radar", "contractVersion": remote_server.RADAR_CONTRACT_VERSION}, caplog)
        == []
    )


def test_additive_minor_patch_is_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    # Same major, newer minor/patch — additive, no warning.
    assert _check({"source": "radar", "contractVersion": "0.9.3"}, caplog) == []


def test_draft_suffix_is_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    assert _check({"source": "radar", "contractVersion": "0.1.0-draft"}, caplog) == []


def test_major_skew_warns(caplog: pytest.LogCaptureFixture) -> None:
    warnings = _check({"source": "radar", "contractVersion": "1.0.0"}, caplog)
    assert len(warnings) == 1
    assert "contractVersion" in warnings[0]


def test_non_radar_source_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    # A real upstream alert (Datadog/Grafana/etc.) never carries this field;
    # even a stray major-different value must not warn for non-radar sources.
    assert _check({"source": "datadog", "contractVersion": "9.9.9"}, caplog) == []


def test_missing_or_blank_version_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    assert _check({"source": "radar"}, caplog) == []
    assert _check({"source": "radar", "contractVersion": "  "}, caplog) == []
    assert _check({"source": "radar", "contractVersion": 1}, caplog) == []  # type: ignore[dict-item]
