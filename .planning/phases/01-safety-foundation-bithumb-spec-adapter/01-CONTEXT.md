# Phase 1: Safety Foundation + Bithumb Spec Adapter - Context

**Gathered:** 2026-09-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 1 delivers the M0 + M1 slice of the milestone spine:

1. **Fail-closed safety rails** — an immutable, per-gate Decision Register (Gate-1 populated in this phase; Gate-2 in Phase 3; Gate-3 in Phase 5), capability-scoped startup validation (SAFE-02 restructured), three-class API key policy with withdrawal permanently disabled, exact-decimal money type enforcement, CI-enforced `core/` ↔ `broker/` import boundary.
2. **Authenticated read-only Bithumb spec/fee adapter (M1)** — using the account/read JWT key, produces a canonical, sanitized, SHA-256-hashed spec snapshot (fee rates, tick/step, min-order, supported order types) with a companion `VERIFICATION.md` bundle attesting the five build-time facts against `apidocs.bithumb.com`. Adapter uses per-capability status fields, not a single `unresolved_adapter_mode`; the simulator consumes the snapshot and never re-queries mid-simulation.
3. **Single `bt` CLI** with central capability registry + guard matrix; Phase-1 verbs only (`config validate --through gate1`, `m0 selfcheck`, `m1 fetch-spec`, `m1 verify-facts`, `m1 verify-snapshot`). Future verbs reserved in the registry + covered by registry/schema tests; handlers must not be scaffolded as if functional.

The complete Gate-1 Decision Register is frozen in this phase (see `<decisions>` § Gate-1 items). No trade-permission credential is ever configured. No real order occurs. `docs/EXECUTION.md` + `docs/RESEARCH.md` are the authoritative source of Gate decisions; the `.planning/` generated docs record and explain but do not authorize or override them.

</domain>

<decisions>
## Implementation Decisions

Every decision below is either **frozen** at Gate 1, **schema_frozen/value_deferred** to a named later gate, or a **test-fixture-only** provisional. Numeric values are never invented at Gate 1 unless explicitly listed. Downstream agents may not silently choose behavior-affecting safety values.

### G1.1 — Venue
- **D-01:** Bithumb KRW spot only. **Prohibited alternatives:** futures, margin, shorting, cross-exchange execution, non-KRW markets (including any `bithumb-pro` / Bithumb Global / Bithumb Futures endpoints — different product, different auth).
- **Status:** frozen.

### G1.2 — Target market(s)
- **D-02:** KRW-BTC only. Additional assets require a Gate-1 amendment plus, once evaluation has begun, the applicable research invalidation rules.
- **Status:** frozen.

### G1.3 — Timeframe (strategy evaluation)
- **D-03:** Native Bithumb 240-minute (4h) candles only for v1 strategy evaluation. A separately tested generic 6h aggregation utility is permitted for utility/tests only; it must aggregate from 60m or trades, never 240m. 6h must NOT enter M4/M5 candidate evaluation, parameter search, selection metrics, or the final holdout unless approved via amendment before evaluation begins.
- **Status:** frozen. (24/7 annualization convention → Gate 2, Phase 3.)

### G1.4 — Calibration option
- **D-04:** Option B — all real orders in M6B; M2 calibration is observe-only and provisional. No trade-permission credential before M6B, unconditionally, across all documents and every code path.
- **Status:** frozen.

### G1.5 — Historical candle/trade data source
- **D-05:** Bithumb public REST via the custom thin adapter. Historical strategy data = Bithumb public REST native 240-minute candles. Intrabar/test support where required = Bithumb public REST candles/trades. **No third-party historical dataset in v1.** Adapter must not silently rely on undocumented behavior.
- **Status:** frozen. Adapter parameters (pagination limits, response ordering, candle timestamps, incomplete-current-candle handling, supported intervals) are M1 build-time verification facts (schema frozen; values recorded in the M1 `VERIFICATION.md` bundle + spec snapshot).

### G1.6 — Real-time trigger source
- **D-06:** `trigger_price = last executed trade price`. Primary source = **Bithumb Public WebSocket v1 trade stream** (`wss://ws-api.bithumb.com/websocket/v1`, `type=trade`, `isOnlyRealtime=true`). Adapter records both exchange `trade_timestamp` and local monotonic receipt time. `sequential_id` for dedup only (does not establish order per official docs). Public WS is **v1**; private WS is **v2** — the two must remain separate adapter configurations.
- **Status:** frozen (endpoint version + type + trigger metric). Operational timing → Gate 2 (see G1.10).

### G1.7 — REST fallback source (trigger channel)
- **D-07:** `GET /v1/trades/ticks` with `market=KRW-BTC`. **`GET /v1/ticker` = diagnostic only** (never the primary last-trade trigger source). Fallback must retain last processed trade timestamp + ID, dedupe trades after disconnect, inspect all recoverable trades after last confirmed event for a stop crossing, distinguish complete recovered interval from unrecoverable history gap, never assume `sequential_id` ordering, and attach `trigger_source = "public_ws_trade"` or `"rest_trade_fallback"` to the trigger decision (never to the exchange fill).
- **Status:** frozen (endpoint choice + role). Operational timing → Gate 2 (see G1.10).

### G1.8 — Simulator fidelity
- **D-08:** Candle-only historical fidelity for v1. Slippage is NOT assumed quantity-independent within sleeve; it is constrained by a max KRW-notional applicability cap (G1.9). Observe-only public book+trade calibration in M2 (no orders); multiple absolute-bp + total round-trip stress scenarios; record observation period + markets + timestamps + spread distribution + conservative bound-selection rule. **Prohibited:** full L2-aware simulator, persistent sequenced L2 pipeline, quantity-dependent impact model.
- **Status:** frozen. (L2-aware pieces = deferred research extensions, mandatory before increasing order size beyond validated cap.)

