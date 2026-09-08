"""Tests for :mod:`bithumb_bot.artifact.canonical` — D-76 canonical JSON.

Every test flows through the public API only. Two invariants are the
whole point of this module:

1. **Byte-identical output** across calls with equivalent input. Two
   dicts that differ only in key order MUST serialize to identical
   bytes; a snapshot's SHA-256 sidecar is meaningless otherwise.
2. **Trailing newline** — POSIX ``sha256sum --check`` and cross-platform
   diff tools depend on it (D-76).
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex, sidecar_line


class TestCanonicalBytes:
    def test_key_order_independent(self) -> None:
        a = canonical_bytes({"b": 1, "a": 2})
        b = canonical_bytes({"a": 2, "b": 1})
        assert a == b

    def test_trailing_newline(self) -> None:
        assert canonical_bytes({"a": 1}).endswith(b"\n")

    def test_sorted_keys_compact(self) -> None:
        got = canonical_bytes({"b": 1, "a": 2})
        # No spaces; keys sorted alphabetically.
        assert got == b'{"a":2,"b":1}\n'

    def test_utf8_non_ascii(self) -> None:
        # ensure_ascii=False — Korean literal encodes as its UTF-8 bytes,
        # not \uXXXX escapes.
        got = canonical_bytes({"market": "KRW-비트"})
        assert "KRW-비트".encode("utf-8") in got

    def test_nested_dicts_sorted(self) -> None:
        got_1 = canonical_bytes({"outer": {"y": 1, "x": 2}})
        got_2 = canonical_bytes({"outer": {"x": 2, "y": 1}})
        assert got_1 == got_2
        assert b'"x":2,"y":1' in got_1

    def test_lists_order_preserved(self) -> None:
        # List order is significant (unlike dict key order) — the same list
        # produces the same bytes; a different order produces different bytes.
        a = canonical_bytes({"xs": [1, 2, 3]})
        b = canonical_bytes({"xs": [3, 2, 1]})
        assert a != b

    def test_stringified_decimal_survives(self) -> None:
        # Callers stringify Decimals BEFORE reaching canonical_bytes; verify
        # the string round-trip is faithful.
        got = canonical_bytes({"bid_fee": "0.0025"})
        assert b'"bid_fee":"0.0025"' in got

    @given(
        st.dictionaries(
            keys=st.text(
                alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E),
                min_size=1,
                max_size=8,
            ),
            values=st.integers(min_value=-1000, max_value=1000),
            max_size=8,
        )
    )
    def test_property_two_calls_equal(self, d: dict[str, int]) -> None:
        assert canonical_bytes(d) == canonical_bytes(d)

    @given(
        st.lists(
            st.tuples(
                st.text(
                    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E),
                    min_size=1,
                    max_size=6,
                ),
                st.integers(min_value=-100, max_value=100),
            ),
            min_size=1,
            max_size=6,
            unique_by=lambda pair: pair[0],
        )
    )
    def test_property_key_order_independent(self, pairs: list[tuple[str, int]]) -> None:
        # Build the dict in the given order, then in reverse; canonical
        # bytes MUST match.
        forward = dict(pairs)
        reverse = dict(reversed(pairs))
        assert canonical_bytes(forward) == canonical_bytes(reverse)

    def test_parses_back_to_equivalent(self) -> None:
        obj: dict[str, Any] = {"a": [1, 2, 3], "b": {"c": "d"}}
        parsed = json.loads(canonical_bytes(obj).decode("utf-8"))
        assert parsed == obj


class TestSha256Hex:
    def test_known_answer(self) -> None:
        # Regression: sha256("") is the well-known constant.
        assert sha256_hex(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_deterministic(self) -> None:
        assert sha256_hex(b"KRW-BTC") == sha256_hex(b"KRW-BTC")


class TestSidecarLine:
    def test_two_space_separator(self) -> None:
        line = sidecar_line("a" * 64, "snapshot.json")
        assert line == "a" * 64 + "  snapshot.json\n"

    def test_trailing_newline(self) -> None:
        assert sidecar_line("b" * 64, "x.json").endswith("\n")
