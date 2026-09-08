# Requirements: Bithumb Autotrading Bot

**Defined:** 2026-09-08
**Core Value:** A trustworthy verdict on whether the strategy has real, cost-and-execution-honest edge — produced by a frozen, hashed artifact evaluated on a holdout opened exactly once.

> Scope of this milestone: **Gate-1 → M0–M5 → M6A → final freeze → one-time holdout.**
> No live trading, no trade key, no real orders. Derived from `docs/EXECUTION.md`
> (milestone spine + Decision Register) and `.planning/research/`. Gate decisions
> are resolved per-milestone at discuss/plan time, not here.

## v1 Requirements

### Safety, Config & Keys (M0)

- [x] **SAFE-01**: Config loader holds the Decision Register (Gate-1/2/3 decisions) as immutable typed values
- [x] **SAFE-02**: Startup self-check prints resolved Gate-1 decisions, key class, and risk denominators, and refuses to run if any required decision or denominator is missing
- [x] **SAFE-03**: Three-class API key policy enforced (public / account-read / trade); no key ever has withdrawal permission
- [x] **SAFE-04**: Secrets loaded from env/secret store only, never from `config.json` or source; secret paths are git-ignored
- [x] **SAFE-05**: Exact-decimal money type used for all price/qty/PnL; a lint/type rule rejects constructing a decimal from a float literal
- [x] **SAFE-06**: `core/` (offline, deterministic, hashable) never imports from `broker/`; the import-direction boundary is checked automatically
- [x] **SAFE-07**: Risk-denominator vocabulary defined and documented (planned_stop_loss vs max_market_loss vs max_operational_loss; position_fraction vs risk_per_trade)

### Bithumb Spec Adapter (M1 — authenticated read, no trade)

- [x] **SPEC-01**: Using the account/read key (JWT), the spec adapter queries available-order-information (e.g. `/v1/orders/chance`) for `bid_fee`, `ask_fee`, `maker_bid_fee`, `maker_ask_fee`, min order size, and supported order types at startup and periodically
- [x] **SPEC-02**: Fees used are recorded per experiment
- [x] **SPEC-03**: Price-tick and qty-step rounding and minimum-order rules are encoded and pass boundary unit tests
- [x] **SPEC-04**: The fee/tick/min-order snapshot is persisted as a hashed artifact that the simulator consumes (never re-queried mid-simulation)
- [x] **SPEC-05**: Per-channel token-bucket rate limiting + backoff for public REST, private REST, and WebSocket

### Data Pipeline (M2)

- [ ] **DATA-01**: Candle ingestion paginates (≤200/req), dedupes, validates timezones, and handles gaps
- [ ] **DATA-02**: Candles stored in an immutable store with SHA-256 integrity checks per file
- [ ] **DATA-03**: 6h candles derived as a re-verifiable pure function over the 60m store (never from 240m), unit-tested against hand-checked bars
- [ ] **DATA-04**: Native intervals 1/3/5/10/15/30/60/240 min supported; pagination cursor inclusivity established empirically, not assumed

### Execution Simulator (M2 — the core deliverable)

- [ ] **SIM-01**: A signal from candle *t* fills no earlier than the next tradable observation (≥ *t+1* open); same-candle fill is impossible by construction
- [ ] **SIM-02**: Market/marketable exits use full OHLC with a predeclared worse-outcome rule when a bar spans stop and target; gap-throughs fill at the next adverse price, not the trigger
- [ ] **SIM-03**: Stop-limit state machine models `watch → wait → done/cancel`, including gap-below-a-protective-sell-limit → **no fill + retained exposure**, unfilled-across-candles, partial-then-decline, cancel-vs-fill races, and the deterministic candle-only no-fill rule for ambiguous intrabar paths
- [ ] **SIM-04**: Cost model applies queried fees (M1) plus modeled-and-calibrated slippage/impact; supports the four cost scenarios (queried, non-promotional reference, absolute-bp, total round-trip)
- [ ] **SIM-05**: Observe-only provisional calibration compares hypothetical marketable-order cost to sim assumptions without placing orders, and sets conservative bounds
- [ ] **SIM-06**: All order submission flows through a single `ExecutionSimulator` chokepoint (no bypass helper can read same-index OHLC)
- [ ] **SIM-07**: Known-answer replay tests pass (same-close impossible; gap-through market exit fills adverse; stop-limit gap-below-limit → no fill + retained position; ambiguous intrabar → no fill)

