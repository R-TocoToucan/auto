# Stack Research

**Domain:** Single-asset, long-or-cash Bithumb (KRW spot) crypto autotrading bot — this milestone builds only the data pipeline, a venue-aware conservative execution *simulator*, and a preregistered statistical *validation* harness. No live trading, no trade key.
**Researched:** 2026-09-08
**Confidence:** MEDIUM-HIGH overall (HIGH on stdlib/mature-library picks; LOW on anything touching Bithumb-specific SDKs or niche statistics packages — flagged individually below and must be re-verified against `apidocs.bithumb.com` and PyPI at build time)

> Supplements `docs/RESEARCH.md` / `docs/EXECUTION.md`. Nothing here overrides a Gate-1/2/3 decision; where this file mentions a library that would encode a Gate-1 choice (e.g. which stop mechanism), the library choice still waits on that decision.

---

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|------------------|
| Python | 3.12 or 3.13 | Runtime | Current stable CPython line as of 2026; both `httpx`, `websockets`, `arch`, `pydantic`, `polars` support it. Avoid 3.14 until the dependency ecosystem (esp. `arch`/numba-adjacent wheels) catches up — verify at build time. **Confidence: HIGH** |
| `decimal` (stdlib) | stdlib | Exact-decimal money/qty/PnL arithmetic | This is the one place the project cannot compromise. `Decimal` gives exact base-10 arithmetic when constructed from strings (never from `float`); it is the industry-default answer to "financial arithmetic in Python" over any third-party money type. Zero dependency risk — the correctness of your P&L math should never depend on an external package's release cadence. **Confidence: HIGH** |
| `httpx` | ≥0.27 | Bithumb REST client (sync + async) | Modern `requests` successor with first-class async, HTTP/2, and typed timeouts/retries hooks — the 2025/2026 default for new REST clients. Use one `httpx.Client`/`AsyncClient` per auth scheme (public / JWT / legacy) since headers differ. **Confidence: HIGH** |
| `websockets` | ≥16.0 (Jan 2026 release; requires Python ≥3.10) | Bithumb public + private (v2) WebSocket feeds | Mature, dependency-free asyncio WS client/server library. Recent releases (2025–2026) added a `reconnect_delays` argument to `connect()` for exponential-backoff tuning beyond the built-in transient-error backoff — exactly the reconnect/backoff behavior `docs/RESEARCH.md` §4/§5 requires (ping/pong, reconnect backoff, dedup after reconnect). **Confidence: HIGH** (verify current version against PyPI at build time) |
| `pandas` + `numpy` | pandas ≥2.2, numpy ≥1.26 | In-memory OHLCV analysis, indicator math, returns series for validation | The statistics stack this project actually needs (`arch`, `scipy`, `statsmodels`-adjacent tooling) is pandas/numpy-native; converting through Polars would add friction with no payoff at this project's data scale (single asset, one 4h series — tens of thousands of rows even including 1-minute calibration windows). See "What NOT to Use" for why Polars is deliberately skipped. **Confidence: HIGH** |
| `pyarrow` | ≥16 | Parquet read/write engine for the immutable candle store | Gives exact round-trip control over column types (including `decimal128`) that `pandas.to_parquet` alone leaves implicit. Used directly for the immutable-store writer/reader, not just as a pandas backend. **Confidence: HIGH** |
| `pydantic` | v2 (≥2.9) | Typed config/settings for Gate-1/2/3 decisions, env secrets, API response models | v2's Rust core (`pydantic-core`) is the de facto standard for typed Python config/validation in 2025/2026. **Gotcha (see "What NOT to Use"): validate money/qty fields as `Decimal` constructed from `str`, never allow implicit `float` coercion.** **Confidence: HIGH**, gotcha detail MEDIUM (verify field-level strict-mode config against current pydantic v2 docs at build time) |
| `structlog` | current (≥24) | Structured, contextvars-aware logging | The 2025/2026-standard answer to "structured logging in Python" — wraps/augments stdlib logging with JSON output for production and human-readable console rendering for dev, with per-request/per-order context binding. Needed for the audit trail EXECUTION.md requires (fills, errors, kill-switch, reconciliation events, order-intent lifecycle). **Confidence: HIGH** |
| `PyJWT` | ≥2.9 | JWT construction for Bithumb's current v1/v2 private-endpoint auth (`access_key`, `nonce`, `timestamp`, `query_hash` claims, HS256) | Bithumb's current API docs (`apidocs.bithumb.com`) confirm JWT auth with HMAC-signed claims for private endpoints (e.g. `/v1/orders/chance`) — PyJWT is the standard, minimal, well-maintained Python JWT library; no need for `python-jose` or heavier alternatives since Bithumb uses symmetric HS256, not RSA. **Confidence: MEDIUM-HIGH** (claim structure — `access_key`/`nonce`/`timestamp`/`query_hash` — corroborated by multiple independent sources describing `apidocs.bithumb.com`, but re-verify exact claim names/hash construction against the live docs before M1) |
| `hmac` + `hashlib` (stdlib) | stdlib | Legacy `Api-Key`/`Api-Nonce`/`Api-Sign` signing for the legacy `/trade/stop_limit` automatic-order endpoint, **only if Gate-1 selects the legacy stop-limit** | This is HMAC-SHA512-over-concatenated-params, which is exactly what stdlib `hmac`/`hashlib` do — no library needed at all. **Do not** pull in a wrapper SDK for this; it is the single most security-sensitive code path in the project (order-placing signature) and belongs in code you wrote and can unit-test with known-answer vectors. **Confidence: MEDIUM** (the legacy scheme's exact hash algorithm/param-ordering must be verified against current `apidocs.bithumb.com` docs — search results describe the general shape but not a confirmed current spec page; flag for build-time verification per `docs/RESEARCH.md`'s own caveat) |

