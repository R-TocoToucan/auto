# Phase 1: Safety Foundation + Bithumb Spec Adapter - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-08
**Phase:** 1 - Safety Foundation + Bithumb Spec Adapter
**Areas discussed:** L2 source + simulator fidelity (Gate-1), Backtest order policy (Gate-1), M0/M1 implementation shape, Stop mechanism (Gate-1) + remaining Gate-1 confirmations, Operational-parameter split

---

## L2 source + simulator fidelity (Gate-1)

### Q1 — Depth-dependent slippage?

| Option | Description | Selected |
|--------|-------------|----------|
| No — candle-only fidelity (Recommended) | Slippage/impact = modeled+calibrated flat/spread bound per side (bp scenarios + conservative gap rule) not varying with intended quantity within sleeve limits. No L2 pipeline required. | ✓ (with clarifications) |
| Yes — L2-aware fidelity | Cost model reads live/historical L2 depth so marketable-cost bound scales with intended quantity. Requires L2 source, timestamp/sequence policy, clock-sync, min forward-collection period. | |
| Hybrid — candle-only historical + forward L2 validation window | Backtest runs on candle-only slippage model. Separate forward window collects L2 via WebSocket purely to validate (not calibrate) the candle-only bound. | |

**User's choice:** Candle-only historical for v1. Slippage NOT assumed quantity-independent within sleeve. M2 uses observe-only public book+trade to provisionally calibrate + validate conservative flat bp cost bounds before final holdout. Explicit applicability cap via max KRW notional and/or top-of-book depth fraction. Cap-exceeding intent → fail-closed or `unvalidated`. Multiple absolute-bp + total round-trip stress scenarios. Persistent L2 + quantity-dependent impact = deferred research extension, mandatory before size increase.

### Q2 — Cap definition

| Option | Description | Selected |
|--------|-------------|----------|
| Max KRW notional per order only | Single Decision Register value; static, cheap to check. | ✓ |
| Fraction of top-of-book depth only | Adaptive; shrinks in thin markets; needs fresh top-of-book at decision time. | |
| Both, min-of applies (Recommended) | Cap = min(max_notional_krw, frac × observed_top_liquidity). | |
| Rolling-window median depth fraction | Cap based on median top-of-book depth over rolling window. | |

**User's choice:** Max KRW notional per order only. `cap_type = absolute_krw_notional`. Predicate `intent.notional_krw <= max_validated_notional_krw`. Gate 1 freezes only the FORM of the cap; numeric value = Gate 2 responsibility. Separate future runtime liquidity guard for live phase.

### Q3 — Cap-exceeded behavior

| Option | Description | Selected |
|--------|-------------|----------|
| Fail closed everywhere (Recommended) | Backtest sim raises `CapExceeded` and aborts; live path refuses. | |
| Fail closed in backtest, label 'unvalidated' in observe-only calibration only | Backtest fail-closed; observe-only calibration may record cap-exceeding hypothetical orders as unvalidated to build the sample. | ✓ |
| Label 'unvalidated' and exclude from metrics | Simulator continues, flags each cap-exceeding trade unvalidated; E∧R excludes them. | |

**User's choice:** Option 2 with tightened semantics — position-sizing stage applies cap BEFORE constructing intent. Invariant violation → `CapExceeded`, abort, invalidate. Never silently clip / exclude / partial-evaluate. Observe-only calibration MAY evaluate preregistered hypothetical-notional grid ABOVE cap, stored separately, labeled `unvalidated_for_strategy_evaluation`, MUST NOT enter returns/metrics/selection/holdout. M6B+ live broker refuses over-cap.

### Q4 — Cap lifecycle

| Option | Description | Selected |
|--------|-------------|----------|
| Two-field: frozen ceiling + observation-derived active cap (Recommended) | Gate-1 ceiling, M2 active cap, active ≤ ceiling, both hashed. | |
| Single frozen value at Gate 1 | One number, frozen at Gate 1, cannot change. | |
| Deferred value, frozen schema | Field declared required with null sentinel; startup refuses until M2 populates; then locked. | ✓ |
| Rolling re-calibration (with change hashes) | Field re-calibrated on preregistered cadence; every change recorded. | |

