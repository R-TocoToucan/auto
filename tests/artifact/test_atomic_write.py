"""Tests for :func:`bithumb_bot.artifact.canonical.atomic_write` (Finding 9).

Two behavioral properties that MUST hold:

1. The temp file lives in ``target.parent`` (same-filesystem correctness
   on Windows — a cross-drive ``os.replace`` raises ``OSError`` on NT).
2. The write is atomic — either the target contains the full new bytes
   or the operation raised an exception that leaves the previous file
   intact.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import atomic_write, write_with_sidecar


class TestAtomicWrite:
    def test_writes_bytes(self, tmp_path: Path) -> None:
        target = tmp_path / "snapshot.json"
        atomic_write(target, b'{"ok":true}\n')
        assert target.read_bytes() == b'{"ok":true}\n'

    def test_temp_file_in_target_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The temp file MUST land in ``target.parent`` (Finding 9)."""
        recorded_paths: list[Path] = []
        original_write_bytes = Path.write_bytes

        def spy(self: Path, data: bytes) -> int:
            recorded_paths.append(self)
            return original_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", spy)
        target = tmp_path / "sub" / "snapshot.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, b"x")
        # The very first write_bytes call is the temp file we care about
        # (any subsequent test-scaffolding write_bytes calls would still
        # be inspected too).
        assert recorded_paths, "expected write_bytes to be recorded"
        tmp = recorded_paths[0]
        assert tmp.parent == target.parent, (
            f"temp file parent {tmp.parent!r} must equal target parent "
            f"{target.parent!r} (Windows same-filesystem correctness)"
        )
        # Convention: `.tmp-<pid>` suffix.
        assert tmp.name.startswith(target.name + ".tmp-"), (
            f"temp file name {tmp.name!r} must be `<target>.tmp-<pid>`"
        )

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        """``atomic_write`` (without ``guard_against_overwrite``) replaces."""
        target = tmp_path / "snapshot.json"
        target.write_bytes(b"old")
        atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    def test_raises_when_parent_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "missing_dir" / "snapshot.json"
        with pytest.raises((FileNotFoundError, OSError)):
            atomic_write(target, b"x")

    def test_temp_file_cleaned_up_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "snapshot.json"
        atomic_write(target, b"x")
        # No leftover *.tmp-* file after a clean write.
        stragglers = [p for p in tmp_path.iterdir() if p.name.startswith("snapshot.json.tmp-")]
        assert stragglers == []


class TestWriteWithSidecar:
    def test_writes_data_and_sidecar(self, tmp_path: Path) -> None:
        target = tmp_path / "snapshot.json"
        got_target, got_sidecar = write_with_sidecar(target, b'{"a":1}\n')
        assert got_target == target
        assert got_sidecar == tmp_path / "snapshot.json.sha256"
        # Sidecar content is `<hex>  <name>\n`.
        text = got_sidecar.read_text(encoding="utf-8")
        assert text.endswith("  snapshot.json\n")
        assert len(text.split()[0]) == 64

    def test_sidecar_verifies_via_sha256sum_convention(self, tmp_path: Path) -> None:
        """The recorded hex + filename MUST be recomputable + matching."""
        import hashlib

        target = tmp_path / "snapshot.json"
        payload = b'{"x":42}\n'
        _, sidecar = write_with_sidecar(target, payload)
        recorded_hex = sidecar.read_text(encoding="utf-8").split()[0]
        expected_hex = hashlib.sha256(payload).hexdigest()
        assert recorded_hex == expected_hex

    def test_sidecar_name_only_no_absolute_path(self, tmp_path: Path) -> None:
        """The sidecar's filename column MUST be `target.name`, not absolute.

        Otherwise `sha256sum --check <sidecar>` from ``target.parent``
        would fail (POSIX convention: filename column is relative to the
        working directory).
        """
        target = tmp_path / "sub" / "deeply" / "nested" / "snapshot.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        _, sidecar = write_with_sidecar(target, b"x")
        text = sidecar.read_text(encoding="utf-8")
        assert text.split()[1] == "snapshot.json"
        # And definitely NOT the absolute path.
        assert os.fspath(target) not in text
