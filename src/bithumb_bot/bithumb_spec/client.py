"""`fetch_spec()` — orchestrates authenticated M1 spec fetch (D-85, D-89).

This is the ONLY function in the codebase that constructs the
account/read credential class (D-89: constructed inside the exact M1
network operation that requires it — never held as a module-global).

Sequence (each numbered step maps to plan Behavior):

1. `validate(("m1", "fetch-spec"))` — refuses if not `ok`
   (defense in depth per D-85 even though the CLI dispatcher already
   ran the same check).
2. Load `BithumbSecrets` ephemerally (D-89).
3. Build the JWT via `build_jwt(access_key, secret, query_params,
   include_timestamp=False)` — Open Verification Item #1 default.
4. Open `httpx.AsyncClient` with D-40 timeouts (transport injectable
   for D-79 offline test).
5. `request_with_retry(...)` on `/v1/orders/chance` with
   `private_rest` bucket.
6. `OrdersChanceResponse.model_validate(response.json())`.
7. Sanitize → write sanitized fixture (`sanitize_orders_chance` + a
   canonical write of the picked fields).
8. `build_snapshot(...)` from the parsed response + fixture path.
9. `write_snapshot_with_sidecar(snapshot,
   artifacts/spec_snapshots/{market}/{utc_ts}.json)`.
10. `write_verification_bundle(verification/bithumb/{utc_ts}/, ...)`
    with the FIVE_BUILD_TIME_FACTS scaffolded unchecked.
11. `del secret_str`; return the `FetchSpecResult`.

Every test in `tests/bithumb_spec/test_fetch_spec_end_to_end.py`
uses `httpx.MockTransport` — no real network I/O ever occurs (D-79).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from bithumb_bot.artifact.canonical import canonical_bytes, write_with_sidecar
from bithumb_bot.artifact.timestamps import utc_timestamp
from bithumb_bot.bithumb_spec.http_client import create_client, request_with_retry
from bithumb_bot.bithumb_spec.jwt_auth import bearer_header, build_jwt
from bithumb_bot.bithumb_spec.rate_limits import private_rest
from bithumb_bot.bithumb_spec.sanitize import sanitize_orders_chance
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    build_snapshot,
    write_snapshot_with_sidecar,
)
from bithumb_bot.bithumb_spec.verification import (
    FIVE_BUILD_TIME_FACTS,
    write_verification_bundle,
)
from bithumb_bot.config.validator import validate
from bithumb_bot.errors import (
    ProhibitedCredentialDetectedError,
    SnapshotValidationError,
)
from bithumb_bot.secrets.loader import load_secrets, reject_trade_credentials

_DEFAULT_BASE_URL = "https://api.bithumb.com"
# D-40 (Gate-1 frozen).
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 15.0
_MAX_ATTEMPTS = 3
_BACKOFF_INITIAL_MS = 500
_BACKOFF_CAP_MS = 5000


@dataclass(frozen=True)
class FetchSpecResult:
    """Filesystem artifacts produced by a successful `fetch_spec` run."""

    snapshot_path: Path
    fixture_path: Path
    verification_bundle_dir: Path


async def fetch_spec(
    market: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    repo_root: Path | None = None,
    artifacts_root: Path | None = None,
) -> FetchSpecResult:
    """Fetch the Bithumb spec + write the three-artifact bundle.

    Args:
        market:         KRW market symbol (e.g. ``"KRW-BTC"``).
        transport:      Optional `httpx.AsyncBaseTransport` for tests
                        (D-79 offline: pass an `httpx.MockTransport`).
        base_url:       Bithumb REST origin. Defaults to
                        ``https://api.bithumb.com``. TODO(M1-verify):
                        the real value is a build-time verification
                        item; the M1 verification bundle records the
                        confirmed origin.
        repo_root:      Repository root for the account-read credential
                        load (`load_secrets`). Defaults to
                        `Path.cwd()` — tests pass `tmp_path` after
                        planting a `config/decisions/gate1.toml`.
        artifacts_root: Root directory under which artifacts, fixtures
                        and verification bundles are written. Defaults
                        to `Path.cwd()` — tests pass `tmp_path`.

    Returns:
        A `FetchSpecResult` with the absolute paths of the sanitized
        fixture, the pinned snapshot, and the verification bundle dir.

    Raises:
        SnapshotValidationError: `validate(('m1', 'fetch-spec'))`
            refused (missing gate1, missing account/read cred, etc.).
        ProhibitedCredentialDetectedError: a trade credential class
            was present at load time (D-68 / D-97).
        `httpx.HTTPError`: pass-through if the retry loop exhausted
            with pure network errors.
    """
    # 1. D-85 defense in depth (dispatcher already ran validate; we
    #    run it again from inside the service function so direct
    #    Python callers also trip the guard).
    _result = validate(("m1", "fetch-spec"))
    if not _result.ok:
        raise SnapshotValidationError(
            f"validate() refused m1 fetch-spec: {_result.reason} "
            f"(missing: {list(_result.missing)!r})"
        )

    repo_root = repo_root or Path.cwd()
    artifacts_root = artifacts_root or Path.cwd()

    # 2. Ephemeral credential load (D-89). `settings` and `secret_str`
    #    stay in this function's local frame; we `del` them before
    #    returning.
    settings = load_secrets(repo_root)
    reject_trade_credentials(settings)  # defense in depth (D-89)
    if (
        settings.account_read_access_key is None
        or settings.account_read_secret_key is None
    ):
        raise SnapshotValidationError(
            "account/read credentials missing at fetch_spec entry "
            "(D-68 requires them for m1 fetch-spec)"
        )
    access_key = settings.account_read_access_key.get_secret_value()
    secret_str = settings.account_read_secret_key.get_secret_value()

    try:
        # 3. Build JWT (Open Verification Item #1 default: no timestamp).
        query_params = {"market": market}
        token = build_jwt(
            access_key=access_key,
            secret_key=secret_str,
            query_params=query_params,
            include_timestamp=False,
        )

        # 4. Open the client with D-40 timeouts.
        async with create_client(
            base_url=base_url,
            connect_timeout_s=_CONNECT_TIMEOUT_S,
            read_timeout_s=_READ_TIMEOUT_S,
            transport=transport,
        ) as client:
            # 5. Retry loop with private_rest bucket.
            response = await request_with_retry(
                client,
                "GET",
                "/v1/orders/chance",
                headers=bearer_header(token),
                params=query_params,
                max_attempts=_MAX_ATTEMPTS,
                backoff_initial_ms=_BACKOFF_INITIAL_MS,
                backoff_cap_ms=_BACKOFF_CAP_MS,
                bucket=private_rest,
            )
        # 6. Validate the response body (allowlist model, D-77).
        response.raise_for_status()
        raw_body: Any = response.json()
        sanitized_body = sanitize_orders_chance(raw_body)
        parsed = OrdersChanceResponse.model_validate(sanitized_body)

        # 7. Write sanitized fixture.
        ts = utc_timestamp()
        fixture_dir = (
            artifacts_root
            / "tests"
            / "fixtures"
            / "bithumb"
            / "sanitized"
            / "orders_chance"
        )
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path = fixture_dir / f"{ts}.json"
        write_with_sidecar(fixture_path, canonical_bytes(sanitized_body))

        # 8. Build snapshot.
        snapshot = build_snapshot(
            parsed, market=market, fixture_paths=[fixture_path]
        )

        # 9. Write snapshot + sidecar.
        snap_dir = artifacts_root / "artifacts" / "spec_snapshots" / market
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snap_dir / f"{ts}.json"
        write_snapshot_with_sidecar(snapshot, snapshot_path)

        # 10. Verification bundle scaffold.
        bundle_dir = artifacts_root / "verification" / "bithumb" / ts
        # `write_verification_bundle` reads snapshot_path for its
        # manifest; the manifest records only the filename by
        # convention, so we copy the snapshot next to the manifest
        # only when the operator wants a self-contained bundle. For
        # M1 we point the bundle at the primary snapshot location.
        bundle_snapshot = bundle_dir / snapshot_path.name
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_snapshot.write_bytes(snapshot_path.read_bytes())
        (bundle_dir / (snapshot_path.name + ".sha256")).write_bytes(
            snapshot_path.with_name(snapshot_path.name + ".sha256").read_bytes()
        )
        write_verification_bundle(
            bundle_dir,
            snapshot_path=bundle_snapshot,
            fixture_paths=[fixture_path],
            facts=FIVE_BUILD_TIME_FACTS,
        )

        # 11. Return; drop secret references first.
        return FetchSpecResult(
            snapshot_path=snapshot_path,
            fixture_path=fixture_path,
            verification_bundle_dir=bundle_dir,
        )
    finally:
        # D-70 / D-89 discipline: drop local secret references before
        # returning. The `settings` object is also let go; Python GC
        # will reclaim it in due course (SecretStr masks `repr`/`str`
        # regardless).
        try:
            del secret_str
        except NameError:  # pragma: no cover
            pass


__all__ = ["FetchSpecResult", "fetch_spec"]


# ---------------------------------------------------------------------------
# Re-export for the CLI handler (so it can catch the same exception).
# ---------------------------------------------------------------------------
_ = ProhibitedCredentialDetectedError  # keep the import used