### Validation Protocol / Preregistration (M3 — Gate 2)

- [ ] **PROTO-01**: The full research protocol is preregistered before any candidate is evaluated (metrics, tie-breaking, parameter ranges, folds, thresholds, tests)
- [ ] **PROTO-02**: Acceptance is an explicit Boolean E∧R table with both E and R fully defined; accept only on E AND R
- [ ] **PROTO-03**: R's uncertainty test is a paired resampling on Δmetric = metric(strategy) − metric(buy_and_hold), not two separate confidence intervals
- [ ] **PROTO-04**: Nested walk-forward with declared train/selection/holdout lengths, step size, and fold construction; signal timing matches simulator temporal separation
- [ ] **PROTO-05**: Each statistical test is assigned to a stage (PBO/CSCV selection diagnostic; DSR as a Sharpe-based probability with threshold, or an objective-matched procedure if the metric is Calmar; block bootstrap for selection and final-holdout CIs separately)
- [ ] **PROTO-06**: Block-bootstrap block-length sensitivity is checked across ≥2–3 candidate lengths and the verdict-stability table committed before the length is frozen
- [ ] **PROTO-07**: Holdout-opened-once, artifact-freeze-and-hash, holdout-burn, and forward-data-replacement rules are written down; the material-change definition is explicit
- [ ] **PROTO-08**: Every candidate evaluated is logged, feeding the multiple-testing correction

### Baseline Evaluation (M4)

- [ ] **EVAL-01**: The preregistered baseline candidate(s) run through the M2 simulator with M1 costs, evaluated within selection windows only
- [ ] **EVAL-02**: Results are produced strictly under the frozen M3 protocol; nothing is changed after seeing them (recorded metrics + candidate count)
- [ ] **EVAL-03**: The validation harness structurally prevents reading holdout rows during any selection-stage call

### Adaptive Modules (M5)

- [ ] **MOD-01**: ATR per-trade sizing (unit-safe formula) with a hard max-position cap, added and gate-tested one at a time
- [ ] **MOD-02**: ATR stop vs. fixed-% stop compared as competing hypotheses under one risk budget/cost/gap/sizing rule
- [ ] **MOD-03**: Trailing stop (e.g. chandelier) added and gate-tested
- [ ] **MOD-04**: Volatility-target overlay added and kept only if net-positive within selection windows
- [ ] **MOD-05**: Regime gate (ADX + long-MA + ATR-vs-average) with anti-whipsaw guardrails, gate-tested as an unvalidated module
- [ ] **MOD-06**: Max-drawdown kill-switch + tiered daily-loss halt implemented
- [ ] **MOD-07**: A declared interaction-testing / module-ordering policy governs add-order; any module failing the gate within selection windows is dropped, not retuned

### Freeze & Broker Scaffolding (M5-freeze, M6A, final freeze, Gate-3)