**User's choice:** Deferred value, frozen schema. `max_validated_notional_krw: Decimal | null`; initial null. Gate 1 freezes field name+type, absolute-KRW-cap policy, calibration procedure, fail-closed behavior, and the rule that the numerical value is produced by M2 and frozen in Gate 2. Null does NOT prevent M0/M1/data collection/unit tests/dedicated M2 observe-only calibration; DOES prevent selection backtests, M4/M5 evaluation, holdout, mock-order submission outside test fixtures, live-order submission. `provisional_engineering_notional_krw = 100_000` for test fixtures + preregistered calibration grid ONLY. **Implies SAFE-02 restructuring: rename generic startup check into capability-scoped validation.**

---

## Backtest order policy (Gate-1)

### Q1 — Order surface

| Option | Description | Selected |
|--------|-------------|----------|
| Marketable-only (`price` buy / `market` sell) (Recommended) | Entries via `price`, exits via `market`. Matches candle-only conservative bounds. | ✓ |
| Marketable-in + limit-out with cancel-on-close | Entry marketable; protective exit as `limit` posted after entry, cancel-and-replace on each candle close if unfilled. | |
| Full v2 surface (limit + `price` + `market` + `best` + Post-Only) | Sim implements every v2 order type + Post-Only-turns-marketable-in-flight. | |
| Marketable-in only, exits via signal-on-close | No stop order at all; exits triggered by signal on candle close and filled at t+1 open. | |

**User's choice:** Marketable-only for M2/M4/M5 strategy execution. Signal from closed candle t → intent only after t closes. Entry ≥ t+1 open + frozen conservative buy-side cost bound; exit same rule EXCEPT already-active protective exit may trigger intrabar under separately-frozen stop policy. Market-buy sizing = KRW spend, acquired coin computed after fees + modeled cost. Market sell = coin qty, proceeds after sell-side fees + cost. Forbidden for M4/M5: limit entry, `best`, Post-Only, maker-fee assumptions, cancel-and-replace. Legacy stop-limit (if selected separately) = protective-exit FSM, NOT reclassified as normal strategy limit. M6A `BithumbBroker` still tests all v2 order types as adapter/integration coverage, not strategy surface.

### Q2 — Rounding + fee application order

| Option | Description | Selected |
|--------|-------------|----------|
| Fees first, then tick/step rounding, then invariant check (Recommended) | For entry: net_coin_qty = (spend − fee) / fill_price → round DOWN to qty step → recompute krw_used → assert ≤ spend and ≤ cap. | |
| Tick/step rounding first, then fees, then invariant check | Round notional to tick/step first, then subtract fees. | |
| Both paths tested via `hypothesis` property tests; pick whichever satisfies invariants | Codify the ordering that passes. | |

**User's choice:** None of Options 1–3 as written. Order-type-specific accounting pipeline; freeze invariants rather than assume universal order. Official Bithumb semantics: `price` market buy submits KRW total (no coin volume); `market` sell submits coin volume (no order price). Simulator must NOT derive+quantity-round market-buy coin amount before request construction. Common rules: all Decimal; never round UP a user-controlled spend/qty if it could exceed balance/sleeve/cap; property tests enforce invariants but do NOT decide undocumented exchange semantics; **M1 MUST verify amount unit, volume step, fee currency, and whether buy fee is reserved outside the submitted market-buy amount; store as hashed fixtures**. Detailed accounting pipelines for buy/sell + 7 invariants + 6 separate fields locked. Slippage embedded in conservative fill price NEVER charged again as separate cash debit. If M1 cannot determine fee-reservation/rounding semantics unambiguously → retain per-capability status and BLOCK strategy evaluation. Do NOT choose whichever ordering passes property tests. M6B will reconcile against actual trade_amount / trade_quantity / paid_fee / reserved_fee.

---

## M0/M1 implementation shape

### Q1 — Decision Register storage

| Option | Description | Selected |
|--------|-------------|----------|
| Single `decisions.toml` at repo root, loaded via pydantic-settings (Recommended) | One TOML with three tables `[gate_1]/[gate_2]/[gate_3]`. | |
| Python module `src/config/decisions.py` with typed `ResolvedConfig` class | Pure Python literals; no parse step. | |
| Split by gate: three files `gate_1.toml`, `gate_2.toml`, `gate_3.toml` | One file per gate; startup composes them. | ✓ |
| Single `decisions.yaml` | YAML instead of TOML. | |

