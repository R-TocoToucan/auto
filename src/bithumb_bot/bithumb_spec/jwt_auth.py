"""Bithumb JWT builder (Finding 6) — D-70 secret discipline.

Bithumb's authenticated REST endpoints accept an ``Authorization:
Bearer <HS256-JWT>`` header. Every private-endpoint JWT this module
produces carries:

* ``access_key`` — the operator's public key ID.
* ``nonce``     — unique per request (UUID4 hex).
* ``timestamp`` — Unix epoch **milliseconds**, integer. Always present.

When the request has query parameters, the token additionally carries:

* ``query_hash``     — SHA-512 hex of the alphabetized, URL-encoded
  query string.
* ``query_hash_alg`` — the literal ``"SHA512"``.

Secret discipline (D-70 / T-1-04-01):

* The ``secret_key`` argument is a plain ``str`` because ``jwt.encode``
  requires it — but the local variable is ``del``-ed before the
  function returns so a debugger inspecting the frame after return
  finds nothing.
* Every raised exception is wrapped as :class:`AuthConstructionError`
  whose message is the fixed string ``"see structured logs"`` — no
  underlying error text, no credential material.
* ``bearer_header(token)`` produces the header dict; the plain string
  token is safe to pass through as a full JWT is not the key.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import jwt

from bithumb_bot.errors import AuthConstructionError


def _default_nonce() -> str:
    """Return a fresh UUID4 hex nonce (32 lowercase hex chars, no dashes)."""
    return uuid.uuid4().hex


def _default_now() -> datetime:
    return datetime.now(UTC)


def _compute_query_hash(query_params: Mapping[str, str]) -> str:
    """SHA-512 hex of the URL-encoded, alphabetically-sorted query string.

    Bithumb's documentation (per Finding 6) specifies alphabetically-
    sorted keys, standard URL encoding, and SHA-512. We use
    :func:`urllib.parse.urlencode` with ``sorted(items)`` — the same
    call ``httpx`` uses internally, so what we hash matches what we
    later place on the wire.
    """
    encoded = urlencode(sorted(query_params.items()))
    return hashlib.sha512(encoded.encode("utf-8")).hexdigest()


def build_jwt(
    access_key: str,
    secret_key: str,
    query_params: Mapping[str, str] | None,
    *,
    nonce_fn: Callable[[], str] = _default_nonce,
    now_fn: Callable[[], datetime] = _default_now,
) -> str:
    """Build an HS256 JWT bearer token for a Bithumb authenticated call.

    The returned token ALWAYS contains ``access_key``, ``nonce`` and a
    millisecond-integer ``timestamp`` claim. When ``query_params`` is
    non-empty, ``query_hash`` and ``query_hash_alg="SHA512"`` are also
    present. An empty dict is treated as absent (D-89 defensive:
    never hash an empty string).

    Args:
        access_key:   The operator's public access-key ID.
        secret_key:   The operator's private secret key (HMAC key
                      material). Passed as a plain ``str`` because
                      ``jwt.encode`` requires it; ``del``-ed before
                      this function returns (T-1-04-01).
        query_params: Optional URL query mapping.
        nonce_fn:     Injectable nonce factory for deterministic tests.
                      Default is :func:`_default_nonce`.
        now_fn:       Injectable current-UTC factory for deterministic
                      tests. Default is :func:`_default_now`.

    Returns:
        A signed JWT string suitable for the ``Authorization: Bearer``
        header.

    Raises:
        AuthConstructionError: any exception raised during payload
            assembly or ``jwt.encode``. The exception message is fixed
            to ``"see structured logs"`` — no credential material is
            spliced in.
    """
    try:
        payload: dict[str, Any] = {
            "access_key": access_key,
            "nonce": nonce_fn(),
            "timestamp": int(now_fn().timestamp() * 1000),
        }
        if query_params:  # empty dict OR None -> skip (D-89 defensive)
            payload["query_hash"] = _compute_query_hash(query_params)
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, secret_key, algorithm="HS256")
        # PyJWT ≥2.0 returns str; older versions returned bytes.
        if isinstance(token, bytes):  # pragma: no cover — defensive
            token = token.decode("utf-8")
    except Exception:
        # D-70 / T-1-04-01: NEVER splice the underlying error or
        # credential material into the raised exception.
        raise AuthConstructionError() from None
    finally:
        # Best-effort: drop the local reference. Python may still hold
        # it in the encode-call frame's locals, but this is the cleanest
        # we can achieve from inside pure Python.
        del secret_key
    return token


def bearer_header(token: str) -> dict[str, str]:
    """Return the ``Authorization`` header dict for a bearer ``token``.

    Args:
        token: A JWT string (or any bearer token).

    Returns:
        ``{"Authorization": f"Bearer {token}"}``.
    """
    return {"Authorization": f"Bearer {token}"}


__all__ = ["bearer_header", "build_jwt"]
