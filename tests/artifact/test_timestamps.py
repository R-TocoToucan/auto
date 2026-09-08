"""Tests for :mod:`bithumb_bot.artifact.timestamps` (D-74 Windows-safe)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from bithumb_bot.artifact import timestamps


class TestUtcTimestamp:
    def test_matches_expected_regex(self) -> None:
        got = timestamps.utc_timestamp()
        assert re.fullmatch(r"\d{8}T\d{6}Z", got), got

    def test_no_colons(self) -> None:
        # D-74: colons are illegal in Windows filenames.
        assert ":" not in timestamps.utc_timestamp()

    def test_length_16(self) -> None:
        # YYYYMMDD (8) + T (1) + HHMMSS (6) + Z (1) = 16.
        assert len(timestamps.utc_timestamp()) == 16

    def test_uses_utc_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        frozen = datetime(2026, 9, 8, 1, 23, 45, tzinfo=UTC)
        monkeypatch.setattr(timestamps, "utc_now", lambda: frozen)
        assert timestamps.utc_timestamp() == "20260908T012345Z"


class TestUtcNow:
    def test_is_timezone_aware_utc(self) -> None:
        now = timestamps.utc_now()
        assert now.tzinfo is not None
        # UTC (offset 0).
        assert now.utcoffset() is not None
        assert now.utcoffset().total_seconds() == 0.0  # type: ignore[union-attr]

    def test_is_recent(self) -> None:
        # Sanity: don't drift more than a few seconds from wall clock.
        now = timestamps.utc_now()
        assert (datetime.now(UTC) - now).total_seconds() < 5.0
