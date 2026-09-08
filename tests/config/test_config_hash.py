"""Tests for `bithumb_bot.config.config_hash.build_config_hash_manifest`.

Covers Behavior contract from PLAN task 01-01-08:
- Returns (manifest_dict, hex_config_hash).
- manifest shape: {schema_version=1, gate1_sha256, gate2_sha256, gate3_sha256}.
- Canonical bytes: json sorted keys, compact separators, no ascii escape,
  trailing newline (D-76 / Finding 9).
- Byte-identical inputs → byte-identical output.
- Single-hex-char change in ANY gate → different hash.
- Phase-1 exercises with gate1-only inputs (gate2=None, gate3=None).
- Pure (no I/O); typed; mypy-strict clean.
"""

from __future__ import annotations

import hashlib
import json

from hypothesis import given
from hypothesis import strategies as st

from bithumb_bot.config.config_hash import (
    _canonical_bytes,
    build_config_hash_manifest,
)


HEX64 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


class TestBuildConfigHashManifestShape:
    def test_returns_tuple_of_dict_and_str(self) -> None:
        manifest, hex_hash = build_config_hash_manifest("a" * 64)
        assert isinstance(manifest, dict)
        assert isinstance(hex_hash, str)

    def test_manifest_has_expected_keys(self) -> None:
        manifest, _ = build_config_hash_manifest("a" * 64)
        assert manifest["schema_version"] == 1
        assert manifest["gate1_sha256"] == "a" * 64
        assert manifest["gate2_sha256"] is None
        assert manifest["gate3_sha256"] is None

    def test_hex_hash_is_64_lowercase_hex_chars(self) -> None:
        _, hex_hash = build_config_hash_manifest("a" * 64)
        assert len(hex_hash) == 64
        assert all(c in "0123456789abcdef" for c in hex_hash)

    def test_gate2_and_gate3_recorded_when_provided(self) -> None:
        manifest, _ = build_config_hash_manifest(
            gate1_sha256="a" * 64,
            gate2_sha256="b" * 64,
            gate3_sha256="c" * 64,
        )
        assert manifest["gate1_sha256"] == "a" * 64
        assert manifest["gate2_sha256"] == "b" * 64
        assert manifest["gate3_sha256"] == "c" * 64


class TestDeterminism:
    def test_byte_identical_inputs_yield_identical_hash(self) -> None:
        m1, h1 = build_config_hash_manifest("f" * 64)
        m2, h2 = build_config_hash_manifest("f" * 64)
        assert h1 == h2
        assert m1 == m2


class TestSensitivity:
    def test_single_hex_char_change_in_gate1_changes_hash(self) -> None:
        _, h_a = build_config_hash_manifest("a" * 64)
        _, h_b = build_config_hash_manifest("b" + "a" * 63)
        assert h_a != h_b

    def test_single_hex_char_change_in_gate2_changes_hash(self) -> None:
        _, h1 = build_config_hash_manifest("a" * 64, gate2_sha256="b" * 64)
        _, h2 = build_config_hash_manifest("a" * 64, gate2_sha256="c" + "b" * 63)
        assert h1 != h2

    def test_single_hex_char_change_in_gate3_changes_hash(self) -> None:
        _, h1 = build_config_hash_manifest(
            "a" * 64, gate2_sha256="b" * 64, gate3_sha256="c" * 64
        )
        _, h2 = build_config_hash_manifest(
            "a" * 64, gate2_sha256="b" * 64, gate3_sha256="d" + "c" * 63
        )
        assert h1 != h2


class TestCanonicalBytesFormat:
    def test_trailing_newline(self) -> None:
        raw = _canonical_bytes({"a": 1, "b": None})
        assert raw.endswith(b"\n")

    def test_sorted_keys(self) -> None:
        raw = _canonical_bytes({"b": 1, "a": 2})
        # Sorted keys → "a" appears before "b" in the raw output.
        assert raw.index(b'"a"') < raw.index(b'"b"')

    def test_compact_separators(self) -> None:
        raw = _canonical_bytes({"a": 1, "b": 2})
        # Compact separators: no spaces around `:` or `,`.
        assert b": " not in raw
        assert b", " not in raw

    def test_hash_matches_manual_recomputation(self) -> None:
        manifest, hex_hash = build_config_hash_manifest("z" * 64)
        expected = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
        assert hex_hash == expected


class TestPurityNoIO:
    def test_no_environment_dependence(self, monkeypatch) -> None:
        # Set random env vars and confirm the hash is unchanged (function
        # is pure — no os.environ reads).
        monkeypatch.setenv("SOMETHING", "else")
        _, h1 = build_config_hash_manifest("a" * 64)
        monkeypatch.delenv("SOMETHING", raising=False)
        _, h2 = build_config_hash_manifest("a" * 64)
        assert h1 == h2


class TestHypothesisPropertyDistinctInputsDistinctHashes:
    @given(a=HEX64, b=HEX64)
    def test_distinct_gate1_hashes_yield_distinct_config_hashes(
        self, a: str, b: str
    ) -> None:
        # Only meaningful when the inputs actually differ; hypothesis will
        # sometimes generate a == b, so skip those.
        if a == b:
            return
        _, ha = build_config_hash_manifest(a)
        _, hb = build_config_hash_manifest(b)
        assert ha != hb