- [ ] **FRZ-01**: Strategy definition frozen after all keep/drop decisions (signal, parameters, sizing, stop/exit, cost model, statistical protocol)
- [ ] **BRK-01**: Mock/paper `BithumbBroker` places/cancels v2 `limit`/`price`/`market`/`best` (+ Post-Only where intended) with simulated partial fills/rejects; live-broker package stays import-isolated from `core/`
- [ ] **BRK-02**: Frozen stop mechanism's client code, state handling, idempotency, reconciliation, and watchdog logic implemented (source written before the final freeze)
- [ ] **BRK-03**: Two idempotency protocols implemented — v2 `client_order_id` (unique, reconcile-before-retry) and legacy intent-log (`/trade/stop_limit`, no client_order_id → persist intent, send once, reconcile across watch/wait/done/cancel + balances before any retry; at most one unresolved identical intent per (market, side, parameter) tuple; else stop for manual reconciliation)
- [ ] **BRK-04**: Durable transactional state — restart never duplicates orders; startup reconciliation against live balances/open orders
- [ ] **BRK-05**: Independent watchdog / separate reconciliation detects a dead stop monitor or a failed cancel
- [ ] **BRK-06**: M6A test suite passes — JSON schema tests, auth-construction tests for both schemes (JWT; legacy Api-Sign if in scope, with known-answer signature + nonce monotonicity/persistence tests), mock lifecycle, recorded-response replay, reconnect recovery from duplicate/missing/out-of-order messages, forced-restart-creates-no-duplicate
- [ ] **FRZ-02**: Final artifact frozen and hashed after all pre-holdout code (source commit/hash, config hash, simulator version, dataset hash, cost model, stop mechanism, statistical protocol); package boundary enforced so the backtest imports nothing from the live-broker package
- [ ] **FRZ-03**: All Gate-3 pre-live risk limits and escalation policies recorded before the holdout result is known (capital-isolation mechanism, tiered halts, size-ladder policy, M7 min duration + min real fills, live-escalation rules, re-validation cadence, M8 capital limit)

### Holdout Evaluation

- [ ] **HOLD-01**: The frozen final strategy on the final hashed artifact is evaluated on the holdout exactly once, against the Boolean E∧R table, only after FRZ-02 and FRZ-03 are complete

## v2 Requirements

Deferred to a future milestone. Tracked but not in this roadmap.

### Post-Holdout Backlog — Live Integration & Pilot (Deferred)

**Status: Deferred — tracked, not started, not completed, not removed from scope.**
These items are the M6B → M7 → M8 continuation of `docs/EXECUTION.md`, gated
behind the successful holdout verdict from Phase 6 and a separate milestone with
its own trade-enabled key and funded technically-isolated subaccount. They must
remain visible in every requirement audit until an explicit new milestone opens
them; do not treat as done and do not delete from this file.

- **LIVE-01**: M6B extremely small live integration tests (trade-only, withdrawal-disabled, IP-restricted key; real submission/trigger/cancel/fill/private-stream/reconciliation at minimum size)
- **LIVE-02**: Live credential-permission verification for both auth paths
- **LIVE-03**: M7 strictly limited pilot on a technically-isolated 1–5% sleeve with tiered halts and predeclared min duration + min real fills
- **LIVE-04**: M8 restricted automated operation (only if a sustained M7 pilot matches expectations)

### Research Extensions

- **RSCH-01**: L2 order-book depth-based slippage calibration (if the cost model makes slippage depth-dependent) — requires a Gate-1 L2 source
- **RSCH-02**: Kimchi-premium logged as a candidate context feature
- **RSCH-03**: Multi-coin universe with survivorship / point-in-time membership
- **RSCH-04**: Statistical regime models (HMM) — only after the rule-based regime gate is solidly profitable in walk-forward

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Shorting / long-short / market-neutral / pairs | Not reproducible on Bithumb KRW spot |
| Kelly sizing | Unjustifiable under a zero-or-negative alpha prior for v1 |
| Market-making / grid | Blows up in trends without inventory management; needs low latency |
| Cross-border kimchi-premium arbitrage | Capital controls, transfer/timing risk, non-atomic cross-exchange fills |
| ML/DL as primary price signal; RL for sizing | Tier-3; leakage/survivorship-prone; forecast-accuracy ≠ net P&L |
| Porting the existing Upbit `bot.py` | Clean-room rebuild; its fill model is invalid — reuse concepts, not code |
| Live trading / real orders in this milestone | Requires a trade key + real money; Option B defers all real orders to M6B (v2) |

## Traceability