**User's choice:** Split by gate — `config/decisions/gate1.toml`, `gate2.toml`, `gate3.toml`. Only `gate1.toml` created + populated in Phase 1; Gate 2/3 files created only after their respective human discussions + approvals (no guessed defaults). All three public, committed to git. Parse via stdlib `tomllib` (Py ≥3.11) or `tomli` then validate through immutable/frozen pydantic BaseModel schemas. Do NOT use BaseSettings as primary TOML parser. Capability-scoped validation loads only required gates per operation. Missing/malformed/unfrozen/hash-mismatched → fail closed. Per-gate provenance metadata (`schema_version`, `status`, `approved_at_utc`, `source_commit`, `research_spec_sha256`, `execution_spec_sha256`). Per-gate SHA-256; final `config_hash` = deterministic hash over canonical manifest of gates + other behavior-affecting config. No silent rewrite of a frozen gate; changes require amendment + human approval + before/after hashes + invalidation/holdout-burn behavior.

### Q2 — Secrets

| Option | Description | Selected |
|--------|-------------|----------|
| Env vars only, read via `pydantic-settings` `BaseSettings` (Recommended) | Startup reads env; zero secret files under repo control. | |
| `.env` file outside repo (e.g. `~/.config/bithumb-bot/secrets.env`) + `python-dotenv` | pydantic-settings reads external `.env` via `env_file`. | |
| OS `keyring` (Windows Credential Manager / macOS Keychain / Secret Service) | Secrets in OS credential store. | |
| Split: env vars in prod + `.env`-outside-repo in dev | Same loader; source depends on `APP_ENV`. | ✓ (with explicit source selection, not implicit APP_ENV) |

**User's choice:** Option 4 with EXPLICIT source selection (no implicit APP_ENV discovery). Prod/CI/headless: process env vars only via `pydantic_settings.BaseSettings` with `SecretStr` (NOT `pydantic.BaseSettings`; v2 moves settings to `pydantic-settings`). Local dev: external secrets file allowed ONLY when operator supplies `BITHUMB_BOT_SECRETS_FILE=<absolute outside-repo path>`. Never auto-search repo/cwd/parent/user profile. **Reject paths resolving inside git repo (incl. symlinks).** OS-appropriate external location (e.g. `%LOCALAPPDATA%\BithumbBot\secrets.env` on Windows). File-permission check where OS supports. Source precedence: (1) env vars → (2) explicitly configured outside-repo file → NO fallback. Reject ambiguous mixed config. Credential classes: `BITHUMB_ACCOUNT_READ_ACCESS_KEY`, `BITHUMB_ACCOUNT_READ_SECRET_KEY`, `BITHUMB_TRADE_ACCESS_KEY` (prohibited pre-M6B), `BITHUMB_TRADE_SECRET_KEY` (prohibited pre-M6B). Public paths no cred; account/read paths only account/read cred; pre-M6B application REJECTS trade cred if supplied (reports class only, never value). **No withdrawal credential or withdrawal-permission configuration anywhere.** Never print/serialize/hash/persist/expose. Logs report only credential class + presence/absence. Tests use dummy values. Authenticated capability startup fails closed when required cred absent/empty.

### Q3 — Static enforcement

| Option | Description | Selected |
|--------|-------------|----------|
| `importlinter` for imports + custom Python AST script for Decimal-lint (Recommended) | Import Linter forbidden contract in pyproject.toml + `scripts/lint_decimal_from_float.py` AST walker. | ✓ (with specific corrections) |
| Single custom Python AST script for both | One script enforces both rules; no third-party dep. | |
| `importlinter` + custom `ruff` plugin | Ruff supports custom rules via plugin interface. | |
| pytest tests that grep+parse the tree | Runtime-checked, pytest tests fail on violation. | |

**User's choice:** Option 1 with corrections. Import Linter forbidden contract in `pyproject.toml`; actual package name after Phase-1 layout fixed; package must be importable when `lint-imports` runs; NO ignore rules; unavoidable exception → human approval. Decimal AST checker recognizes at minimum: `Decimal(0.1)`, `Decimal(-0.1)`, `Decimal(+0.1)`, `decimal.Decimal(0.1)`, `dec.Decimal(0.1)`, `D(0.1)` (aliases imported from stdlib `decimal`). Must NOT flag `Decimal("0.1")` / `Decimal(1)` / `Decimal("-0.1")`. Statically identifiable float literals only; no runtime type inference. Both `lint-imports` and Decimal AST checker in pre-commit AND mandatory CI (pre-commit alone not enforcement, can be skipped). Both return nonzero on violations. Positive+negative fixture tests for AST checker. Architectural-violation fixture proves Import Linter contract fails. Pin tool versions in lockfile. Include both in single documented quality command. **No custom Ruff plugin** (ruff continues for built-in rules).