### Execution Simulator — Build vs. Buy (explicit verdict)

**Verdict: BUILD CUSTOM. Do not adopt vectorbt, backtesting.py, backtrader, or nautilus_trader as the M2 simulator engine.**

| Library | Why it does NOT satisfy M2's requirements |
|---|---|
| **vectorbt** | Vectorized (NumPy/Numba) across the whole history for fast parameter-sweep research. Slippage/fill modeling is simplified by design (that's the tradeoff for speed) — it has no concept of a stop-*limit* state machine, gap-below-limit-means-no-fill semantics, or Bithumb's `watch → wait → done/cancel` states. Wrong tool for a project whose stated premise is "execution realism, not signal speed." **Confidence: HIGH** |
| **backtesting.py** | Good for fast prototyping; fills are a simple next-bar-open/close model with no configurable gap rule and no stop-limit lifecycle. Cannot express "assume no fill unless trade data establishes trigger-before-fill ordering." **Confidence: HIGH** |
| **backtrader** | Feature-rich but effectively in maintenance mode; float-based broker simulation; no venue-specific order-lifecycle modeling. Would need as much custom code bolted on as building from scratch, with less control. **Confidence: MEDIUM-HIGH** |
| **nautilus_trader** | The closest philosophical match — event-driven, Rust-native, designed explicitly to avoid look-ahead by using bar `ts_event` as close-time and processing events in strict chronological order, and it does model L2/latency/queue position for venues it supports. **But**: (1) as of late 2025/2026 there is an *open GitHub issue* (`nautechsystems/nautilus_trader#4063`) specifically requesting "next-bar-open execution for bar-based backtesting" — i.e. the exact ≥t+1-open temporal-separation guarantee this project needs is not yet a first-class, drop-in feature even in the most execution-realistic mainstream engine. (2) It has no notion of Bithumb's legacy stop-limit endpoint or its idiosyncratic gap-below-limit-retains-exposure rule — you would write that state machine yourself regardless. (3) Adopting it means adopting its instrument/venue/data-type model, a Rust build toolchain, and a large surface area, for a single-asset single-venue backtest whose hardest logic (the Bithumb-specific state machine) it cannot provide anyway. The integration cost is not repaid. **Confidence: MEDIUM-HIGH** |

**What to build instead** (small, in-repo, fully unit-testable against M2's own "known-answer replay" requirement):
- A plain Python event loop over the immutable candle store: for each closed candle *t*, compute the signal, then hand any resulting order to a simulator that may only fill at *t+1* or later — enforced by construction (the simulator never sees candle *t+1* until it has already recorded candle *t*'s decision), not by convention.
- A cost model module (fees from M1, slippage/impact modeled per the cost-scenario section) — plain Python + `Decimal`.
- A stop-limit state machine implementing exactly the states named in `docs/EXECUTION.md` (`watch → wait → done/cancel`), because this is bespoke Bithumb-account-lifecycle logic no third-party backtest library encodes.
- Estimated size: a few hundred to ~1000 lines of pure Python. This is smaller, more auditable, and easier to property-test (`hypothesis`) than gluing a framework's abstractions to a domain-specific requirement it wasn't built for.
- **Optional, later, non-core:** once a preregistered baseline exists (M4+), a fast vectorized tool like vectorbt *could* be used purely as a cheap pre-screen across parameter grids in M5's module-addition sweeps — never as the fill-model source of truth, and never touching the final holdout. Not required for this milestone; flag as a nice-to-have only if module sweeps in M5 become slow.

### Statistical Validation Tooling

| Library | Version | Purpose | Why / Caveat |
|---------|---------|---------|---------------|
| `arch` (Kevin Sheppard) | ≥7.2 (stable) / 8.x in dev | Block bootstrap (`StationaryBootstrap`, `CircularBlockBootstrap`, `MovingBlockBootstrap`, `optimal_block_length`) **and** White's Reality Check / Hansen's SPA test (`arch.bootstrap.SPA`) | This is the single best find of this research pass: one mature, actively maintained, widely-used package (best known for GARCH modeling, but its `bootstrap` submodule is exactly the toolset `docs/RESEARCH.md` §3.3 calls for) covers **both** the block-bootstrap requirement and the Reality-Check/SPA requirement with real, documented, tested implementations — not something you'd want to hand-roll. Use `StationaryBootstrap` on the aligned per-period excess-return series as specified; use `optimal_block_length` to justify the block-length choice in Gate 2. **Confidence: HIGH** |
| Custom, in-repo | — | Deflated Sharpe Ratio (DSR) and Probabilistic Sharpe Ratio (PSR) | **Do not depend on a third-party DSR package for this project's single most safety-critical number.** The formerly-canonical implementation (`mlfinlab`) went closed-source/paid years ago; the free fork has been unmaintained since 2018. Newer PyPI entrants found in this search (`sharpebench`, `quant-harness`) bundle DSR/PSR/PBO/CSCV/Reality-Check/SPA/BH-FDR, but are new, of **unverified maturity and correctness** (no evidence found of citation, audit, or wide adoption) — do not trust them blind. DSR/PSR are closed-form (Bailey & López de Prado 2014, already cited in `docs/RESEARCH.md`), roughly 30–50 lines each, and straightforward to implement directly with `numpy`/`scipy.stats` (`skew`, `kurtosis`) and cover with `hypothesis` property tests plus known-answer regression tests against the paper's own worked example. **Confidence: HIGH that this is the right call; LOW confidence in any specific third-party DSR package — if one is used at all, vet its test suite and correctness against the source paper before trusting it, or use it only as a cross-check against your own implementation, never as the sole implementation.** |
| `pypbo` (esvhd/pypbo, GitHub) | — | Reference implementation of PBO/CSCV (Bailey et al.) | Small, focused, matches the cited paper directly — useful to **read and cross-check against**, but it is not on PyPI as an actively released package and its maintenance status is unclear (last activity not confirmed current). Treat as a reference/vendor-in-if-useful candidate, not a pinned runtime dependency. Implement PBO/CSCV in-repo (it's a well-specified combinatorial procedure over a candidate return matrix) and cross-check against `pypbo`'s logic during development. **Confidence: MEDIUM** |
| Custom, in-repo (or `purgedcv` PyPI, if evaluated) | — | Purged/embargoed CV, only where label/event horizons overlap (a narrow use case per `docs/RESEARCH.md` §3.3) | `docs/EXECUTION.md`'s main selection design is nested walk-forward (plain rolling train/selection/holdout windows), not full combinatorial purged CV — so a hand-rolled purge+embargo helper (a few dozen lines) is likely sufficient and keeps this safety-critical logic auditable. `purgedcv` (PyPI, scikit-learn-compatible, includes CPCV + DSR) exists if the need grows more complex than a simple walk-forward, but is a newer/smaller package — vet before adopting. **Confidence: MEDIUM** |
| `scipy`, `numpy` | current | Underlying numerical primitives (skew, kurtosis, distributions) for DSR/PSR and general stats | Already a transitive dependency of `arch`; use directly rather than re-deriving. **Confidence: HIGH** |

### Bithumb REST/WebSocket Access — SDK vs. Custom (explicit verdict)

**Verdict: BUILD a thin custom client on `httpx` + `websockets` + `PyJWT` + stdlib `hmac`. Do not adopt a community wrapper or `ccxt` as the integration layer.**

- **No official Bithumb Python SDK exists.** `apidocs.bithumb.com` publishes REST/WS docs (and an `llms.txt` index) but no first-party client library. **Confidence: HIGH**
- **Do not confuse `bithumb-pro` / `bithumbfutures` GitHub docs with this project's target venue.** `bithumb-pro/bithumb.pro-official-api-docs` and `bithumbfutures.github.io` describe **Bithumb Global / Bithumb Futures** — a related but distinct product/API surface (different base URL, different HMAC-over-alphabetized-params auth scheme) from the Korean KRW-spot `apidocs.bithumb.com` API this project targets. This is an easy, high-consequence mix-up (identical brand name, different API) — **flag for explicit verification at M1 build time**, and never copy auth code from the `bithumb-pro` docs. **Confidence: HIGH that this distinction matters; verify current base URLs directly at build time.**
- **Community wrapper `python-bithumb` (youtube-jocoding, PyPI, Apache-2.0)** — actively released through 2025 (0.1.3 in June 2025), covers public/private v1-style endpoints (price, candlestick/OHLCV, orders, balances, order cancellation). **No evidence found that it implements the legacy `/trade/stop_limit` automatic-order endpoint or the private v2 WebSocket** — and given how niche that endpoint is, assume it does not without checking the source. Even where it overlaps with what's needed, adopting it means trusting an unaudited third party for the exact code path (request signing) that moves money later. **Confidence: MEDIUM** (existence/recency verified; feature completeness NOT verified — read the source before relying on any claim about it)
- **Older `pybithumb` (sharebook-kr)** — predates the JWT v2 migration; likely targets the legacy v1 REST API only. Not recommended as a base for new JWT-authenticated v2 code. **Confidence: MEDIUM**
- **`ccxt`'s bithumb module** — a 100+-exchange unified library necessarily targets lowest-common-denominator spot trading (place/cancel/query limit and market orders). It is extremely unlikely to expose Bithumb's proprietary legacy stop-limit endpoint or the dual-auth-scheme nuance this project's Gate-1 may require, and adds a large, frequently-changing dependency for the sake of one exchange. **Confidence: MEDIUM-HIGH**
- **Why build custom is right here, not just "no good option exists":** `docs/EXECUTION.md` explicitly requires *two separate auth adapters* (JWT for v1/v2 endpoints; legacy `Api-Key`/`Api-Nonce`/`Api-Sign` for `/trade/stop_limit` if selected), an idempotency/reconciliation protocol with no `client_order_id` support on the legacy endpoint, and per-channel rate-limit handling. No existing wrapper is built around these constraints — a wrapper library would only save you writing typed request/response models, while hiding exactly the signing and error-handling code you most need to audit and unit-test with known-answer vectors. A ~150–300 line custom client (two auth adapters + typed endpoint methods you actually use + per-channel token-bucket rate limiting) is more trustworthy and no slower to build than vetting and patching someone else's wrapper.
- **Rate limiting:** implement a minimal per-channel token bucket in stdlib Python (`time.monotonic()` + a counter, ~20 lines) — one instance per channel (public REST, private REST, batch, WS connection) per `docs/RESEARCH.md`'s "separate token buckets and backoff per channel" requirement. Do not add `pyrate-limiter`/`limits` for this; it's YAGNI at this scope. **Confidence: HIGH**

### Data Storage — Immutable Candle Archive

| Choice | Why |
|---|---|
| **Parquet files** (via `pyarrow`), one file (or small partition) per `(market, interval)`, append-only, never mutated in place | Satisfies M2's "immutable store with integrity checks" requirement directly: a file, once written, is never edited — corrections are new files plus a superseding manifest entry, giving a natural audit trail. **Confidence: HIGH** |
| **Manifest file** (JSON, hand-rolled — no library needed) recording per-Parquet-file SHA-256 (stdlib `hashlib`), row count, min/max timestamp, and write timestamp | This *is* the "integrity check" — a load path that verifies the checksum before trusting a file catches silent corruption or partial writes. A few dozen lines of stdlib code; no dependency. **Confidence: HIGH** |
| **Exact price/qty columns stored as `pyarrow.decimal128` (or, if that proves awkward with a chosen tool, as fixed-format strings) — never `float64`** | This is the one non-obvious but load-bearing stack rule: pandas/NumPy/Polars all treat `Decimal` as slow "object" data or need explicit decimal-dtype support, and Parquet's own native float types will silently reintroduce the exact defect this project exists to remove if OHLCV prices round-trip through `float64`. Keep raw price/quantity as decimal/string all the way to the point they're consumed by the `Decimal`-based simulator; only *derived* indicator/statistics columns (ADX, ATR, returns for DSR/bootstrap) may live as `float64`, since academic convention there is float and exactness is not the safety property being protected. **Confidence: HIGH on the principle; MEDIUM on `pyarrow.decimal128` specifically — verify current pandas/pyarrow decimal-dtype interop at build time, string-encoding is the simpler fallback if friction appears.** |
| **No database (DuckDB, SQLite, Postgres) for v1** | YAGNI at this data scale: single asset, one native 4h series (thousands of rows) plus modest calibration-period 1-minute data — a directory of Parquet files plus a manifest is sufficient, simpler to reason about as "immutable," and has zero operational surface (no server, no migrations). **Optional later:** DuckDB can query a folder of Parquet files directly with zero ingestion cost, so it remains available as a pure convenience layer for ad-hoc analysis without ever becoming the system of record. **Confidence: HIGH** |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| `uv` | Dependency management, virtualenvs, lockfile, `pyproject.toml` (PEP 621) | The 2025/2026 default: ~10x faster than Poetry on cold installs/lockfile resolution, was PEP 621-compliant from the start (Poetry only added PEP 621 support in v2.0, Jan 2025), and has visible production adoption (Anthropic, Stripe, and others cited in CI usage reports). No compelling reason to reach for Poetry or raw `pip`/`pip-tools` for a new project in 2026. **Confidence: HIGH** |
| `ruff` | Linting + formatting (replaces flake8 + Black + isort + pyupgrade) | One Rust binary, ~900 lint rules from 50+ legacy tools, was rated the most-admired developer tool in the 2025 Stack Overflow survey. Configure via `pyproject.toml`; run in pre-commit and CI. **Confidence: HIGH** |
| `mypy --strict` | Static typing | Recommended as the primary type checker: most mature, has a `pydantic` plugin for accurate model typing, and is the tool most existing Decimal/pydantic-typing guidance targets. `pyright` (or the newer `pyrefly`) is a legitimate, faster alternative, especially if already using VS Code/Pylance — but at this project's codebase size, mypy's plugin maturity outweighs the speed difference. **Confidence: MEDIUM-HIGH** (re-check `pyrefly` maturity at build time — it's very new as of this research pass) |
| `pytest` + `hypothesis` | Testing, including property-based tests | `pytest` is the default; `hypothesis` is specifically valuable here beyond typical usage — property-test the `Decimal` rounding/tick/step helpers (M1), the stop-limit state machine and gap rule (M2's own "known-answer replay," "gap-through fills adverse," "ambiguous intrabar → no fill" test requirements), and the JWT/HMAC signing functions against known-answer vectors. This pairing directly matches the kind of edge-case-heavy, monetary, state-machine code this milestone is full of. **Confidence: HIGH** |
| `structlog` | Structured logging (listed above under Core, repeated here for dev-tooling context) | Configure once at process start; bind order-intent IDs / experiment IDs / candle timestamps as context for every log line touching a decision. |
| `pandera` *(optional)* | DataFrame schema validation (OHLCV column types, monotonic timestamps, no duplicate candles, gap detection) | Nice-to-have, not mandatory: at this project's scale, explicit assertions in the ingestion code may be simpler (ponytail: don't add a dependency until the manual checks get unwieldy). Consider adopting only if the M2 "dedupe, validate timezones, handle gaps" logic grows past a few straightforward checks. **Confidence: MEDIUM** |
| `pre-commit` | Run ruff/mypy/pytest fast-subset on commit | Standard hygiene; configure to run `ruff check --fix`, `ruff format`, and `mypy` on changed files. |

---

## Installation

```bash
# Core runtime deps
uv add httpx websockets pandas numpy pyarrow pydantic pydantic-settings structlog pyjwt arch scipy

# Dev dependencies
uv add --dev ruff mypy pytest hypothesis pre-commit

# Optional, evaluate before adding
# uv add pandera        # only if manual OHLCV validation checks become unwieldy
# uv add duckdb         # only if ad-hoc SQL over the Parquet lake becomes useful
```

Do **not** add: `vectorbt`, `backtesting.py`, `backtrader`, `nautilus_trader`, `ccxt`, `pybithumb`/`python-bithumb`, `mlfinlab`, `py-moneyed`/`dinero`, `polars`, `pyrate-limiter`/`limits` — see "What NOT to Use" for the specific reason for each.

---

## Alternatives Considered

| Category | Recommended | Alternative | When Alternative Makes Sense |
|----------|-------------|-------------|-------------------------------|
| Execution backtest engine | Custom, event-driven, `Decimal`-based | `nautilus_trader` | If this project later expands to multi-venue, multi-asset, latency-sensitive live trading (M6B+/v2+) where Nautilus's production-grade order-book/latency simulation and unified backtest/live codepaths start paying for their integration cost. Not this milestone. |
| Dataframe library | `pandas` | `polars` | If candle data volume grows by orders of magnitude (e.g. full L2 tick-level historical data across many assets) such that pandas's single-threaded, non-lazy execution becomes a measured bottleneck. Revisit only with an actual perf complaint in hand. |
| DSR/PSR/PBO implementation | Custom, in-repo, tested against the source papers | `sharpebench` / `quant-harness` (PyPI) | If one of these packages develops a track record (citations, wide adoption, visible test coverage against the Bailey/López de Prado formulas) by the time M3/M4 is built — re-evaluate then, and even so, cross-check against your own reference implementation before trusting a pass/fail gate to it. |
| Bithumb API access | Custom `httpx`/`websockets`/`PyJWT` client | `python-bithumb` (youtube-jocoding) | If, after reading its source, it turns out to fully and correctly cover the JWT v2 endpoints this project needs (candles, `/v1/orders/chance`, balances) — it could still be reasonable to vendor/fork just the read-only public/JWT surface, while writing the legacy stop-limit auth and idempotency logic yourself regardless (that part no wrapper will have). |
| Type checker | `mypy --strict` | `pyright` / `pyrefly` | If IDE-integrated fast feedback matters more than plugin ecosystem maturity, or if `pyrefly` (Meta's new, much faster checker) proves stable and well-documented by build time. |
| Package manager | `uv` | `poetry` | If the team specifically wants Poetry's more opinionated project-management UX and doesn't mind materially slower installs; no functional reason to prefer it for a new project in 2026. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|--------------|
| `float` for any price, quantity, fee, or PnL value | Binary floating point cannot represent most decimal fractions exactly; silently introduces the exact class of "impossible number" bug this project's whole premise is to eliminate. | `decimal.Decimal`, constructed from `str`, everywhere money/quantity touches disk, network, or arithmetic. |
| `pydantic` model fields typed `Decimal` but fed from JSON `float`/`int` literals without strict validation | Pydantic v2 will coerce a JSON float into a `Decimal` via the float's imprecise binary value unless the field/model is configured to require a string or already-`Decimal` input — silently reintroducing float error through the back door. | Parse Bithumb API responses' numeric-as-string fields directly to `Decimal(str(...))`; configure pydantic strict mode / custom validators for monetary fields; never round-trip through `float`. |
| `vectorbt`, `backtesting.py`, `backtrader` as the M2 simulator's fill-model source of truth | All three assume simplified, same-bar-or-naive-next-bar fills with no gap rule or stop-limit lifecycle — exactly the "same-candle execution bias" class of defect this project exists to fix. | Custom event-driven simulator (see verdict above). |
| `nautilus_trader` as a dependency for this milestone | Real execution-realism engineering, but its temporal-separation guarantee for **bar-based** backtests is still an open feature request upstream, it has no Bithumb legacy-stop-limit concept, and its integration cost (instrument/venue model, Rust toolchain) isn't repaid at single-asset/single-venue scale. | Custom simulator now; revisit Nautilus only if the project scales to multi-venue/live-latency concerns post-v1. |
| `mlfinlab` (free/community fork) | Unmaintained since ~2018; the maintained successor is closed-source/paid. Depending on it for DSR/PBO would tie the project's most safety-critical statistic to unmaintained code of unknown current correctness. | In-repo DSR/PSR implementation, tested against the source paper; `arch` for bootstrap/SPA; `pypbo` only as a design reference for PBO/CSCV. |
| Unverified new statistics packages (`sharpebench`, `quant-harness`) as the sole/authoritative DSR/PBO implementation | Found in this research pass but with no evidence of adoption, citation, or audited correctness against the Bailey & López de Prado formulas. | Implement the formulas yourself (they're closed-form and well-specified); use these only as an optional cross-check, never as the only implementation of an accept/reject gate. |
| `ccxt` for Bithumb integration | Lowest-common-denominator multi-exchange abstraction; will not expose Bithumb's legacy stop-limit endpoint or the dual-auth-scheme requirement, and is a large, frequently-updated dependency for a single-venue project. | Custom thin client on `httpx` + `websockets` + `PyJWT` + stdlib `hmac`. |
| Any wrapper library's request-signing code, used without reading its source first | Order-signing code is the highest-consequence code path in the project (it is what will eventually place real orders in M6B); trusting an unaudited third party here is the wrong place to save time. | Write and unit-test (known-answer vectors) your own JWT and legacy-HMAC signing functions. |
| `polars` / `duckdb` as day-one requirements | Solve a data-scale problem (multi-GB, 100M+ row) this project does not have in this milestone; adds dependency and conceptual surface with no measured benefit yet. | `pandas` + `pyarrow`/Parquet files + a manifest. Revisit only if a concrete performance complaint appears. |
| `py-moneyed`, `dinero`, or other money-type packages | Add a dependency and an abstraction layer over `decimal.Decimal` for a single-currency (KRW) domain whose actual hard problem is exchange-defined tick/step rounding, not multi-currency arithmetic — those packages solve a problem this project doesn't have. | `decimal.Decimal` plus a small project-local `Money`/`Qty` wrapper enforcing Bithumb's tick/step rounding rules. |
| Confusing `bithumb-pro`/`bithumbfutures` GitHub docs with the target venue | These describe Bithumb Global/Futures, a different API/product from the Korean KRW-spot `apidocs.bithumb.com` this project targets — copying auth code from there would silently implement the wrong signature scheme. | Use only `apidocs.bithumb.com` (and its `llms.txt` index) as the source of truth; verify base URLs explicitly at M1. |

---

## Stack Patterns by Variant

**If Gate-1 selects v2-only (no legacy stop-limit):**
- Skip the stdlib `hmac`/legacy-auth adapter entirely; only `PyJWT` is needed for auth.
- The M2 simulator's order-state machine simplifies to whichever v2-only protective-exit mechanism Gate-1 chose (client-side trigger → market/limit sell, or candle-close signal exit) — still custom, still no external backtest library covers it, but smaller than the full legacy state machine.

**If Gate-1 selects v2 + legacy stop-limit:**
- Build both auth adapters (`PyJWT` for JWT; stdlib `hmac`/`hashlib` for legacy `Api-Key`/`Api-Nonce`/`Api-Sign`) before M6A, per `docs/EXECUTION.md`.
- The M2 simulator must implement the full `watch → wait → done/cancel` state machine with the gap-below-limit-means-no-fill rule — this is the strongest reason no off-the-shelf backtest library is usable, independent of any other consideration in this document.

**If Gate-1 selects L2-fidelity simulation (cost model makes slippage a function of depth):**
- Add an order-book snapshot ingestion path (public WS or REST) alongside candles; store snapshots the same way as candles (immutable, Parquet + manifest, exact decimal price levels).
- No additional library needed beyond what's already listed — `websockets`/`httpx` cover collection, `pyarrow` covers storage.

**If candle-only fidelity is sufficient (Gate-1 decision):**
- Skip L2 collection entirely for the historical backtest; the observe-only calibration path in M2 (§3.5) still needs live book/trade observation via `websockets`, but only for the forward comparison, not as simulator input.

---

## Version Compatibility

| Package | Compatible With | Notes |
|---------|------------------|-------|
| `arch` | `numpy`, `scipy` | `arch` transitively pulls in both; pin all three together and re-lock via `uv` on upgrade rather than upgrading `arch` alone. |
| `pydantic` v2 | `pydantic-settings` | Use `pydantic-settings` (a separate package since pydantic v2) for env-based config loading of Gate-1/2/3 decisions and secrets — do not hand-roll `os.environ` parsing for typed config. |
| `pyarrow` decimal dtype | `pandas` | Pandas' native support for Arrow-backed decimal columns has historically been partial; **verify current pandas/pyarrow interop for `decimal128` round-tripping at build time** — string-encoding of exact price/qty columns is the safe fallback if friction appears. |
| `websockets` ≥16 | Python ≥3.10 | Confirmed minimum Python version in the library's own changelog; not a concern given the Python 3.12/3.13 recommendation above. |
| `mypy` + `pydantic` | `pydantic`'s mypy plugin | Enable the plugin in `pyproject.toml`/`mypy.ini` so `mypy` understands pydantic-generated `__init__` signatures and validators correctly. |

---

## Sources

- [`arch` docs — Time-series Bootstraps](https://arch.readthedocs.io/en/latest/bootstrap/timeseries-bootstraps.html) — StationaryBootstrap/CircularBlockBootstrap/optimal_block_length. MEDIUM-HIGH (official docs, fetched via web search)
- [`arch.bootstrap.multiple_comparison` docs](https://arch.readthedocs.io/en/latest/_modules/arch/bootstrap/multiple_comparison.html) — SPA (White's Reality Check / Hansen SPA) class. MEDIUM-HIGH
- [`nautilus_trader` GitHub issue #4063 — "Support next-bar-open execution for bar-based backtesting"](https://github.com/nautechsystems/nautilus_trader/issues/4063) — confirms the temporal-separation gap even in the most execution-realistic mainstream engine. MEDIUM
- [`nautilus_trader` backtest docs](https://nautilustrader.io/docs/latest/tutorials/backtest_fx_bars/) — bar `ts_event` = close-time convention. MEDIUM
- [`backtesting.py` alternatives doc](https://github.com/kernc/backtesting.py/blob/master/doc/alternatives.md) — framework landscape comparison. MEDIUM
- [`pypbo` (esvhd) GitHub](https://github.com/esvhd/pypbo) — PBO/CSCV reference implementation. MEDIUM
- [`sharpebench` PyPI page](https://pypi.org/project/sharpebench/) — newer bundled DSR/PBO/SPA package, unverified maturity. LOW — verify before any use
- [`apidocs.bithumb.com`](https://apidocs.bithumb.com/) and its `/v1/orders/chance` reference page — JWT auth (access_key/nonce/timestamp/query_hash claims). MEDIUM-HIGH — re-verify exact claim names/endpoint at M1
- [`bithumb-pro/bithumb.pro-official-api-docs`](https://github.com/bithumb-pro/bithumb.pro-official-api-docs) — confirmed to describe a **different** Bithumb product (Global/Futures), not this project's target venue. HIGH (as a "do not confuse" flag)
- [`python-bithumb` on PyPI](https://pypi.org/project/python-bithumb/) — release history through mid-2025. MEDIUM (recency confirmed; feature coverage not independently verified)
- [`websockets` changelog](https://websockets.readthedocs.io/en/latest/project/changelog.html) — `reconnect_delays` argument, v16.0 Jan 2026 release. MEDIUM-HIGH
- [Ruff v0.15.0 — Astral blog](https://astral.sh/blog/ruff-v0.15.0) and [Ruff GitHub](https://github.com/astral-sh/ruff) — 2025 Stack Overflow "most admired" tool citation. MEDIUM-HIGH
- [uv vs Poetry adoption/benchmark roundups (danilchenko.dev, cuttlesoft.com, others)](https://www.danilchenko.dev/posts/uv-vs-pip-vs-poetry/) — download-count and benchmark claims. LOW-MEDIUM (aggregator blog content, cross-checked across multiple independent write-ups reaching the same conclusion — treat the *direction* of the recommendation as solid, exact download figures as indicative only)
- [Polars vs pandas 2026 benchmarks (multiple aggregator sources)](https://www.danilchenko.dev/posts/polars-vs-pandas/) — used only to confirm Polars' advantage is at a data scale this project doesn't have. LOW-MEDIUM
- [mypy vs pyright comparison, pydevtools handbook](https://pydevtools.com/handbook/explanation/how-do-mypy-pyright-and-ty-compare/) — strict-mode behavior differences. MEDIUM
- [`structlog` Logging Best Practices docs](https://www.structlog.org/en/stable/logging-best-practices.html) — official docs. MEDIUM-HIGH
- [Hypothesis + pytest property-based testing guides](https://semaphore.io/blog/property-based-testing-python-hypothesis-pytest) — general pattern confirmation, not project-specific. LOW-MEDIUM

**Flagged for mandatory re-verification against live sources before/at build time (per the quality gate and `docs/RESEARCH.md`'s own caveat to never trust a third-party wrapper or this list blindly):**
1. Exact current base URLs and endpoint paths on `apidocs.bithumb.com` for JWT claim structure and the legacy `/trade/stop_limit` endpoint (if selected).
2. The legacy `Api-Sign` hash algorithm and parameter-ordering (this research pass found the *shape* of the scheme described secondhand, not a confirmed current first-party spec page).
3. Whether `python-bithumb` (youtube-jocoding) covers anything beyond public/basic-private v1-style endpoints — read its source before assuming any feature.
4. Current pinned versions for every package above via `uv add` / PyPI at the moment of M0 setup — the versions cited here are current as of this research pass (September 2026) but this document is not a lockfile.
5. Correctness of any in-repo DSR/PSR/PBO/CSCV implementation against Bailey & López de Prado's original worked examples before it is allowed to gate an accept/reject decision.

---
*Stack research for: Bithumb autotrading bot — build + validate milestone (no live trading)*
*Researched: 2026-09-08*
