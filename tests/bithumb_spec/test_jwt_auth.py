"""Tests for :mod:`bithumb_bot.bithumb_spec.jwt_auth`.

Covers Finding 6 shape (access_key + nonce + query_hash/SHA-512 +
optional timestamp) and T-1-04-01 (credential material must never
appear in an AuthConstructionError message).

All test secrets are >=32 bytes to sidestep PyJWT's
`InsecureKeyLengthWarning` (RFC 7518 §3.2 minimum for HS256).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest import mock

import jwt as pyjwt
import pytest

from bithumb_bot.bithumb_spec.jwt_auth import bearer_header, build_jwt
from bithumb_bot.errors import AuthConstructionError

# Use a >=32 byte secret so PyJWT does not emit
# `InsecureKeyLengthWarning` (pytest filterwarnings=error turns any
# warning into a test failure).
SK = "sk_test_" + "x" * 32


class TestKnownAnswer:
    def test_minimal_payload_decodes(self) -> None:
        token = build_jwt(
            access_key="ak_test",
            secret_key=SK,
            query_params=None,
            nonce_fn=lambda: "fixed-nonce",
        )
        decoded = pyjwt.decode(token, SK, algorithms=["HS256"])
        assert decoded == {"access_key": "ak_test", "nonce": "fixed-nonce"}

    def test_empty_query_dict_treated_as_absent(self) -> None:
        """D-89 defensive: empty {} MUST NOT trigger query_hash."""
        token = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params={},
            nonce_fn=lambda: "n",
        )
        decoded = pyjwt.decode(token, SK, algorithms=["HS256"])
        assert "query_hash" not in decoded
        assert "query_hash_alg" not in decoded


class TestQueryHash:
    def test_query_hash_matches_sha512_of_url_encoded_sorted(self) -> None:
        token = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params={"market": "KRW-BTC"},
            nonce_fn=lambda: "n",
        )
        decoded = pyjwt.decode(token, SK, algorithms=["HS256"])
        expected = hashlib.sha512(b"market=KRW-BTC").hexdigest()
        assert decoded["query_hash"] == expected
        assert decoded["query_hash_alg"] == "SHA512"
        # Regression: 128 hex chars for SHA-512.
        assert len(decoded["query_hash"]) == 128

    def test_query_hash_keys_alphabetized(self) -> None:
        """Two mappings with the same keys/values but different insertion
        order MUST hash to the same value (sorted before urlencode)."""
        token_a = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params={"market": "KRW-BTC", "count": "10"},
            nonce_fn=lambda: "n",
        )
        token_b = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params={"count": "10", "market": "KRW-BTC"},
            nonce_fn=lambda: "n",
        )
        decoded_a = pyjwt.decode(token_a, SK, algorithms=["HS256"])
        decoded_b = pyjwt.decode(token_b, SK, algorithms=["HS256"])
        assert decoded_a["query_hash"] == decoded_b["query_hash"]


class TestIncludeTimestamp:
    def test_default_excludes_timestamp(self) -> None:
        token = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params=None,
            nonce_fn=lambda: "n",
        )
        decoded = pyjwt.decode(token, SK, algorithms=["HS256"])
        assert "timestamp" not in decoded

    def test_include_timestamp_true_adds_ms_int(self) -> None:
        frozen = datetime(2026, 9, 8, 1, 23, 45, tzinfo=UTC)
        token = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params=None,
            include_timestamp=True,
            nonce_fn=lambda: "n",
            now_fn=lambda: frozen,
        )
        decoded = pyjwt.decode(token, SK, algorithms=["HS256"])
        assert isinstance(decoded["timestamp"], int)
        assert decoded["timestamp"] == int(frozen.timestamp() * 1000)


class TestBearerHeader:
    def test_shape(self) -> None:
        assert bearer_header("abc.def.ghi") == {"Authorization": "Bearer abc.def.ghi"}


class TestSecretDiscipline:
    """T-1-04-01: no credential material may appear in the raised exception."""

    def test_encode_raises_secret_never_in_exception(self) -> None:
        sentinel = "0xDEADBEEF-SENTINEL-SK-" + "y" * 32
        with mock.patch("jwt.encode", side_effect=RuntimeError("boom-INNER-secret")):
            with pytest.raises(AuthConstructionError) as excinfo:
                build_jwt(
                    access_key="ak",
                    secret_key=sentinel,
                    query_params=None,
                    nonce_fn=lambda: "n",
                )
        # Full stringification of the exception MUST NOT contain any
        # 4+ char substring of the sentinel secret nor the inner error.
        rendered = str(excinfo.value) + repr(excinfo.value)
        for chunk in ("DEAD", "BEEF", "SENTINEL", "0xDE", "boom-INNER"):
            assert chunk not in rendered, (
                f"secret / inner error chunk {chunk!r} leaked into "
                f"AuthConstructionError rendering: {rendered!r}"
            )
        # Exception message is exactly the fixed sentinel.
        assert str(excinfo.value) == "Auth construction failed — see structured logs"

    def test_exception_class_carries_no_credential_attributes(self) -> None:
        with mock.patch("jwt.encode", side_effect=RuntimeError("x")):
            with pytest.raises(AuthConstructionError) as excinfo:
                build_jwt(
                    access_key="ak",
                    secret_key="SECRET_MATERIAL_" + "z" * 32,
                    query_params=None,
                    nonce_fn=lambda: "n",
                )
        assert excinfo.value.__dict__ == {} or "SECRET_MATERIAL" not in repr(
            excinfo.value.__dict__
        )

    def test_no_query_hash_on_none_params(self) -> None:
        token = build_jwt(
            access_key="ak",
            secret_key=SK,
            query_params=None,
            nonce_fn=lambda: "n",
        )
        decoded = pyjwt.decode(token, SK, algorithms=["HS256"])
        assert "query_hash" not in decoded


class TestDocstringMarker:
    """The plan requires a `# TODO(M1-verify)` anchor in the docstring."""

    def test_module_docstring_has_open_verification_item_marker(self) -> None:
        import bithumb_bot.bithumb_spec.jwt_auth as mod

        assert "TODO(M1-verify)" in (mod.__doc__ or "")
        assert "Open Verification Item #1" in (mod.__doc__ or "")

    def test_build_jwt_body_has_todo_anchor(self) -> None:
        """The plan says the docstring must anchor Open Verification Item #1;
        we also check that the function body contains the marker so a
        code reviewer stumbling into `build_jwt` sees the deferred fact."""
        import inspect

        src = inspect.getsource(build_jwt)
        assert "TODO(M1-verify)" in src