### Q4 — Spec snapshot artifact + verification workflow

| Option | Description | Selected |
|--------|-------------|----------|
| JSON body + SHA-256 sidecar + recorded-response replay (Recommended) | `artifacts/spec_snapshot/<market>/<ISO_utc>.json` + `.sha256`; canonical JSON; spike records raw responses to `tests/fixtures/`; per-fact `VERIFICATION.md`. | ✓ (with detailed corrections) |
| Single JSON with embedded `content_hash` field (no sidecar) | One file; hash embedded. | |
| Parquet snapshot + manifest (mirrors M2 candle store) | Snapshot as Parquet + manifest. | |
| Split: committed JSON fixture in repo + separate runtime hashed snapshot | Fixture for tests; runtime snapshot fetched fresh. | |

**User's choice:** Option 1 with major corrections. Windows-safe UTC timestamps (no colons): `artifacts/spec_snapshots/KRW-BTC/YYYYMMDDTHHMMSSZ.json` + `.sha256`. Sanitized fixtures at `tests/fixtures/bithumb/sanitized/<endpoint>/…`. Verification bundle at `verification/bithumb/YYYYMMDDTHHMMSSZ/VERIFICATION.md` + `manifest.json`. Canonical snapshot fields specified (all decimals as strings). Canonical JSON: UTF-8, sorted keys, compact separators, exact trailing-newline policy; hash exact canonical bytes; sidecar `<64-char-sha>  <filename>`; atomic write via temp + same-fs replace; never overwrite consumed snapshot. Security: never commit raw authenticated responses without inspection; strip auth headers/cookies/signatures/nonces/access keys/account IDs/balances/etc; prefer schema allowlist over blacklist redact; raw responses outside repo, deleted post-review; automated secret+account-data checks on committed fixtures. Verification workflow: minimum endpoint-specific requests only; per-fact records exact claim, status (confirmed|contradicted|unresolved), doc URL + access ts, endpoint tested, sanitized fixture path + SHA-256, observed result, effect on implementation, remaining limitation, user approval status; docs statement ≠ observed API response. Offline tests: CI never calls Bithumb, replays sanitized fixtures only; simulator accepts snapshot only when sidecar hash + schema + market + required verification statuses pass; every experiment records path + SHA-256 consumed. **Capability-specific status fields** (not blanket `unresolved_adapter_mode`): `general_fee_rate: confirmed_read_only`, `market_buy_fee_reservation: provisional_documented`, `rounding_rejection_behavior: unresolved_until_M6B`, `live_order_acceptance: unresolved_until_M6B`. Unresolved live-only fact blocks live but must not unnecessarily block conservative simulator path whose assumptions are recorded + stress-tested. Human-approved (not human-signed) unless crypto signature implemented. SHA-256 sidecar detects byte changes; not itself proof artifact came from Bithumb.

### Q5 — Capability CLI surface

| Option | Description | Selected |
|--------|-------------|----------|
| Single `bt` CLI with sub-verbs; each verb declares its capability requirements (Recommended) | Central registry; dispatcher validates before handler runs. | ✓ (with detailed corrections) |
| Per-milestone console scripts (`bt-m0`, `bt-m1`, `bt-m2`, …) as separate entry points | One executable per milestone. | |
| Library-level capability tokens; no CLI framework in Phase 1 | Guards as decorators + registry. | |
| Two entry points: `bt m0 selfcheck` + programmatic library for everything else in Phase 1 | Only M0 self-check as CLI in Phase 1. | |

