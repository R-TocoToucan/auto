"""`bithumb_bot.bithumb_spec` — authenticated read-only Bithumb spec adapter.

Public surface (built up across plan 01-04):

* :mod:`.jwt_auth` — Bithumb JWT builder (D-40 / D-89).
* :mod:`.http_client` — `httpx.AsyncClient` factory + retry loop
  honoring `Retry-After` (D-40).
* :mod:`.schemas` — pydantic allowlist models for the authenticated
  ``/v1/orders/chance`` response (D-77 allowlist).
* :mod:`.sanitize` — allowlist-only sanitizer used before any fixture
  is committed (D-77).
* :mod:`.snapshot` — `SnapshotV1` + build/write/load with sidecar
  hashing (D-75, D-76, D-79, D-80).
* :mod:`.verification` — `VERIFICATION.md` bundle scaffolder + parser
  (D-78, D-84).
* :mod:`.client` — `fetch_spec()` orchestrator (D-85, D-89).
* :mod:`.rate_limits` — three per-channel `TokenBucket` instances
  (sentinel values pending Open Verification Item #3).

Every function in this package that touches a credential invokes
:func:`bithumb_bot.config.validator.validate` FIRST (D-85 defense in
depth). No live venue call is made in CI (D-79) — every test flows
through ``httpx.MockTransport``.
"""
