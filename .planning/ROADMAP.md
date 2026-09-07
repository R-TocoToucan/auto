# Roadmap: Bithumb Autotrading Bot

## Overview

This roadmap delivers a trustworthy, cost-and-execution-honest verdict on whether a
single-asset Bithumb KRW-spot strategy has real edge. It mirrors the authoritative
milestone spine in `docs/EXECUTION.md`: safety rails and an authenticated read-only
spec adapter come first, then the core deliverable — a venue-aware conservative
execution simulator that makes same-candle fills impossible by construction. Only then
is the full research protocol preregistered (Gate 2), the baseline and adaptive modules
evaluated one at a time within selection windows, and the complete artifact frozen and
hashed alongside mock-broker scaffolding and Gate-3 risk limits. The final holdout is
opened exactly once, after both the artifact hash and the risk limits are locked. Live
trading (M6B/M7/M8) is explicitly out of this milestone.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Safety Foundation + Bithumb Spec Adapter** - Fail-closed safety rails, immutable Decision Register, three-class key policy, and an authenticated read-only fee/tick/min-order snapshot (M0 + M1)
- [ ] **Phase 2: Data Pipeline + Conservative Execution Simulator** - Immutable candle store and the single-chokepoint venue-aware simulator that makes same-candle fills impossible and models the stop-limit state machine (M2 — the core deliverable)
- [ ] **Phase 3: Preregistered Validation Protocol** - The full research protocol frozen before any candidate: metrics, folds, thresholds, statistical tests, and the Boolean E∧R acceptance table (M3 — Gate 2)
- [ ] **Phase 4: Baseline + Adaptive Modules Under the Gate** - Preregistered baseline and each module evaluated one at a time within selection windows only, keep/drop by the gate (M4 + M5)
- [ ] **Phase 5: Strategy Freeze, Mock-Broker Scaffolding + Gate-3** - Strategy freeze, import-isolated mock broker with dual-auth/idempotency/reconciliation/watchdog code, final artifact hash, and Gate-3 risk limits (M5-freeze → M6A → final freeze → Gate-3)
- [ ] **Phase 6: One-Time Holdout Evaluation** - The frozen hashed artifact evaluated on the holdout exactly once against the Boolean E∧R table (the trustworthy verdict)

## Phase Details

### Phase 1: Safety Foundation + Bithumb Spec Adapter
**Goal**: The project has fail-closed safety rails, a documented immutable Decision Register, a three-class API key policy with withdrawal permanently disabled, and an authenticated read-only Bithumb spec/fee adapter producing a hashed snapshot the simulator will consume.
**Depends on**: Nothing (first phase). Gate-1 pre-build decisions (venue, market, timeframe, stop mechanism, Option B, data + L2 source, simulator fidelity, order policy) are resolved at discuss/plan time and recorded here as immutable config.
**Requirements**: SAFE-01, SAFE-02, SAFE-03, SAFE-04, SAFE-05, SAFE-06, SAFE-07, SPEC-01, SPEC-02, SPEC-03, SPEC-04, SPEC-05
**Gate**: Gate-1 decisions frozen in config/docs before this phase's build work; `core/` vs `broker/` import boundary established here (not retrofitted at freeze).
**Success Criteria** (what must be TRUE):
  1. Startup self-check exits non-zero and refuses to run when any Gate-1 decision or risk denominator is missing, and prints resolved decisions + key class + denominators when present.
  2. A CI/lint check fails the build if `core/` imports from `broker/`, or if a Decimal is constructed from a float literal.
  3. No configured API key carries withdrawal permission; secrets resolve only from env/secret store (never `config.json`/source), with secret paths git-ignored; the risk-denominator vocabulary (planned_stop_loss vs max_market_loss vs max_operational_loss; position_fraction vs risk_per_trade) is documented.
  4. Using the account-read JWT key, the spec adapter fetches `bid_fee`/`ask_fee`/`maker_bid_fee`/`maker_ask_fee`, min order size, tick/step, and supported order types, records fees per experiment, and persists them as a SHA-256-hashed snapshot the simulator consumes (never re-queried mid-simulation).
  5. Tick/step rounding and minimum-order boundary unit tests pass; per-channel token-bucket rate limiting + backoff applies independently to public REST, private REST, and WebSocket.
**Plans**: TBD (~4)

Plans:
- [ ] 01-01: Config loader + immutable Decision Register + startup self-check (fail-closed)
- [ ] 01-02: Three-class key policy, secret loading, Decimal money type + float-literal lint, risk-denominator vocabulary
- [ ] 01-03: `core/`↔`broker/` import-direction boundary + CI enforcement scaffolding
- [ ] 01-04: `BithumbSpec` authenticated read adapter, hashed fee/tick/min-order snapshot, per-channel rate limiting