**User's choice:** Option 1 with corrections. Central capability registry; dispatcher validates all prerequisites before invoking handler or producing side effect. Sensitive service functions repeat capability check internally (defence in depth). `core/` defines capability types + validation results but never imports `broker/` or the CLI adapter. Help + version commands require no project capability and never load credentials. Phase-1 commands: `bt config validate --through gate1`, `bt m0 selfcheck`, `bt m1 fetch-spec --market KRW-BTC`, `bt m1 verify-facts --bundle <verification-bundle>`, `bt m1 verify-snapshot --snapshot <snapshot-path>`. Future reserved (not implemented in Phase 1): `bt m2 collect-observations`, `bt m2 calibrate-costs`, `bt m2 replay-known-answer`, `bt m4 evaluate-selection`, `bt m5 evaluate-module`, `bt freeze strategy`, `bt m6a verify-mock-broker`, `bt freeze final`, `bt holdout evaluate`. Do NOT call M4 selection "m2 backtest-selection". No M6B/live command in v1 scope. Guard matrix locked (see CONTEXT.md D-88). Semantics: `config validate` may inspect unfrozen candidate but never marks it approved; null cap allowed only for observation/calibration/test-fixtures; every pre-M6B command fails if trade-cred env vars present; account/read creds loaded only by the M1 network op needing them; `holdout evaluate` requires one-time non-persistent human authorization beyond gate/hash checks; unknown commands fail; empty capability defaults prohibited. Phase discipline: only Phase-1 handlers implemented; future verbs documented + covered by registry/schema tests but must NOT be scaffolded as if functional.

---

## Stop mechanism (Gate-1) + remaining Gate-1 confirmations

### Q1 — Stop family

| Option | Description | Selected |
|--------|-------------|----------|
| v2-only — client-side trigger → marketable sell (Recommended) | Bot subscribes to public trade WS, detects trigger, submits v2 market sell. | ✓ (with detailed corrections) |
| v2 + legacy `/trade/stop_limit` for the protective exit | Exchange-side stop-limit; M2 builds `watch→wait→done/cancel` FSM; M6A gains HMAC adapter; BRK-03 dual idempotency. | |
| v2-only — candle-close signal exit (no intrabar protective stop) | No protective stop placed; exit only at signal cadence. | |
| v2-only — no protective stop at all | Position held until reverse signal or max-DD kill-switch. | |

**User's choice:** Option 1 for v1. No legacy `/trade/stop_limit`; no `watch→wait→done/cancel` FSM in M2; no legacy HMAC in M6A; BRK-03 uses only v2 `client_order_id` intent/reconcile-before-retry. Legacy-stop-specific fees + nonce = deferred research alternative. Do NOT interpret v2 APIs that can query watch orders as proof v2 order-creation supports protective stops. Protective exit lifecycle: persisted intent → observe trigger → create exactly one v2 market-sell intent → submit with unique client_order_id → reconcile before retry → verify fills + residual → repeat only for confirmed residual. Explicitly NOT exchange-guaranteed; failure model includes bot crash, WS/data disconnect/stale, exchange REST rejection/timeout, ambiguous response, partial fill, private-stream delay/loss, watchdog failure, shared-host/network failure. Independent watchdog uses persisted position + stop state; doesn't depend on main loop; timeout never causes unconditional duplicate sell. M2 models financial stop/gap behavior conservatively + includes separate operational stress cases (trigger delayed/missed); normal backtest stop fill != evidence operational trigger is reliable. Adding legacy stop-limit later = material change under holdout-burn.

### Q2 — Trigger source

| Option | Description | Selected |
|--------|-------------|----------|
| Public trade-stream WS (last-trade price) + REST-tick fallback (Recommended) | Trigger against last executed trade from public v2 WS; REST ticker fallback on WS drop. | ✓ (with corrections) |
| Public ticker WS (best-bid or mid) + REST-tick fallback | Trigger on best-bid or midpoint. | |
| Candle-close price only (WS or REST) | Trigger only at close of each 4h candle. | |
| REST polling only (no WS) | Poll `/v1/ticker` at preregistered cadence. | |

