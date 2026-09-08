"""Tests for :func:`bithumb_bot.artifact.canonical.guard_against_overwrite` (D-76 / T-1-04-09)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import (
    guard_against_overwrite,
    sha256_hex,
    sidecar_line,
    write_with_sidecar,
)
from bithumb_bot.errors import CriticalCorruptionAlert, SnapshotAlreadyConsumedError


class TestGuardAgainstOverwrite:
    def test_fresh_target_no_error(self, tmp_path: Path) -> None:
        """Missing target file → guard is a no-op (fresh write is allowed)."""
        target = tmp_path / "snapshot.json"
        sidecar = tmp_path / "snapshot.json.sha256"
        # Should NOT raise.
        guard_against_overwrite(target, sidecar)
        # Nothing was created either.
        assert not target.exists()
        assert not sidecar.exists()

    def test_matching_sidecar_raises_already_consumed(self, tmp_path: Path) -> None:
        """D-76: existing file with matching sidecar → SnapshotAlreadyConsumedError."""
        target = tmp_path / "snapshot.json"
        payload = b'{"x":1}\n'
        write_with_sidecar(target, payload)
        sidecar = target.with_name(target.name + ".sha256")
        with pytest.raises(SnapshotAlreadyConsumedError) as excinfo:
            guard_against_overwrite(target, sidecar)
        assert excinfo.value.target == target
        # The on-disk file MUST still be intact — the guard never touches it.
        assert target.read_bytes() == payload

    def test_mismatched_sidecar_renames_and_raises_corruption(
        self, tmp_path: Path
    ) -> None:
        """T-1-04-09 branch (b): mismatched sidecar → rename + CriticalCorruptionAlert."""
        target = tmp_path / "snapshot.json"
        sidecar = target.with_name(target.name + ".sha256")
        # Write a file whose bytes do NOT match the sidecar's recorded hex.
        target.write_bytes(b"tampered contents")
        wrong_hex = sha256_hex(b"different bytes entirely")
        sidecar.write_text(sidecar_line(wrong_hex, target.name), encoding="utf-8")
        with pytest.raises(CriticalCorruptionAlert) as excinfo:
            guard_against_overwrite(target, sidecar)
        # Corrupt path was moved out of the way (not silently overwritten).
        assert not target.exists()
        corrupt = Path(excinfo.value.corrupt_path)
        assert corrupt.exists()
        assert corrupt.parent == target.parent
        assert corrupt.name.startswith("_corrupt_")
        assert corrupt.name.endswith("_" + target.name)

    def test_missing_sidecar_treated_as_corruption(self, tmp_path: Path) -> None:
        """No sidecar → same quarantine + alert path (never silently trust)."""
        target = tmp_path / "snapshot.json"
        target.write_bytes(b"orphan file")
        sidecar = target.with_name(target.name + ".sha256")
        assert not sidecar.exists()
        with pytest.raises(CriticalCorruptionAlert):
            guard_against_overwrite(target, sidecar)
        assert not target.exists()  # renamed to _corrupt_<ts>_...
        stragglers = [p for p in tmp_path.iterdir() if p.name.startswith("_corrupt_")]
        assert len(stragglers) == 1

    def test_malformed_sidecar_treated_as_corruption(self, tmp_path: Path) -> None:
        """Sidecar exists but hex column is unparseable → quarantine + alert."""
        target = tmp_path / "snapshot.json"
        target.write_bytes(b"payload")
        sidecar = target.with_name(target.name + ".sha256")
        sidecar.write_text("this is not a valid sha256 line\n", encoding="utf-8")
        with pytest.raises(CriticalCorruptionAlert):
            guard_against_overwrite(target, sidecar)
        assert not target.exists()

    def test_never_overwrites_silently(self, tmp_path: Path) -> None:
        """Meta-property: the guard NEVER returns cleanly when target exists."""
        target = tmp_path / "snapshot.json"
        target.write_bytes(b"anything")
        sidecar = target.with_name(target.name + ".sha256")
        # Sidecar missing OR mismatched OR matching — any branch either
        # raises SnapshotAlreadyConsumed or CriticalCorruption. Return
        # without raising IS a bug.
        with pytest.raises((SnapshotAlreadyConsumedError, CriticalCorruptionAlert)):
            guard_against_overwrite(target, sidecar)
