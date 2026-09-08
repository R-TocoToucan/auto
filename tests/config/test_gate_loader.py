"""Tests for `bithumb_bot.config.gate_loader.load_gate1`.

Covers Behavior contract from PLAN task 01-01-05:
- Returns tuple[Gate1Decisions, hex_sha256_string]
- Uses binary-mode read (D-58 gotcha)
- Hash byte-stability across repeated calls
- Hash byte-sensitivity to single-byte mutation
- Missing file → Gate1LoadError (with path context)
- Malformed TOML → Gate1LoadError (wraps tomllib.TOMLDecodeError)
- status != "frozen" → Gate1LoadError (D-60)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.config.gate1_model import Gate1Decisions
from bithumb_bot.config.gate_loader import load_gate1
from bithumb_bot.errors import Gate1LoadError


class TestLoadGate1Positive:
    def test_returns_model_and_hex_hash(self, tmp_gate1_toml: Path) -> None:
        gate1, sha256_hex = load_gate1(tmp_gate1_toml)
        assert isinstance(gate1, Gate1Decisions)
        assert isinstance(sha256_hex, str)
        assert len(sha256_hex) == 64
        assert all(c in "0123456789abcdef" for c in sha256_hex)

    def test_model_is_frozen(self, tmp_gate1_toml: Path) -> None:
        gate1, _ = load_gate1(tmp_gate1_toml)
        assert gate1.model_config.get("frozen") is True

    def test_hash_stable_across_calls(self, tmp_gate1_toml: Path) -> None:
        _, sha1 = load_gate1(tmp_gate1_toml)
        _, sha2 = load_gate1(tmp_gate1_toml)
        assert sha1 == sha2

    def test_committed_gate1_toml_loads(self) -> None:
        """The real, committed config/decisions/gate1.toml must load."""
        # Repo root: parent of tests/ directory.
        repo_root = Path(__file__).parent.parent.parent
        target = repo_root / "config" / "decisions" / "gate1.toml"
        assert target.is_file(), f"committed gate1.toml missing at {target}"
        gate1, sha256_hex = load_gate1(target)
        assert gate1.status == "frozen"
        assert len(sha256_hex) == 64


class TestLoadGate1HashSensitivity:
    def test_hash_changes_on_single_byte_mutation(
        self, tmp_gate1_toml: Path
    ) -> None:
        _, sha_before = load_gate1(tmp_gate1_toml)
        raw = tmp_gate1_toml.read_bytes()
        # Flip a single byte in the source_commit hex value (does not
        # break TOML syntax or pydantic validation).
        assert b"source_commit = \"0000" in raw
        mutated = raw.replace(
            b"source_commit = \"0000",
            b"source_commit = \"1000",
            1,
        )
        assert mutated != raw
        tmp_gate1_toml.write_bytes(mutated)
        _, sha_after = load_gate1(tmp_gate1_toml)
        assert sha_before != sha_after


class TestLoadGate1Negative:
    def test_missing_file_raises_gate1_load_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.toml"
        with pytest.raises(Gate1LoadError) as excinfo:
            load_gate1(missing)
        # Path context in the error message.
        assert "nope.toml" in str(excinfo.value)

    def test_malformed_toml_raises_gate1_load_error(
        self, tmp_gate1_toml: Path
    ) -> None:
        tmp_gate1_toml.write_text("this is [ not valid toml", encoding="utf-8")
        with pytest.raises(Gate1LoadError) as excinfo:
            load_gate1(tmp_gate1_toml)
        # The wrapped tomllib error should surface in the message.
        assert "toml" in str(excinfo.value).lower()

    def test_unfrozen_status_raises_gate1_load_error(
        self, tmp_gate1_toml: Path
    ) -> None:
        raw = tmp_gate1_toml.read_text(encoding="utf-8").replace(
            'status = "frozen"', 'status = "draft"'
        )
        tmp_gate1_toml.write_text(raw, encoding="utf-8")
        with pytest.raises(Gate1LoadError) as excinfo:
            load_gate1(tmp_gate1_toml)
        assert "frozen" in str(excinfo.value).lower() or "status" in str(
            excinfo.value
        ).lower()

    def test_extra_key_raises_via_wrapped_validation_error(
        self, tmp_gate1_toml: Path
    ) -> None:
        raw = tmp_gate1_toml.read_text(encoding="utf-8") + '\nunexpected = "x"\n'
        tmp_gate1_toml.write_text(raw, encoding="utf-8")
        with pytest.raises(Gate1LoadError):
            load_gate1(tmp_gate1_toml)

    def test_unquoted_decimal_raises_via_wrapped_validation_error(
        self, tmp_gate1_toml: Path
    ) -> None:
        raw = tmp_gate1_toml.read_text(encoding="utf-8").replace(
            'provisional_engineering_notional_krw = "100000"',
            "provisional_engineering_notional_krw = 100000",
        )
        tmp_gate1_toml.write_text(raw, encoding="utf-8")
        with pytest.raises(Gate1LoadError):
            load_gate1(tmp_gate1_toml)