**User's choice:** Option 1 general architecture with corrections. Primary trigger source = **Bithumb Public WebSocket v1 trade stream** (`wss://ws-api.bithumb.com/websocket/v1`, `type=trade`, `isOnlyRealtime=true`). Bithumb's official docs assign public trade to WebSocket **v1** and reserve WebSocket **v2** for private `myOrder`/`myAsset`. Trigger compares frozen stop threshold against `trade_price`. Record both exchange `trade_timestamp` and local monotonic receipt time. Dedupe using `sequential_id`, but do NOT assume it establishes message order (per official docs). Fallback source = `GET /v1/trades/ticks` (matching semantics — executed trades, not ticker snapshot). Ticker REST may be used only as secondary diagnostic context. Fallback must retain last processed trade timestamp+ID, dedupe after disconnect, inspect all recoverable trades after last confirmed event for crossing, distinguish complete recovered interval from unrecoverable gap, never assume `sequential_id` ordering, attach `trigger_source = "public_ws_trade" | "rest_trade_fallback"` to trigger decision (not to exchange fill). Degraded-state: mark primary stream stale, prohibit new entries immediately, switch to REST fallback within separately-rate-limited channel, keep watchdog active, emit high-priority alert, continue reconciliation + reconnect attempts. Explicit deferral of degraded-state policy to Q3. Endpoint/version facts recorded as build-time verified facts in M1; Public WS v1 and Private WS v2 remain separate adapter configurations.

### Q3 — Degraded-state per-scenario policy

| Option | Description | Selected |
|--------|-------------|----------|
| Conservative-hold policy (Recommended) | (1) WS stale + REST fresh + complete → triggers proceed via REST; entries prohibited. (2) WS stale + REST fresh unreconstructible → HALT. (3) All market-data stale + order API reachable → HALT + retain exposure. (4) All exchange paths unavailable → HALT. (5) Recovery preflight before resuming. Philosophy: never sell on incomplete info. | |
| Aggressive-exit policy | Same as A for (1). (2)-(3) submit protective market sell on best-available. (4) HALT. (5) force exit on any hint of crossing. Philosophy: prefer known operational loss to unknown market loss. | |
| Split by scenario — explicit per-case decision required | Reject blanket policy; decide each scenario individually. | ✓ |
| Halt-on-any-degradation | Any transition to degraded = halt strategy loop; no automated submission. | |

**User's choice:** Split by scenario. Distinguish uncertainty-about-crossing from complete-loss-of-market-info. Do NOT use "hold" as synonym for "conservative". Locked policies for all 5 scenarios (see CONTEXT.md D-20…D-24). Shared rules: monotonic stop transitions armed → triggered/uncertain → exit_pending → reconciled; at most one unresolved protective-exit intent per position; every timeout requires reconciliation before retry; protective exit qty = confirmed residual balance; partial fills → reconciliation + new intent for confirmed residual only; new entries prohibited throughout degraded/uncertain/recovery states; watchdog executes same frozen policy (no discretionary decisions). Numerical thresholds frozen separately before implementation. Policy accepts possible unnecessary liquidation when trigger interval cannot be reconstructed, but no blind market sell while every current market-data source is unavailable.

### Q4 — Watchdog + transactional exclusion

| Option | Description | Selected |
|--------|-------------|----------|
| Separate OS process + durable state file with an OS advisory file lock (Recommended) | State file protected by fcntl.flock (POSIX) / msvcrt.locking (Windows). | |
| Separate OS process + SQLite `BEGIN IMMEDIATE` on a shared state DB | Shared SQLite file; writer uses BEGIN IMMEDIATE for exclusive write lock. | ✓ (with detailed corrections) |
| Same-process, thread-isolated watchdog + `threading.Lock` on the intent | Watchdog as daemon thread in same process. | |
| Separate process + external leader election (`etcd` / `redis-lock`) | Formal leader election via external service. | |

**User's choice:** Option 2. sqlite3 stdlib (no pip dep). No custom JSON WAL or advisory file lock for correctness. Storage: `state/runtime.sqlite3` — local filesystem only (not network share / cloud-sync / removable), git-ignored, restrictive OS perms, WAL mode, `PRAGMA synchronous=FULL`, busy timeout, foreign keys, schema version recorded, no secrets in DB. Process model: main + watchdog as separate OS processes, same state-service library + frozen state machine, neither permanently owner, watchdog operational if main crashes; process independence ONLY (not full infrastructure independence). Tables: `positions`, `protective_exit_intents`, `ownership_leases`, `broker_observations`, `audit_events`, `schema_metadata`. Stable app-generated `intent_id` + deterministic v2 `client_order_id`. Decimals as canonical text, never REAL. DB-level invariants (unique partial index for single-unresolved-intent per position, monotonic transitions, unique intent_id + client_order_id, append-only audit event IDs). Full claim protocol locked (BEGIN IMMEDIATE → read → reconcile → claim → commit BEFORE network I/O → confirm epoch → submit at most one → persist result in new txn; never keep write txn open across HTTP). Lease takeover: expiry alone does NOT authorize replacement; watchdog reconciles balance/orders/events/client_order_id/prior evidence first; `submission_ambiguous` on unconfirmed prior submission; no unconditional duplicate sell; v2 reconcile-before-retry mandatory. Partial fills reconciliation + child attempt IDs, not new independent intents. Failure handling: DB corruption/schema mismatch/failed integrity → prohibit automated submission + critical alert; restart requires integrity + broker reconciliation; schema migration during unresolved intent prohibited without human authorization. Required tests enumerated (M6A against mock broker only, no real order).