### Phase 2: Data Pipeline + Conservative Execution Simulator
**Goal**: An immutable, integrity-checked candle store and a single-chokepoint venue-aware conservative simulator that makes same-candle fills impossible by construction, applies the intrabar + gap rule, models the Gate-1 stop-limit state machine, and carries a calibrated cost model. This is the core deliverable.
**Depends on**: Phase 1 (consumes the hashed fee/tick snapshot and the import boundary).
**Requirements**: DATA-01, DATA-02, DATA-03, DATA-04, SIM-01, SIM-02, SIM-03, SIM-04, SIM-05, SIM-06, SIM-07
**Gate**: Gate-1 stop-mechanism decision is implemented here (frozen in Gate-1). Holdout physical-separation discipline begins here.
**Success Criteria** (what must be TRUE):
  1. Known-answer replay: a signal on candle *t* never fills before *t+1* open (same-close fill impossible), and reading same-index OHLC is structurally blocked because all submission flows through a single `ExecutionSimulator` chokepoint.
  2. A gap-through market/marketable exit fills at the next adverse price, not the trigger; a bar spanning stop and target resolves to the predeclared worse outcome.
  3. Stop-limit gap-below-a-protective-sell-limit test yields no fill + retained exposure; an ambiguous intrabar path yields no fill (deterministic candle-only rule).
  4. 6h candles derived as a pure function over the 60m store (never from 240m) match hand-checked bars; ingestion paginates (≤200/req), dedupes, validates timezones, handles gaps, and every store file carries a SHA-256 integrity check across native intervals 1/3/5/10/15/30/60/240 min.
  5. The cost model applies M1 queried fees plus modeled/calibrated slippage across the four cost scenarios; observe-only provisional calibration compares hypothetical marketable-order cost to sim assumptions without placing any orders and sets conservative bounds.
**Plans**: TBD (~4)

Plans:
- [ ] 02-01: `BithumbData` ingestion (pagination, dedupe, tz validation, gap handling) + immutable Parquet store with SHA-256 integrity
- [ ] 02-02: 6h-from-60m aggregation as a re-verifiable pure function, unit-tested against hand-checked bars
- [ ] 02-03: `ExecutionSimulator` chokepoint — fill ≥ t+1, intrabar + gap rule, stop-limit state machine
- [ ] 02-04: Cost model + observe-only provisional calibration + known-answer replay test suite