Every v1 requirement maps to exactly one phase. See `.planning/ROADMAP.md`.

| Requirement | Phase | Status |
|-------------|-------|--------|
| SAFE-01 | Phase 1 | Complete |
| SAFE-02 | Phase 1 | Complete |
| SAFE-03 | Phase 1 | Complete |
| SAFE-04 | Phase 1 | Complete |
| SAFE-05 | Phase 1 | Complete |
| SAFE-06 | Phase 1 | Complete |
| SAFE-07 | Phase 1 | Complete |
| SPEC-01 | Phase 1 | Complete |
| SPEC-02 | Phase 1 | Complete |
| SPEC-03 | Phase 1 | Complete |
| SPEC-04 | Phase 1 | Complete |
| SPEC-05 | Phase 1 | Complete |
| DATA-01 | Phase 2 | Pending |
| DATA-02 | Phase 2 | Pending |
| DATA-03 | Phase 2 | Pending |
| DATA-04 | Phase 2 | Pending |
| SIM-01 | Phase 2 | Pending |
| SIM-02 | Phase 2 | Pending |
| SIM-03 | Phase 2 | Pending |
| SIM-04 | Phase 2 | Pending |
| SIM-05 | Phase 2 | Pending |
| SIM-06 | Phase 2 | Pending |
| SIM-07 | Phase 2 | Pending |
| PROTO-01 | Phase 3 | Pending |
| PROTO-02 | Phase 3 | Pending |
| PROTO-03 | Phase 3 | Pending |
| PROTO-04 | Phase 3 | Pending |
| PROTO-05 | Phase 3 | Pending |
| PROTO-06 | Phase 3 | Pending |
| PROTO-07 | Phase 3 | Pending |
| PROTO-08 | Phase 3 | Pending |
| EVAL-01 | Phase 4 | Pending |
| EVAL-02 | Phase 4 | Pending |
| EVAL-03 | Phase 4 | Pending |
| MOD-01 | Phase 4 | Pending |
| MOD-02 | Phase 4 | Pending |
| MOD-03 | Phase 4 | Pending |
| MOD-04 | Phase 4 | Pending |
| MOD-05 | Phase 4 | Pending |
| MOD-06 | Phase 4 | Pending |
| MOD-07 | Phase 4 | Pending |
| FRZ-01 | Phase 5 | Pending |
| BRK-01 | Phase 5 | Pending |
| BRK-02 | Phase 5 | Pending |
| BRK-03 | Phase 5 | Pending |
| BRK-04 | Phase 5 | Pending |
| BRK-05 | Phase 5 | Pending |
| BRK-06 | Phase 5 | Pending |
| FRZ-02 | Phase 5 | Pending |
| FRZ-03 | Phase 5 | Pending |
| HOLD-01 | Phase 6 | Pending |

**Coverage — Requirement Accounting:**

Total specification requirements: **55** (from `docs/EXECUTION.md` milestone spine)

| Bucket | Count | Status |
|---|---|---|
| **In-scope v1 (roadmap-mapped)** | **51** | Pending — mapped to Phases 1–6 |
| **Deferred post-holdout backlog (LIVE-01…04)** | **4** | Deferred — tracked, not started, not completed, not removed |
| **Total specification requirements** | **55** | — |

**v1 in-scope breakdown:** SAFE 7 + SPEC 5 + DATA 4 + SIM 7 + PROTO 8 + EVAL 3 + MOD 7 + FRZ 3 + BRK 6 + HOLD 1 = 51.

- In-scope mapped to phases: **51 / 51** ✓
- In-scope unmapped: **0**
- Deferred LIVE items visible in v2 backlog: **4 / 4** ✓

Research Extensions (RSCH-01…04) are opportunistic future work outside the 55 spec requirements; tracked separately in the v2 section but not counted toward specification totals.

---
*Requirements defined: 2026-09-08*
*Last updated: 2026-09-08 after roadmap creation (traceability populated); accounting reconciled to 55 spec requirements on 2026-09-08*