### Q5 — Remaining Gate-1 items (5-item bulk approval)

| Option | Description | Selected |
|--------|-------------|----------|
| Approve all five as stated (Recommended) | Venue = Bithumb KRW spot; market = KRW-BTC; timeframe = 240m only; calibration = Option B; data source = Bithumb public REST/WS via custom thin client. | ✓ (with clarifications) |
| Approve venue+market+timeframe+Option B; revise data source | Change candle/trade acquisition. | |
| Approve venue+market+Option B+data source; revise timeframe | Change baseline timeframe. | |
| I want to revise multiple items | Revise multiple items. | |

**User's choice:** Approve all five with clarifications. Venue = Bithumb KRW spot only (no futures/margin/shorting/cross-exchange/non-KRW). Target market = KRW-BTC only (additional assets require Gate-1 amendment + applicable research invalidation). Timeframe = native 240-min candles only for v1 strategy evaluation; 6h aggregation utility permitted separately if EXECUTION.md requires but must NOT enter M4/M5 evaluation/parameter search/selection/holdout unless approved amendment before evaluation begins; if enabled, must aggregate from 60m/trades never 240m. Calibration = Option B; M2 observe-only provisional; no real order + no trade cred before M6B. Primary data source: historical strategy data via Bithumb public REST native 240-min candles through custom thin adapter; intrabar/test support via Bithumb public REST candles/trades; real-time trigger + forward observation via Public WS v1 trade; REST fallback `/v1/trades/ticks`; ticker REST diagnostic only; NO third-party historical in v1; pagination/timestamps/ordering/incomplete-current-candle/supported-intervals remain M1 build-time verification facts (adapter must not silently rely on undocumented behavior). Authority: `docs/EXECUTION.md` + `docs/RESEARCH.md` authoritative; generated `.planning/*` records/explains but does not authorize/override.

---

## Operational-parameter split

### Q1 — Handling of 7 stop-operational parameters

| Option | Description | Selected |
|--------|-------------|----------|
| Schema-freeze all 7 at Gate 1; freeze M1-required values now; defer M2/live values to Gate 2 with capability-CLI blocks (Recommended) | rest_request_timeout_ms=30000; rest_reconnect_policy defaults; defer max_stream_lag_ms / poll_hz / max_unverified_interval / stability / watchdog / reconnect to Gate 2. | |
| Freeze specific numeric values for all 7 now | Concrete numbers at Gate 1 for all 7; any change post-Gate-1 = material. | |
| Schema-freeze all 7 at Gate 1; defer ALL values to Gate 2 | Even M1 REST timeout waits for Gate 2. | |
| None of the above — revise | Different scheme. | ✓ |