### G1.9 — Applicability-cap type + lifecycle + cap-exceeded behavior
- **D-09:** `cap_type = absolute_krw_notional`. Field: `max_validated_notional_krw: Decimal | null` — initial `null`; numeric value derived by M2's preregistered observe-only calibration and **frozen numerically at Gate 2** before evaluating any candidate or opening holdout. `provisional_engineering_notional_krw = 100_000` — test-fixture + preregistered hypothetical calibration grid ONLY; never a validated cap; never used for final strategy evaluation; never carried into holdout.
- **D-10:** Sizing stage applies `max_validated_notional_krw` BEFORE constructing the final order intent. Cap normally acts as a sizing constraint.
- **D-11:** Invariant `final_intent.notional_krw <= max_validated_notional_krw`. Violation → raise `CapExceeded`, abort the run, invalidate the run. Never silently clip an already-created intent; never exclude the trade from metrics; never partial-evaluate.
- **D-12:** Observe-only M2 calibration MAY evaluate a preregistered grid of hypothetical notionals ABOVE the cap; stored separately, labeled `unvalidated_for_strategy_evaluation`, and NEVER enters strategy returns, benchmark returns, E/R metrics, parameter selection, or the holdout.
- **D-13:** After Gate 2, value must not roll or update automatically. Change before final holdout → invalidate affected research + restart from appropriate protocol stage. Change after final artifact freeze = material. Change after holdout opened = burns holdout + forward replacement required.
- **D-14:** A separate future runtime liquidity guard using a fresh order book is required for live phase (M6B+), distinct from the historical-model cap; must fail closed on stale/unavailable snapshot. **Status:** schema deferred to M6B project.
- **Status:** `cap_type` = frozen; `max_validated_notional_krw` = schema_frozen/value_deferred to Gate 2; `provisional_engineering_notional_krw` = frozen (test-only value).

### G1.10 — Stop mechanism family + degraded-data policy + main/watchdog ownership + operational-parameter split

**Family**
- **D-15:** v2-only client-side trigger → v2 `market` sell. Persisted protective-stop intent → client observes trigger → create exactly one v2 market-sell intent → submit with unique `client_order_id` → reconcile before any retry → verify fills + residual position → repeat only for confirmed residual quantity.
- **D-16:** Explicitly NOT an exchange-guaranteed stop. Failure model includes: bot process crash; public market-data disconnect / stale data; exchange REST rejection or timeout; ambiguous submission response; partial fill; private-stream delay or loss; watchdog failure; shared-host or shared-network failure.
- **D-17:** M2 does NOT implement the legacy `watch → wait → done/cancel` stop-limit FSM. M6A does NOT implement the legacy `Api-Key`/`Api-Nonce`/`Api-Sign` HMAC scheme. BRK-03 uses only the v2 `client_order_id` intent/reconcile-before-retry protocol in v1. Legacy stop-limit + legacy fee + nonce behavior remain a deferred research alternative.
- **D-18:** Do not interpret v2 APIs that can query watch orders as proof that the ordinary v2 order-creation endpoint supports a protective stop order. Record this distinction in verification evidence.
- **D-19:** Adding a legacy stop-limit or another exchange-side mechanism later changes exit + failure behavior → **material strategy/execution change** requiring revalidation under the holdout-burn rules.