### Phase 3: Preregistered Validation Protocol
**Goal**: The complete research protocol is preregistered and frozen — metrics, folds, thresholds, statistical tests, and an explicit Boolean E∧R acceptance table — before any candidate is evaluated.
**Depends on**: Phase 2 (walk-forward signal timing must match the simulator's temporal separation).
**Gate**: Gate-2 (research protocol) frozen here.
**Requirements**: PROTO-01, PROTO-02, PROTO-03, PROTO-04, PROTO-05, PROTO-06, PROTO-07, PROTO-08
**Success Criteria** (what must be TRUE):
  1. The frozen protocol exists before any candidate run and defines primary metric + tie-break, parameter ranges, nested walk-forward train/selection/holdout lengths, step size, and fold construction with signal timing matching the simulator's temporal separation.
  2. Acceptance is an explicit Boolean E∧R table (both E and R fully defined; accept only on E AND R); R's uncertainty test is a paired resampling on Δmetric = metric(strategy) − metric(buy_and_hold), unit-tested with synthetic pass/fail fixtures.
  3. Each statistical test is assigned to a stage: PBO/CSCV as a selection diagnostic; DSR as a Sharpe-based probability with a threshold (or an objective-matched procedure if the metric is Calmar); block bootstrap for selection and final-holdout CIs declared separately.
  4. Block-length sensitivity is checked across ≥2–3 candidate lengths and the verdict-stability table is committed before the length is frozen.
  5. Holdout-opened-once, artifact-freeze-and-hash, holdout-burn, forward-data-replacement rules, and the explicit material-change definition are written down; every candidate evaluated is logged, feeding the multiple-testing correction.
**Plans**: TBD (~3)

Plans:
- [ ] 03-01: Metric choice, fold/walk-forward design, E∧R Boolean table, paired-resampling Δmetric procedure
- [ ] 03-02: Per-stage statistical test assignment (PBO/CSCV, DSR, block bootstrap) + block-length sensitivity table
- [ ] 03-03: Holdout/freeze/burn/forward-replacement rules, material-change definition, candidate-log harness

### Phase 4: Baseline + Adaptive Modules Under the Gate
**Goal**: The preregistered baseline and each adaptive module are evaluated one at a time, strictly under the frozen protocol, within selection windows only — keeping or dropping by the gate — with the holdout structurally unreadable.
**Depends on**: Phase 3 (nothing is evaluated before the protocol is frozen).
**Requirements**: EVAL-01, EVAL-02, EVAL-03, MOD-01, MOD-02, MOD-03, MOD-04, MOD-05, MOD-06, MOD-07
**Gate**: Operates entirely under the frozen Gate-2 protocol; no new gate opened.
**Success Criteria** (what must be TRUE):
  1. The preregistered baseline runs through the M2 simulator with M1 costs within selection windows only; recorded metrics + candidate count are produced strictly under the frozen protocol and nothing is changed after seeing them.
  2. The validation harness structurally prevents reading holdout rows during any selection-stage call (runtime guard throws).
  3. Each module — ATR sizing with a hard max-position cap, ATR vs fixed-% stop, trailing stop, volatility-target overlay, regime gate with anti-whipsaw guardrails — is added and gate-tested independently per the declared module-ordering/interaction policy.
  4. A max-drawdown kill-switch + tiered daily-loss halt is implemented.
  5. Any module failing the gate within selection windows is dropped (and recorded), never retuned.
**Plans**: TBD (~4)

Plans:
- [ ] 04-01: Baseline candidate implementation + evaluation within selection windows; holdout runtime guard
- [ ] 04-02: ATR sizing (with cap) and ATR-vs-fixed-% stop comparison under one risk/cost/gap rule
- [ ] 04-03: Trailing stop, volatility-target overlay, regime gate — each gate-tested one at a time
- [ ] 04-04: Max-drawdown kill-switch + tiered daily-loss halt; module-ordering/interaction policy enforcement

### Phase 5: Strategy Freeze, Mock-Broker Scaffolding + Gate-3
**Goal**: Freeze the strategy, build the import-isolated mock broker with dual-auth construction, idempotency, reconciliation, and watchdog code, then hash the complete artifact and record all Gate-3 risk limits — all before the holdout is opened.
**Depends on**: Phase 4 (all keep/drop decisions complete). Internal order is strict: strategy freeze (FRZ-01) → M6A broker code (BRK-*) → final artifact freeze + hash (FRZ-02) → Gate-3 limits (FRZ-03).
**Requirements**: FRZ-01, BRK-01, BRK-02, BRK-03, BRK-04, BRK-05, BRK-06, FRZ-02, FRZ-03
**Gate**: Gate-3 (pre-live risk limits) frozen here; the final artifact freeze + hash completes here. Both must complete BEFORE Phase 6.
**Success Criteria** (what must be TRUE):
  1. The strategy definition (signal, parameters, sizing, stop/exit, cost model, statistical protocol) is frozen after all keep/drop decisions, before any broker code is written.
  2. The mock `BithumbBroker` places/cancels v2 `limit`/`price`/`market`/`best` (+ Post-Only where intended) with simulated partial fills/rejects; the live-broker package is import-isolated from `core/` (enforced), and the M6A suite passes (JSON schema, dual-auth construction incl. legacy nonce monotonicity/persistence, mock lifecycle, recorded-response replay, reconnect recovery from duplicate/missing/out-of-order messages).
  3. Both idempotency protocols work: v2 `client_order_id` reconcile-before-retry, and the legacy intent-log (persist intent, send once, reconcile across watch/wait/done/cancel + balances, at most one unresolved identical intent per (market, side, parameter) tuple, else stop for manual reconciliation); a forced restart never duplicates orders and reconciles against live balances/open orders on startup.
  4. An independent watchdog / separate reconciliation detects a dead stop monitor or a failed cancel.
  5. The final artifact is frozen and hashed (source commit/hash, config hash, simulator version, dataset hash, cost model, stop mechanism, statistical protocol) with the backtest importing nothing from the live-broker package, and all Gate-3 pre-live risk limits + escalation policies are recorded before the holdout result is known.
**Plans**: TBD (~4)

Plans:
- [ ] 05-01: Strategy freeze (FRZ-01) — lock signal/params/sizing/stop/cost/protocol before broker code
- [ ] 05-02: Mock `BithumbBroker` + dual-auth construction + M6A test suite (schema, replay, reconnect)
- [ ] 05-03: Two idempotency protocols, durable transactional state, startup reconciliation, independent watchdog
- [ ] 05-04: Final artifact freeze + hash (package-boundary enforced) and Gate-3 risk-limit recording

### Phase 6: One-Time Holdout Evaluation
**Goal**: Evaluate the frozen, hashed final strategy on the holdout exactly once against the Boolean E∧R table — the trustworthy verdict this project exists to produce.
**Depends on**: Phase 5 — specifically both FRZ-02 (artifact hash) and FRZ-03 (Gate-3 limits) must be complete. This boundary is inviolable.
**Requirements**: HOLD-01
**Gate**: No new gate; consumes frozen Gate-1/2/3 decisions.
**Success Criteria** (what must be TRUE):
  1. The holdout is opened exactly once, only after FRZ-02 and FRZ-03 are both complete.
  2. The frozen final strategy runs on the final hashed artifact against the frozen Boolean E∧R table, producing an explicit Accept/Reject verdict.
  3. No strategy, simulator, cost, or protocol change occurs during or after the holdout run without explicitly burning the holdout per the material-change definition (forward data is the only valid replacement under Option B).
**Plans**: TBD (~1)

Plans:
- [ ] 06-01: Open holdout once; evaluate frozen hashed artifact against the E∧R table; record verdict

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Safety Foundation + Bithumb Spec Adapter | 0/4 | Not started | - |
| 2. Data Pipeline + Conservative Execution Simulator | 0/4 | Not started | - |
| 3. Preregistered Validation Protocol | 0/3 | Not started | - |
| 4. Baseline + Adaptive Modules Under the Gate | 0/4 | Not started | - |
| 5. Strategy Freeze, Mock-Broker Scaffolding + Gate-3 | 0/4 | Not started | - |
| 6. One-Time Holdout Evaluation | 0/1 | Not started | - |