**User's choice:** Option 4 with detailed scheme. Separate non-safety-critical M1 HTTP behavior from protective-exit operational behavior; assign numerical freezes according to when evidence and use become available. Gate 1 freezes schemas, units, qualitative policy, gate ownership. Do NOT use one generic `rest_request_timeout_ms` for both spec fetching and protective-stop fallback. Freeze at Gate 1 (M1 read-only HTTP only, MUST NOT be reused implicitly by protective-exit path): `m1_spec_http_connect_timeout_ms=5000`, `m1_spec_http_read_timeout_ms=15000`, `m1_spec_http_max_attempts=3`, `m1_spec_http_backoff_initial_ms=500`, `m1_spec_http_backoff_cap_ms=5000`, `m1_spec_http_jitter=full`. Retry semantics: only network errors / connect+read timeouts / HTTP 408 / HTTP 429 per Retry-After where valid / retryable 5xx; do NOT retry ordinary auth/permission/validation/non-retryable 4xx. Freeze at Gate 2 (numeric values after M1 rate-limit verification + M2 observation window): `max_received_trade_delivery_lag_ms`, `public_ws_transport_liveness_timeout_ms`, `fallback_rest_poll_interval_ms`, `trigger_rest_connect_timeout_ms`, `trigger_rest_read_timeout_ms`, `max_unverified_interval_ms`, `ws_recovery_stability_window_ms`, `ws_recovery_min_valid_events`. Split `max_stream_lag_ms` name into two (delivery lag ≠ absence of trades on healthy connection); transport liveness uses heartbeat/PING-PONG + fallback checks. Any M4/M5 evaluation requires these Gate-2 non-null + frozen. Freeze at Gate 3 (schema defined at Gate 1, values at Gate 3 before holdout): `watchdog_heartbeat_interval_ms`, `watchdog_lease_ttl_ms`, `ws_reconnect_backoff_initial_ms`, `ws_reconnect_backoff_cap_ms`, `ws_reconnect_jitter_policy`, `per_reconnect_cycle_attempt_limit`, `reconnect_circuit_breaker_window_ms`. Required invariants: `heartbeat < lease_ttl`; `lease_ttl >= 3 * heartbeat`; `lease_ttl > worst_case_in_flight_request_window + persistence_margin`; lease expiry never authorizes duplicate; takeover still requires broker reconciliation. M6A tests parameterized implementation via explicit fixture values; must not pretend fixtures are approved live settings. **M2 observation-only provisional profile** (break null-value circularity): `profile_status = provisional_observation_only`; every provisional value + hash recorded in observation artifact; permitted for public observation collection / reconnect experiments / latency measurement / deterministic mock tests; PROHIBITED for M4/M5 evaluation / final freeze / holdout / live. M2 collector therefore NOT blocked by null final values; evaluation + operational capabilities remain blocked. Capability effects table (M1 fetch/verify requires Gate-1 M1 HTTP values; M2 observation collection permits recorded provisional profile; M2 deterministic simulator tests permit explicit fixture parameters; M4/M5 requires Gate-2; M6A mock watchdog tests permit explicit test fixtures no live claim; final holdout requires Gate 1+2+3+relevant hashes; M6B+ additionally requires live authorization + successfully verified credentials). Reconnect semantics: per-call/per-cycle attempt limit does NOT permanently stop protection; exhaustion opens circuit breaker, persists degraded, alerts, begins later bounded retry under frozen policy; must not spin indefinitely or exceed verified channel limits.

---

## Final consistency review

| Option | Description | Selected |
|--------|-------------|----------|
| Approve — write CONTEXT.md + DISCUSSION-LOG.md and commit (Recommended) | All 16 rows resolved; cross-cutting confirmations locked; capability-status contradiction corrected; requirement clarifications noted. | ✓ |
| Request specific revisions | Something in a specific row (G1.X) needs revision. | |
| Abort — do not write CONTEXT.md | Stop workflow; leave checkpoint; no commit. | |

**User's choice:** Approved. Write CONTEXT.md + DISCUSSION-LOG.md and commit.

---

## Claude's Discretion

- Repo directory layout final naming (planner discretion within `core/`↔`broker/` boundary; Import Linter contract needs the actual package name).
- Risk-denominator vocabulary implementation form (typed value objects vs docs + naming convention; SAFE-07 requires "documented" minimum).
- Per-channel token-bucket implementation details (asyncio-native vs threading; numeric values are M1 build-time verification items).
- `tests/` layout and hypothesis strategies (pytest + hypothesis are the fixed stack; layout is planner discretion).

## Deferred Ideas

- Full L2-aware simulator + quantity-dependent impact model (mandatory before increasing order size beyond validated cap).
- Persistent sequenced L2 pipeline.
- Live-phase runtime liquidity guard using fresh order book (distinct from historical-model cap; M6B+).
- Legacy `/trade/stop_limit` + HMAC adapter + `watch→wait→done/cancel` FSM + legacy intent-log idempotency (adding later = material change under holdout-burn rules).
- Numeric values for 8 Gate-2 stop-operational parameters (frozen Phase 3).
- Numeric values for 7 Gate-3 stop-operational parameters (frozen Phase 5, before holdout).
- Per-channel numeric rate-limit values (M1 build-time verification items).