**Degraded-data scenario policy** (all five frozen)
- **D-20:** *Scenario 1 — WS stale + REST fresh + interval fully reconstructed:* degraded mode; prohibit new entries; evaluate every recovered trade after last confirmed event; if any crossed the stop → persist one triggered exit intent + submit v2 market sell for confirmed residual; else continue protection via REST fallback. Do not resume normal until WS passes the separately defined stability check.
- **D-21:** *Scenario 2 — WS stale + REST fresh + interval NOT fully reconstructible:* `trigger_uncertain`; prohibit entries + strategy actions; if current fresh executed-trade price ≤ stop → submit protective market sell immediately; else bounded reconstruction attempts only until preregistered `max_unverified_interval_ms`; if still incomplete → treat protection contract as potentially breached → emergency market sell for confirmed residual, `exit_reason = unrecoverable_trigger_gap`. Accepts possible unnecessary exit in preference to silent unbounded exposure.
- **D-22:** *Scenario 3 — All market-data sources stale, private/order API reachable:* NO immediate blind market sell; prohibit entries + discretionary actions; persist degraded state; continuous alerts; retry independent public sources via separately-rate-limited connections; main + watchdog must not both create exit intents (transactional ownership); when any fresh executed-trade source returns → apply Scenario 2. Do not describe an order as sold "at the last known price" (v2 market sell doesn't specify price).
- **D-23:** *Scenario 4 — Market data + exchange order paths both unavailable:* persist `exchange_unreachable`; prohibit every new action except reconnect / reconciliation / durable logging / alerts; do not manufacture a successful cancellation or exit state; bounded backoff with jitter; preserve last confirmed position + stop + intent state; report position as exposed + protective mechanism unavailable.
- **D-24:** *Scenario 5 — Recovery after unobserved interval:* mandatory recovery reconciliation (balances / open orders / previous exit intents / fills / trades since last confirmed event / completeness / confirmed residual position) before normal strategy processing; complete + crossed → submit/reconcile protective sell; complete + no crossing → restore protection, resume only after stability check; incomplete + fresh price ≤ stop → sell immediately; incomplete + fresh price > stop → Scenario 2 bounded deadline; no fresh market data → remain in Scenario 3 or 4.

**Shared rules**
- **D-25:** Stop transitions monotonic `armed → triggered/uncertain → exit_pending → reconciled`; reconnects cannot silently revert to `armed`.
- **D-26:** At most one unresolved protective-exit intent per position (enforced at DB level).
- **D-27:** Every timeout requires reconciliation before retry. A timeout never triggers an unconditional retry.
- **D-28:** Protective exit quantity = confirmed residual balance (not originally requested entry qty). Partial fills → reconciliation + new intent for confirmed residual only.
- **D-29:** New entries prohibited throughout degraded, uncertain, and recovery states.
- **D-30:** Watchdog executes the same frozen policy; no discretionary "seems warranted" decisions.

**Main/watchdog ownership model**
- **D-31:** Separate OS processes (main strategy + independent watchdog) sharing a local SQLite state DB `state/runtime.sqlite3`. Local filesystem only — NOT a network share, cloud-synchronized directory, or removable drive. Directory git-ignored. Restrictive OS permissions. WAL mode. `PRAGMA synchronous = FULL`. Configured busy timeout. Foreign keys enabled. Schema version recorded. Secrets never stored in the database. Provides **process independence only** — does not protect against shared OS, host, disk, power, or network failure.
- **D-32:** Same state-service library + same frozen state machine in both processes. Neither process is permanently the exit owner. Watchdog must remain operational if the main strategy loop crashes.
- **D-33:** Durable tables (conceptual): `positions`, `protective_exit_intents`, `ownership_leases`, `broker_observations`, `audit_events`, `schema_metadata`. Stable application-generated `intent_id` and deterministic v2 `client_order_id`. Decimal values stored as canonical text, never SQLite REAL.
- **D-34:** DB-level invariants: at most one unresolved protective-exit intent per position (unique partial index or equivalent); monotonic allowed state transitions; unique `intent_id`; unique `client_order_id`; append-only audit-event identifiers.
- **D-35:** Claim protocol: `BEGIN IMMEDIATE` → read position/intent/lease/last broker observations → reconcile intent state (submitted / filled / partially filled / rejected / ambiguous) → if eligible, claim by writing `owner_instance_id`, `lease_epoch`, `claimed_at`, `lease_expires_at`, `state` → commit BEFORE performing network I/O → immediately before submission confirm lease-epoch ownership → submit at most one request for the stable intent / client-order ID → persist response or ambiguity in a new transaction. Never keep a SQLite write transaction open while waiting for an exchange HTTP response.
- **D-36:** Lease takeover: expiration alone does NOT authorize a replacement order. Before the watchdog takes over an expired lease → reconcile current coin balance + confirmed residual position + open/pending orders + completed/cancelled orders + private order events + stable `client_order_id` + previous request/response evidence. If prior submission may have succeeded but cannot be confirmed → mark intent `submission_ambiguous`; do NOT issue an unconditional duplicate market sell. SQLite prevents local conflicting writes but cannot make DB update + exchange submission atomic → **v2 reconcile-before-retry is mandatory.**
- **D-37:** Partial fills: reconcile actual filled quantity first → calculate residual exit quantity from confirmed remaining balance → the original intent stays linked to all subsequent submission attempts + fills → new residual submission requires a new child attempt identifier but MUST NOT create a second independent protective-exit intent.
- **D-38:** Failure handling: DB corruption / schema mismatch / failed integrity check → prohibit automated submission + raise critical alert. On restart: DB integrity check + broker reconciliation before either process may claim an intent. Schema migration while a position or unresolved intent exists = prohibited without explicit human authorization.
- **D-39:** Required tests (M6A against mock broker only; no real order permitted): simultaneous main/watchdog claim race; main killed before claim commit; killed after claim commit but before HTTP submission; killed while request in flight; response received but persistence fails; expired lease with possibly-successful prior submission; partial fill followed by owner crash; SQLite busy/locked timeout; WAL recovery after forced termination; duplicate `client_order_id`; corrupted database and schema-version mismatch.

**Operational-parameter split**

- **D-40 (Gate 1 — frozen NOW, M1 read-only HTTP behavior only; MUST NOT be reused implicitly by the protective-exit path):**
  - `m1_spec_http_connect_timeout_ms = 5_000`
  - `m1_spec_http_read_timeout_ms = 15_000`
  - `m1_spec_http_max_attempts = 3`
  - `m1_spec_http_backoff_initial_ms = 500`
  - `m1_spec_http_backoff_cap_ms = 5_000`
  - `m1_spec_http_jitter = full`
  - Retry semantics: only network errors, connect/read timeouts, HTTP 408, HTTP 429 per `Retry-After` where valid, and retryable 5xx. Do NOT retry ordinary auth/permission/validation/other non-retryable 4xx failures.

- **D-41 (Gate 2 — schema frozen at Gate 1, numeric values frozen at Gate 2 in Phase 3 after M1 rate-limit verification + preregistered M2 observation window; any M4/M5 evaluation requires these non-null and frozen):**
  - `max_received_trade_delivery_lag_ms` (delivery lag of a received event)
  - `public_ws_transport_liveness_timeout_ms` (heartbeat / PING-PONG-based liveness; a quiet trade stream alone is NOT proof the WebSocket transport is dead)
  - `fallback_rest_poll_interval_ms`
  - `trigger_rest_connect_timeout_ms`
  - `trigger_rest_read_timeout_ms`
  - `max_unverified_interval_ms`
  - `ws_recovery_stability_window_ms`
  - `ws_recovery_min_valid_events`
  - Rationale for split of the old `max_stream_lag_ms` name into two: delivery lag of a received event ≠ absence of trades on an otherwise healthy connection. Transport liveness must use the connection-management heartbeat/PING-PONG behavior plus fallback checks.

- **D-42 (Gate 3 — schema frozen at Gate 1, numeric values frozen at Gate 3 in Phase 5 before holdout is opened):**
  - `watchdog_heartbeat_interval_ms`
  - `watchdog_lease_ttl_ms`
  - `ws_reconnect_backoff_initial_ms`
  - `ws_reconnect_backoff_cap_ms`
  - `ws_reconnect_jitter_policy`
  - `per_reconnect_cycle_attempt_limit`
  - `reconnect_circuit_breaker_window_ms`
  - Required invariants (config-loader-checked at load time):
    - `watchdog_heartbeat_interval_ms < watchdog_lease_ttl_ms`
    - `watchdog_lease_ttl_ms >= 3 * watchdog_heartbeat_interval_ms`
    - `watchdog_lease_ttl_ms > worst_case_in_flight_request_window_ms + persistence_margin_ms`
    - Lease expiry never authorizes a duplicate order; takeover still requires broker reconciliation.
  - M6A tests the parameterized implementation using explicit fixture values; it must NOT pretend those fixtures are approved live settings.

- **D-43 (M2 provisional-observation-only profile to break the null-value circularity):**
  - `profile_status = provisional_observation_only`; every provisional value + its hash recorded in the observation artifact.
  - Permitted uses: public observation collection; reconnect experiments; latency measurement; deterministic mock tests.
  - Prohibited uses: M4/M5 strategy evaluation; final freeze; holdout evaluation; live operation.
  - **The M2 collector is therefore NOT blocked by null final Gate-2/Gate-3 values.** Evaluation and operational capabilities remain blocked until their values are frozen.

- **D-44 (Reconnect semantics):** a per-call or per-cycle attempt limit does NOT mean the protection system permanently stops after that count. Exhaustion opens the circuit breaker, persists the degraded state, alerts, and begins a later bounded retry cycle under the frozen policy. Must not spin indefinitely or exceed verified channel limits.

**Status:** family + degraded policy + shared rules + ownership model + operational-parameter split + Gate-1 M1 HTTP values = frozen. Gate-2 operational values = schema_frozen/value_deferred to Gate 2. Gate-3 operational values = schema_frozen/value_deferred to Gate 3.

### G1.11 — Backtest order surface + buy/sell accounting + rounding policy
- **D-45:** Marketable-only surface (`price` buy / `market` sell) for M2/M4/M5 strategy execution. Signal from closed candle t → intent created only after t closes. Entry uses v2 `price` market buy, executes no earlier than t+1 open + frozen conservative buy-side cost bound. Exit uses v2 `market` sell with the same temporal-separation rule, EXCEPT an already-active protective exit may trigger intrabar under the frozen stop policy + gap rule (see G1.10).
- **D-46:** Prohibited for M4/M5 strategy candidates: `limit` entry, `best`, Post-Only, maker-fee assumptions, cancel-and-replace execution.
- **D-47:** Adapter/integration scope note — M6A `BithumbBroker` still tests v2 `limit` / `price` / `market` / `best` / Post-Only request construction as adapter/integration coverage; recorded as adapter surface, NOT strategy surface.
- **D-48:** Accounting pipeline is **order-type-specific**. Freeze invariants rather than assuming one universal rounding-vs-fees order. Official Bithumb semantics: `price` market buy submits a KRW total order amount (no coin volume); `market` sell submits coin volume (no order price). Simulator must NOT derive+quantity-round a market-buy coin amount before constructing the request.
- **D-49:** Common rules — all values `Decimal` (constructing `Decimal` from float prohibited per SAFE-05 and the Decimal AST checker); never round UP a user-controlled spend or sell quantity if doing so can exceed available balance, sleeve, or validated notional cap; property tests enforce invariants but do NOT decide undocumented exchange semantics; **M1 must verify the exchange's amount unit, volume step, fee currency, and whether the buy fee is reserved outside the submitted market-buy amount — store the verified responses as hashed fixtures.**
- **D-50:** Market buy (`order_type=price`): start from max total cash-debit budget → derive permitted submitted KRW order amount using M1-verified fee-reservation semantics → quantize submitted KRW amount DOWN to the exchange-supported KRW amount unit (NOT a limit-price tick) → simulate ≥1 fills at conservative executable prices, each modeled buy fill price quantized in the adverse direction (UPWARD) to a valid market tick → compute gross executed amount + gross acquired coin from the fills → compute fee from executed amount using verified buy-side fee rate + fee currency → derive net acquired coin + actual total cash debit. Do NOT pre-round a derived buy quantity to a volume step (no volume is submitted for `price` market buy). Asset-precision treatment applies to modeled fills + the resulting balance, based on verified venue behavior.
- **D-51:** Market sell (`order_type=market`): start from available sellable coin balance → quantize submitted volume DOWN to the verified volume step → simulate fills at conservative executable prices (sell fill prices quantized DOWNWARD to a valid market tick — adverse) → compute gross KRW proceeds from actual modeled fills → compute sell fee from executed amount using verified sell-side fee rate + fee currency → derive net KRW proceeds.
- **D-52:** Required invariants (all enforced):
  - `submitted_buy_amount_krw <= permitted_order_amount_krw`
  - `gross_executed_buy_notional_krw <= max_validated_notional_krw`
  - `total_buy_cash_debit_krw <= cash_budget_krw`
  - `total_buy_cash_debit_krw <= isolated_sleeve_available_krw`
  - `submitted_sell_volume <= available_sellable_volume`
  - `submitted_sell_volume` is volume-step valid
  - `net_proceeds_krw <= gross_proceeds_krw`
- **D-53:** Keep these definitions separate: `order_notional_krw`, `fee_krw`, `total_cash_debit_krw`, `net_acquired_coin`, `gross_proceeds_krw`, `net_proceeds_krw`. The slippage/impact adjustment embedded in the conservative fill price MUST NOT be charged again as a separate cash debit.
- **D-54:** Unresolved-adapter escalation — if M1 cannot determine fee-reservation and rounding semantics unambiguously from official documentation + authenticated read-only responses, retain per-capability status field(s) (see G1.15) and block strategy evaluation via the capability-scoped validator (G1.16). Do NOT choose whichever ordering merely passes property tests. M6B will later reconcile the model against actual `trade_amount`, `trade_quantity`, `paid_fee`, and `reserved_fee`.
- **Status:** frozen.

### G1.12 — Decision Register storage
- **D-55:** Split by gate:
  - `config/decisions/gate1.toml`
  - `config/decisions/gate2.toml`
  - `config/decisions/gate3.toml`
- **D-56:** Phase 1 creates and populates ONLY `gate1.toml`. Gate 2 and Gate 3 files are created only after their respective human discussions and approvals (Phases 3 and 5). Do NOT create apparently valid Gate 2 or Gate 3 files with guessed defaults.
- **D-57:** All three files contain public configuration only and are committed to git. Secrets never belong in these files.
- **D-58:** Parse TOML separately (stdlib `tomllib` if project minimum is Python ≥3.11, else `tomli`), then validate through immutable/frozen `pydantic.BaseModel` schemas. Do NOT use `pydantic.BaseSettings` as the primary TOML parser; reserve `pydantic-settings`/environment sources for operational configuration and secrets.
- **D-59:** Capability-scoped validation loads only the gates required by the requested operation:
  - M0 / M1 / M2 build paths → frozen Gate 1
  - Candidate evaluation → frozen Gate 1 + Gate 2
  - Final holdout → frozen Gate 1 + Gate 2 + Gate 3 + final freeze manifest
  - Live paths additionally require explicit runtime authorization
- **D-60:** Missing, malformed, unfrozen, or hash-mismatched required gate files → fail closed.
- **D-61:** Per-gate non-secret provenance metadata:
  - `schema_version = 1`
  - `status = "frozen"`
  - `approved_at_utc = "..."`
  - `source_commit = "..."`
  - `research_spec_sha256 = "..."`
  - `execution_spec_sha256 = "..."`
- **D-62:** Record a SHA-256 for each gate independently when it is frozen. The final `config_hash` = deterministic hash over a canonical manifest containing the hashes of Gate 1 + Gate 2 + Gate 3 + any other behavior-affecting configuration — NOT merely the hash of one TOML file.
- **D-63:** Once a gate is frozen, later phases must not rewrite it silently. A proposed change requires an explicit amendment record, human approval, new before/after hashes, and the invalidation or holdout-burn behavior required by `docs/EXECUTION.md`.
- **Status:** frozen.

### G1.13 — Secret loading
- **D-64:** Prod / CI / headless: secrets from process environment variables only via `pydantic_settings.BaseSettings` with `SecretStr` fields. **Do NOT use `pydantic.BaseSettings`** — in pydantic v2, settings support is provided by the separate `pydantic-settings` package.
- **D-65:** Local development: an external secrets file is allowed ONLY when the operator explicitly supplies its absolute path via `BITHUMB_BOT_SECRETS_FILE=<absolute outside-repository path>`. Do NOT automatically search the repository, current directory, parent directories, or user profile for `.env` files. **Reject a secrets-file path that resolves inside the git repository, including through a symlink.** Use an OS-appropriate external configuration location, e.g. `%LOCALAPPDATA%\BithumbBot\secrets.env` on Windows. Check restrictive file permissions where the operating system exposes a reliable check; fail or issue a prominent security error when the file is broadly readable.
- **D-66:** Source precedence:
  1. Explicit process environment variables
  2. Explicitly configured outside-repository secrets file
  3. No fallback
  Reject ambiguous mixed configuration when the same credential is defined differently in both sources.
- **D-67:** Credential classes:
  - `BITHUMB_ACCOUNT_READ_ACCESS_KEY`
  - `BITHUMB_ACCOUNT_READ_SECRET_KEY`
  - `BITHUMB_TRADE_ACCESS_KEY` — prohibited before M6B
  - `BITHUMB_TRADE_SECRET_KEY` — prohibited before M6B
- **D-68:** Public paths load NO credential. Account/read capabilities require only the account/read credential. Before M6B, the application MUST reject the trade credential if supplied rather than silently loading it (validator reports only that a prohibited credential class was detected, never its value).
- **D-69:** **No withdrawal credential or withdrawal-permission configuration exists anywhere in the application.**
- **D-70:** Handling: never print, serialize, hash, persist, include in exception text, or expose secrets through model `repr`. Logs may report only credential class and presence/absence, never values. Keep operational secret settings completely separate from committed Gate TOML files. Tests use injected dummy values and must not read real operator credentials. Authenticated capability startup fails closed when its required credential is absent or empty.
- **Status:** frozen (policy). Exact launcher scripts + OS permission checks implemented in M0.

### G1.14 — Static-invariant enforcement
- **D-71:** Import boundary via **Import Linter**. In `pyproject.toml`:
  ```toml
  [tool.importlinter]
  root_package = "<actual_package_name>"
  exclude_type_checking_imports = false

  [[tool.importlinter.contracts]]
  name = "Core must not import broker"
  type = "forbidden"
  source_modules = ["<actual_package_name>.core"]
  forbidden_modules = ["<actual_package_name>.broker"]
  ```
  Actual package name is determined after the Phase-1 repository layout is fixed. The package must be importable when `lint-imports` runs. Do NOT add ignore rules merely to make the initial contract pass. If an unavoidable exception is proposed, stop for human approval.
- **D-72:** Decimal-from-float lint via a **repository-owned Python AST checker**. Recognizes at least:
  - `Decimal(0.1)`, `Decimal(-0.1)`, `Decimal(+0.1)`
  - `decimal.Decimal(0.1)`
  - `dec.Decimal(0.1)`
  - `D(0.1)`
  where `dec` or `D` is an alias imported from the standard `decimal` module.
  Must NOT flag safe constructions such as `Decimal("0.1")`, `Decimal(1)`, `Decimal("-0.1")`.
  Scope: rejection of statically identifiable float literals passed to stdlib `decimal.Decimal`. Do NOT pretend the checker can infer the runtime type of every variable.
- **D-73:** Both `lint-imports` and the Decimal AST checker run in **pre-commit AND mandatory CI** (pre-commit alone is not enforcement — it can be skipped). Both commands return nonzero on violations. Add positive+negative fixture tests for the AST checker (imports, aliases, unary signs, nested directories, syntax errors, false-positive cases). Add an architectural-violation fixture or controlled test proving the Import Linter contract actually fails. Pin the tool versions in the project lockfile. Include both checks in the single documented quality command used by Phase-1 verification. **Do NOT use a custom Ruff plugin** (ruff continues to run for its supported built-in rules).
- **Status:** frozen.

### G1.15 — Spec-snapshot artifact + verification workflow + corrected capability-status rules

**Artifact layout** (Windows-safe UTC timestamps without colons):
- **D-74:**
  ```
  artifacts/spec_snapshots/KRW-BTC/20260908T012345Z.json
  artifacts/spec_snapshots/KRW-BTC/20260908T012345Z.json.sha256
  tests/fixtures/bithumb/sanitized/<endpoint>/20260908T012345Z.json
  verification/bithumb/20260908T012345Z/VERIFICATION.md
  verification/bithumb/20260908T012345Z/manifest.json
  ```
  Do not use filenames containing `:` — the project must work on Windows.

**Canonical snapshot**
- **D-75:** Simulator-consumed JSON contains only normalized, non-secret, behavior-relevant fields:
  ```json
  {
    "schema_version": 1,
    "venue": "bithumb",
    "market": "KRW-BTC",
    "retrieved_at_utc": "2026-09-08T01:23:45Z",
    "source_endpoints": [],
    "fee_rates": {
      "bid": "decimal-string",
      "ask": "decimal-string",
      "maker_bid": "decimal-string-or-null",
      "maker_ask": "decimal-string-or-null"
    },
    "minimums": {},
    "price_tick_rules": {},
    "quantity_step_rules": {},
    "supported_order_types": [],
    "verification_status": {},
    "source_fixture_hashes": []
  }
  ```
  Every decimal as a string. Do NOT serialize monetary values through a binary float.
- **D-76:** Canonical JSON: UTF-8, sorted keys, fixed compact separators, no insignificant whitespace, exact trailing-newline policy. Hash the exact canonical bytes. Sidecar format: `<64-character-sha256>  <filename>`. Atomic write via temporary file + same-filesystem replacement. **Never overwrite a previously consumed snapshot.**

**Security and privacy**
- **D-77:** Never commit raw authenticated responses without inspection. Strip authorization headers, cookies, request signatures, nonces, access keys, account identifiers, balances, average purchase prices, locked balances, and unrelated account information. Prefer a schema allowlist that constructs a sanitized fixture from permitted fields rather than a blacklist that tries to redact forbidden fields. Store any temporarily retained raw authenticated response outside the repository and delete it after producing and reviewing the sanitized fixture. Add automated secret and account-data checks for committed fixtures.

**Verification workflow**
- **D-78:** A small read-only verification spike making only the minimum endpoint-specific requests required. For each of the five build-time facts, `VERIFICATION.md` records:
  - exact claim
  - status: `confirmed` | `contradicted` | `unresolved`
  - official documentation URL + access timestamp
  - API endpoint or WebSocket channel tested, where applicable
  - sanitized fixture path + SHA-256
  - observed result
  - effect on implementation
  - remaining limitation
  - user approval status
  A documentation statement and an observed API response are different evidence classes. Do NOT claim live behavior was verified when only a documentation page was read.

**Offline tests**
- **D-79:** CI never calls Bithumb. CI replays only sanitized, committed fixtures. Tests verify schema parsing, Decimal preservation, normalization, canonicalization, sidecar validation, tamper detection, unsupported schema versions, missing fields, and contradictory-fixture behavior. The simulator accepts a snapshot only when its sidecar hash, schema, market, and required verification statuses pass. Every experiment records the exact snapshot path and SHA-256 it consumed.

**Corrected capability-status rules** (removing the earlier contradiction)
- **D-80:** Missing or unverified **general fee rate, amount unit, tick rule, or volume step required by the simulator** → **blocks strategy evaluation** (capability-scoped validator refuses `m4 evaluate-selection`, `m5 evaluate-module`, `freeze strategy`, `m6a verify-mock-broker`, `freeze final`, `holdout evaluate`).
- **D-81:** Documented but not live-tested **fee reservation / rejection / fill behavior** MAY use an approved conservative simulator assumption (recorded in `VERIFICATION.md` with cost treatment + verification status + stress scenarios). This may **permit backtest evaluation** but does NOT claim live validation.
- **D-82:** Such provisional behavior **blocks live operation** (M6B+ capabilities remain refused). Actual live fee-reservation, rejection, and fill behavior that cannot be verified without a real order remains `unresolved_until_M6B`.
- **D-83:** Use **capability-specific status fields** rather than one blanket `unresolved_adapter_mode`, e.g.:
  - `general_fee_rate: confirmed_read_only`
  - `market_buy_fee_reservation: provisional_documented`
  - `rounding_rejection_behavior: unresolved_until_M6B`
  - `live_order_acceptance: unresolved_until_M6B`
- **D-84:** Human approval: call `VERIFICATION.md` **human-approved**, not "human-signed", unless an actual cryptographic signature mechanism is implemented. Claude Code prepares the evidence; the user approves the conclusion. Git history and the later final-artifact manifest provide provenance. A SHA-256 sidecar detects byte changes but is not, by itself, proof that an artifact came from Bithumb.
- **Status:** frozen.

### G1.16 — Capability CLI + guard matrix
- **D-85:** Single `bt` console script. One central capability registry declares each operation's prerequisites. The CLI dispatcher validates all prerequisites before invoking a handler or producing any network/file side effect. Sensitive application-service functions repeat the capability check internally so direct Python calls cannot bypass CLI validation. `core/` defines capability types and validation results but never imports `broker/` or the CLI adapter. Help + version commands require no project capability and never load credentials.
- **D-86 (Phase-1 commands implemented):**
  - `bt config validate --through gate1`
  - `bt m0 selfcheck`
  - `bt m1 fetch-spec --market KRW-BTC`
  - `bt m1 verify-facts --bundle <verification-bundle>`
  - `bt m1 verify-snapshot --snapshot <snapshot-path>`
  `fetch-spec` performs the minimum authenticated read-only requests and creates sanitized evidence + a candidate snapshot. `verify-facts` and `verify-snapshot` are offline verification commands.
- **D-87 (Future commands reserved in the capability spec, NOT implemented / registered in Phase 1):**
  - `bt m2 collect-observations`
  - `bt m2 calibrate-costs`
  - `bt m2 replay-known-answer`
  - `bt m4 evaluate-selection`
  - `bt m5 evaluate-module`
  - `bt freeze strategy`
  - `bt m6a verify-mock-broker`
  - `bt freeze final`
  - `bt holdout evaluate`
  Do NOT call strategy selection `m2 backtest-selection`; selection belongs to M4. **Do NOT add any M6B/live command in the current v1 scope.** A future M6B project must add it only after explicit authorization.

- **D-88 (Guard matrix):**

  | Command | Frozen Gate 1 | Frozen Gate 2 | Frozen Gate 3 | Verified spec snapshot | Non-null validated cap | Account/read cred | Trade cred | Human authorization |
  |---|---|---|---|---|---|---|---|---|
  | `config validate --through gate1` | candidate being validated | no | no | no | no | no | prohibited | no |
  | `m0 selfcheck` | yes | no | no | no | no | no | prohibited | no |
  | `m1 fetch-spec` | yes | no | no | no | no | required | prohibited | command invocation only |
  | `m1 verify-facts` | yes | no | no | evidence input | no | no | prohibited | no |
  | `m1 verify-snapshot` | yes | no | no | candidate being verified | no | no | prohibited | no |
  | `m2 collect-observations` | yes | no | no | yes | may be null | no | prohibited | no |
  | `m2 calibrate-costs` | yes | no | no | yes | may be null; command produces candidate value | no | prohibited | no |
  | `m2 replay-known-answer` | yes | no | no | fixture-defined | may be fixture value | no | prohibited | no |
  | `m4 evaluate-selection` | yes | yes | no | yes | required | no | prohibited | no |
  | `m5 evaluate-module` | yes | yes | no | yes | required | no | prohibited | no |
  | `freeze strategy` | yes | yes | no | yes | required | no | prohibited | explicit approval |
  | `m6a verify-mock-broker` | yes | yes | no | yes | required | no | prohibited | no |
  | `freeze final` | yes | yes | no | yes | required | no | prohibited | explicit approval |
  | `holdout evaluate` | yes | yes | yes | yes | required | no | prohibited | **one-time explicit approval** |

- **D-89 (Semantics):**
  - `config validate` may inspect an unfrozen candidate Gate file because its purpose is validation; it must NOT mark or rewrite it as approved. Only a separately approved freeze action records a gate as frozen.
  - A null `max_validated_notional_krw` is allowed only for observation collection, calibration, and explicit test fixtures. Any selection, module evaluation, final freeze, or holdout path requires the non-null frozen cap.
  - Every pre-M6B command fails if trade-credential environment variables are present. The validator reports only that a prohibited credential class was detected, never its value.
  - Account/read credentials are loaded only by the exact M1 network operation that requires them. Offline verification must not load them.
  - `holdout evaluate` must require a **one-time, non-persistent human authorization mechanism** in addition to Gate + hash checks. Merely reaching Phase 6 is not authorization.
  - A future command that is not in the registry fails as unknown; capability requirements may not default to an empty set.

- **D-90 (Phase discipline):** Phase 1 implements only the Phase-1 commands and the reusable validator. Future verbs are documented and covered by registry/schema tests, but their handlers must NOT be scaffolded in a way that could be mistaken for completed functionality.
- **Status:** frozen.

### Cross-cutting confirmations (locked, explicit)
- **D-91:** Public trigger WebSocket is **v1**, not v2.
- **D-92:** Private order WebSocket is **v2** — must remain a separate adapter configuration from public v1.
- **D-93:** REST fallback is `/v1/trades/ticks` (`/v1/ticker` = diagnostic only).
- **D-94:** Legacy `/trade/stop_limit` and legacy `Api-Key`/`Api-Nonce`/`Api-Sign` authentication are **excluded from v1** (deferred research alternative; adding later = material change under holdout-burn rules).
- **D-95:** 240-minute candles are the only v1 strategy-evaluation timeframe (6h aggregation utility permitted for tests only, must aggregate from 60m/trades, must not enter M4/M5 or holdout).
- **D-96:** No M6B / live handler is implemented or registered.
- **D-97:** Trade credentials are prohibited before M6B; validator rejects and reports class only, never value.
- **D-98:** No withdrawal credential or withdrawal-permission configuration exists anywhere in the application.

### Requirement clarifications this discussion introduces to `.planning/REQUIREMENTS.md`
- **D-99 (SAFE-02 restructured):** one blanket startup self-check → capability-scoped validation with per-command guard matrix (see G1.16). Missing/required inputs surface per capability, not as a global refuse-to-run.
- **D-100 (SPEC-01 / -02 / -03 / -04 expanded):** M1 must additionally verify amount unit, volume step, fee currency, and fee-reservation semantics; store verified responses as hashed fixtures; adapter carries per-capability status fields (see G1.15); simulator refuses to load a snapshot whose required verification statuses do not pass.

### Claude's Discretion
The following are planner discretion within the frozen invariants above; the user did not further constrain them:
- **Repo directory layout** — final package name and module layout under `src/`. Must respect: `core/` never imports from `broker/`; the actual package name must be usable by the Import Linter contract in `pyproject.toml` (D-71); the `.planning/research/ARCHITECTURE.md` suggested layout is a reasonable starting point (`src/config`, `src/spec_snapshot`, `src/bithumb_spec`, `src/bithumb_data`, `src/core/*`, `src/broker`, `src/artifact`) but nothing binds Phase 1 to it verbatim.
- **Risk-denominator vocabulary implementation form** — SAFE-07 requires "documented". Whether the risk vocabulary (planned_stop_loss / max_market_loss / max_operational_loss / position_fraction / risk_per_trade) also lives as pydantic value objects / `NewType` / `Annotated[Decimal, ...]` consumed by the sizing pipeline, or stays as docstrings + naming convention only, is planner discretion.
- **Per-channel token-bucket implementation** — asyncio-native vs threading vs both; concrete internal API. Numeric per-channel rate values are M1 build-time verification items (recorded in the M1 `VERIFICATION.md` bundle).
- **Test framework layout** — pytest + hypothesis are the stack choice (per `.planning/research/STACK.md`); the layout of `tests/` and any hypothesis strategies for the Decimal AST checker and rounding invariants are planner discretion.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Authoritative build spine (do NOT edit during phase work)
- `docs/EXECUTION.md` — ordered milestone spine + three-gate Decision Register. All Gate-1/2/3 decisions ultimately trace back to this file. §Gate 1, §M0, §M1, §M2, §Decision Register are directly load-bearing for this phase.
- `docs/RESEARCH.md` — strategy landscape, validation theory, Bithumb integration facts, security. Companion to EXECUTION.md; both are the frozen product of eight rounds of audit.

### Planning artifacts
- `.planning/PROJECT.md` — project value, constraints, key decisions table (records — does not authorize).
- `.planning/REQUIREMENTS.md` — the 55-requirement spec breakdown; SAFE-01…07 and SPEC-01…05 are this phase's scope. See D-99, D-100 for the clarifications this discussion introduces.
- `.planning/ROADMAP.md` — phase list, dependencies, per-phase success criteria; Phase 1 success criteria at lines 38–43.
- `.planning/STATE.md` — current position + accumulated context; will be updated after this CONTEXT.md is written.

### Supplemental research (records — do NOT authorize)
- `.planning/research/SUMMARY.md` — executive synthesis of stack/features/architecture/pitfalls verification for Gate-1 / M0-M5-M6A milestone.
- `.planning/research/STACK.md` — recommended Python stack (uv, ruff, mypy --strict, pytest+hypothesis, pydantic v2, pydantic-settings, structlog, httpx, websockets, PyJWT, arch, pandas, pyarrow, decimal.Decimal), alternatives considered, "what NOT to use", version compatibility notes.
- `.planning/research/ARCHITECTURE.md` — offline-core / isolated-broker structure; component responsibilities; suggested `src/` layout (planner discretion).
- `.planning/research/FEATURES.md` — table-stakes vs differentiators vs anti-features.
- `.planning/research/PITFALLS.md` — 14 implementation traps; top-5 all directly apply to this phase (same-candle bias helpers; multiple-testing tool mismatch; two separate CIs; block-length sensitivity; holdout leakage via debugging/simulator changes).

### Bithumb API surface (verify at M1 build time)
- `apidocs.bithumb.com` — the ONLY authoritative surface. All five build-time facts (private WS v1-vs-v2 boundary, JWT claim construction, legacy `/trade/stop_limit` fee — deferred in v1, pagination cursor inclusivity, per-channel rate-limit values) are attested per fact in the M1 `VERIFICATION.md` bundle.
- **Do NOT confuse** `bithumb-pro/bithumb.pro-official-api-docs` or `bithumbfutures.github.io` with the target venue — different product, different API, different auth scheme.

### Artifacts produced/consumed by Phase 1
- `config/decisions/gate1.toml` — created + populated in Phase 1 (D-55…D-63). Public.
- `artifacts/spec_snapshots/KRW-BTC/<YYYYMMDDTHHMMSSZ>.json` + `.sha256` — created by `bt m1 fetch-spec` (D-74…D-76).
- `tests/fixtures/bithumb/sanitized/<endpoint>/<YYYYMMDDTHHMMSSZ>.json` — sanitized recorded-response fixtures (D-77, D-79).
- `verification/bithumb/<YYYYMMDDTHHMMSSZ>/VERIFICATION.md` + `manifest.json` — per-fact evidence bundle (D-78, D-84).
- `state/runtime.sqlite3` — schema not yet needed in Phase 1 (no protective-exit path built); however, its Gate-1 schema policy (D-31…D-34) is frozen here because it constrains M2/M6A code that will later be scaffolded on top.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- None. This is a clean-room greenfield rebuild — no `src/`, no `pyproject.toml`, no committed Python code as of 2026-09-08. The prior `bot.py` (Upbit paper bot with the same-candle execution bias) is deliberately NOT ported; reuse concepts, not code.

### Established Patterns
- **Import-direction boundary (`core/` never imports `broker/`)** — must be established in Phase 1 as an Import Linter forbidden-contract in `pyproject.toml` (D-71), NOT retrofitted at freeze.
- **Exact-decimal money** — enforced project-wide by the repo-owned Decimal AST checker (D-72). Every disk/network boundary uses strings or `pyarrow.decimal128`; every arithmetic uses `Decimal`.
- **Immutable artifacts + SHA-256** — the spec snapshot pattern (D-74…D-76, atomic write + sidecar hash, never overwrite) is the template M2 will reuse for the Parquet candle store's per-file integrity check.
- **Capability-scoped validation** — every capability's prerequisites are declared once in the registry (D-85, D-88); the same validator is called by both the CLI dispatcher and the internal service functions (defence in depth against direct-Python-call bypass).

### Integration Points
- The M1 spec snapshot is the ONLY authoritative fee/tick/step source the M2 simulator consumes. The simulator loads a snapshot only when its sidecar hash + schema + market + required verification statuses pass (D-79). Every experiment records the snapshot path + SHA-256 it consumed (D-79) — this record feeds the final artifact hash in Phase 5 (`config_hash` component per D-62).
- The Gate-1 TOML is loaded via `tomllib`/`tomli` → pydantic-frozen model → hashed. The hash feeds the final `config_hash` manifest (D-62), which is one of the six components frozen at Phase 5 FRZ-02.
- The Gate-3 watchdog schema (D-31…D-39) constrains what Phase 2 (M2 observe-only calibration) and Phase 5 (M6A mock broker) can build against `state/runtime.sqlite3`; the DB itself is not exercised by Phase 1 code.

</code_context>

<specifics>
## Specific Ideas

- **Bithumb Public WebSocket v1 trade stream** at `wss://ws-api.bithumb.com/websocket/v1`, subscription `type=trade`, `isOnlyRealtime=true`, is the primary trigger source (D-06). This exact URL + type is a locked v1 fact — do not change to v2.
- **REST trade fallback endpoint** `GET /v1/trades/ticks` — locked (D-07). `GET /v1/ticker` is diagnostic only, not the primary trigger.
- **Directory naming for spec snapshots** uses Windows-safe timestamps `YYYYMMDDTHHMMSSZ` with no colons (D-74) — the project must work on Windows.
- **Provisional test-fixture cap value** `provisional_engineering_notional_krw = 100_000` KRW — locked (D-09, D-43) for unit tests and preregistered hypothetical calibration grid only; never a validated cap.
- **M1 read-only HTTP timeouts** (D-40) are frozen at Gate-1 numeric values NOW: connect 5000 ms; read 15000 ms; max attempts 3; backoff initial 500 ms, cap 5000 ms; full jitter. These MUST NOT be reused implicitly by the protective-exit path — the trigger REST fallback timeouts (D-41) are separate Gate-2 fields.
- **SQLite state DB** at `state/runtime.sqlite3` — WAL mode, `PRAGMA synchronous = FULL`, decimals as canonical text (never REAL), tables per D-33 (`positions`, `protective_exit_intents`, `ownership_leases`, `broker_observations`, `audit_events`, `schema_metadata`).
- **VERIFICATION.md is human-approved, not human-signed** (D-84) — do not describe it as a cryptographic signature.

</specifics>

<deferred>
## Deferred Ideas

Recorded, not started; each is out of Phase-1 scope. None represents scope creep during this discussion — they are naturally-deferred future work.

- **Full L2-aware simulator with quantity-dependent impact model** — deferred research extension. Mandatory before increasing order size beyond validated applicability cap (G1.9). Coupled to `.planning/research/FEATURES.md` "L2 order-book slippage" differentiator.
- **Persistent sequenced L2 pipeline** — deferred research extension.
- **Live-phase runtime liquidity guard using fresh order book** — required for live phase (M6B+); distinct from historical-model cap; must fail closed on stale/unavailable snapshot (D-14).
- **Legacy `/trade/stop_limit` protective exit** + legacy `Api-Key`/`Api-Nonce`/`Api-Sign` HMAC adapter + `watch→wait→done/cancel` FSM + intent-log idempotency (BRK-03 legacy branch) — deferred research alternative. Adding later = material change under holdout-burn rules (D-19).
- **Numeric values for the 8 Gate-2 stop-operational parameters** (D-41) — frozen in Phase 3 after M1 rate-limit verification + preregistered M2 observation window.
- **Numeric values for the 7 Gate-3 stop-operational parameters** (D-42) — frozen in Phase 5 before the holdout is opened.
- **Per-channel rate-limit numeric values** — M1 build-time verification items; recorded in the M1 `VERIFICATION.md` bundle (D-78).
- **Risk-denominator vocabulary implementation form** — planner discretion within SAFE-07 "documented" minimum.
- **Repo directory layout final naming** — planner discretion within the `core/`↔`broker/` boundary; Import Linter contract needs the actual package name (D-71).

</deferred>

---

*Phase: 1 - Safety Foundation + Bithumb Spec Adapter*
*Context gathered: 2026-09-08*
