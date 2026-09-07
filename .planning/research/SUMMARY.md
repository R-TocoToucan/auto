# Research Synthesis Summary: Bithumb Autotrading Bot

**Date:** 2026-09-08 | **Scope:** Gate-1 → M0–M5 → M6A → freeze → one-time holdout | **No live trading this milestone**

> Supplemental synthesis. The authoritative design remains `docs/RESEARCH.md` and
> `docs/EXECUTION.md` (eight rounds of audit). This file verifies current facts,
> fixes a concrete Python stack, operationalizes architecture boundaries, and adds
> implementation-level pitfall depth. Items flagged "verify at build time" must be
> confirmed against `apidocs.bithumb.com` before M1/M6A.

## Executive Summary

This project rebuilds a crypto autotrading bot to fix a critical flaw in the predecessor (`bot.py`): same-candle execution bias invalidating all backtest results. The remedy is threefold: (1) build a custom venue-aware conservative execution simulator enforcing temporal separation (`fill ≥ t+1`), (2) establish a preregistered walk-forward validation protocol with nested data roles and multiple-testing correction, (3) implement a frozen-artifact, hashed-checkpoint regime opening a one-time holdout only after all design decisions are locked.

**Recommended approach:** Python 3.12/3.13 with `decimal.Decimal` for money, `httpx` + `websockets` + `PyJWT` for Bithumb (custom thin client, not third-party SDK), `arch` for block-bootstrap/SPA, `pandas`/`numpy` for analytics, `pydantic` for config. **Do not** adopt vectorbt, backtesting.py, backtrader, or nautilus_trader as the M2 simulator — they lack Bithumb-specific stop-limit state-machine modeling and cannot guarantee the temporal-separation property this rebuild exists to enforce.

**Key risks:** (1) Temporal separation re-enters via helper functions — mitigated by a single chokepoint `ExecutionSimulator` with static checks; (2) multiple-testing mismatched to objective (DSR vs. Calmar) — mitigated by explicit Gate-2 preregistration and unit-tested acceptance code; (3) holdout leakage via debugging/simulator changes — mitigated by physical data separation, access logging, and the material-change definition re-burning the holdout; (4) Bithumb API facts (private WS v1 vs v2, legacy stop-limit fees, pagination cursor semantics) must be re-verified at M1/M6A build time against `apidocs.bithumb.com`.

## Technology Stack Findings

**Core Technologies (HIGH confidence):**
Python 3.12/3.13, `decimal.Decimal` (stdlib), `httpx` ≥0.27, `websockets` ≥16.0, `pandas` + `numpy`, `pyarrow` ≥16, `pydantic` v2 ≥2.9 (strict mode for monetary fields — beware float coercion), `structlog` ≥24, `PyJWT` ≥2.9 (claims structure must be re-verified at M1), `arch` ≥7.2 (single best find: covers both block-bootstrap AND White's Reality Check/SPA test). Dev tooling: `uv`, `ruff`, `mypy --strict`, `pytest` + `hypothesis`.

**Execution Simulator — VERDICT: BUILD CUSTOM (~500–1000 lines)**
- NOT vectorbt, backtesting.py, backtrader, or nautilus_trader (all lack Bithumb stop-limit FSM and cannot guarantee temporal separation; nautilus_trader has an open upstream issue #4063 for next-bar-open execution)
- Build: cost model (fees + modeled slippage), stop-limit state machine (watch→wait→done/cancel), gap rule, fill ≥ t+1 enforced by construction

**Bithumb Integration — VERDICT: BUILD CUSTOM THIN CLIENT (~150–300 lines)**
- NOT python-bithumb, ccxt, or bithumb-pro/bithumbfutures (incomplete coverage; note bithumb-pro/futures docs describe a DIFFERENT product from the target `apidocs.bithumb.com` KRW-spot venue)
- Build: two auth adapters (JWT for v1/v2; legacy HMAC `Api-Key`/`Api-Nonce`/`Api-Sign` for `/trade/stop_limit` if Gate-1 selects it), typed models, per-channel token-bucket rate limiting

**Data Storage:** Parquet files (append-only) + manifest (SHA-256, row count, timestamp per file); prices/quantities as `decimal128` or fixed-format strings; no database for v1.

**M1/M6A Build-Time Verification Against `apidocs.bithumb.com`:**
1. Private WS endpoint path (v1 vs v2 — direct conflict in current sources)
2. JWT claim names / hash construction for v2 private endpoints
3. Legacy `/trade/stop_limit` fee (may differ from general endpoint)
4. Candle pagination cursor semantics (must test empirically — inclusive vs exclusive at page boundaries)
5. Per-channel rate-limit figures

## Features: Prerequisites + Differentiators + Exclusions

**Table Stakes (verdict untrustworthy without):**
Three-class key policy + withdrawal-disable | Exact-decimal money | Queried fees | Temporal separation (fill ≥ t+1) | Full-OHLC + gap rule | Stop-limit FSM (if legacy) | Conservative simulator | Robust data pipeline | Full preregistration | Nested walk-forward | One-time frozen holdout | Multiple-testing correction matched to objective | Block bootstrap with paired resampling | Boolean E∧R table | Baseline-first evaluation | Module-at-a-time addition | Kill-switch + watchdog | Structured alerting | Idempotency per endpoint type | Risk-denominator vocabulary | Capital-isolation | Startup self-check

**Differentiators (kept only if they clear the preregistered E∧R gate within selection windows):**
ATR sizing | ATR vs. fixed-% stop | Trailing stop | Volatility-target overlay | Regime gating | Kimchi-premium logging | L2 order-book slippage | Rule-based strategy selection. Layer 3 meta-adaptation (rule-based selection, HMM) only after Layers 1–2 pay off.

**Anti-Features (deliberately excluded v1):**
Shorting/long-short/pairs | Kelly sizing | Market-making/grid | Multi-coin universe | ML/DL primary signal | RL for sizing | Cross-border kimchi arbitrage | Live trading (this milestone) | Porting bot.py | HMM regime models (v1)

**Feature Dependencies:**
Gate-1 → M0 key policy → M1 fee/tick adapter → M2 simulator → M3 preregistration (Gate-2) → M4 baseline → M5 modules (one at a time, kept only if gate passes). Conditional branches: stop-limit FSM + its idempotency protocol exist only if Gate-1 selects the legacy stop-limit; L2 calibration exists only if the cost model makes slippage depth-dependent (Gate-1/Gate-2) and depends on the Gate-1 L2 source (Bithumb's book endpoint is snapshot-only).

## Architecture: Offline Core + Isolated Broker

**Two Hard Rules:**
1. Nothing fills earlier than t+1 relative to the signal candle.
2. The artifact for the holdout must be frozen + hashed; `core/` (offline, deterministic, hashed) never imports from `broker/` (live, import-isolated).

**Components:**
- **Config loader:** immutable Gate decisions; startup self-check fails closed if anything missing
- **BithumbSpec (M1):** JWT query of `/v1/orders/chance`; hashed snapshot artifact (never re-queried during simulation)
- **BithumbData (M2):** pagination (≤200/req), dedupe, tz validation, gap handling; immutable Parquet + SHA-256 per file; 6h aggregated from 60m (never 240m), as a re-verifiable pure function over the 60m store
- **Simulator (M2):** pure, deterministic, offline; enforces fill ≥ t+1, intrabar/gap rule, stop-limit FSM, cost model
- **Strategy/Signal layer:** baseline (M4) + swappable modules (M5); one interface; evaluated in isolation; dropped (not retuned) on gate failure
- **Validation/walk-forward harness:** owns three disjoint data roles; computes metrics, PBO/CSCV, DSR, block-bootstrap CIs; applies E∧R table; **physically withholds the holdout** (runtime guard throws on holdout access during selection)
- **Artifact freeze/hash boundary:** build gate (not a running component); after M6A, snapshot the six hashes
- **BithumbBroker package (M6A mock / M6B live):** order placement, both auth schemes, idempotency, reconciliation, watchdog; import-guarded (core never imports broker) — CI-enforced import-direction rule, established EARLY (M0/M1), not retrofitted at freeze
- **Watchdog:** independent process/thread; detects a dead stop monitor or failed cancel; halts even if the primary is unresponsive

**Project Structure:**
```
src/config/                # Gate decisions + ResolvedConfig + hash
src/spec_snapshot/         # Shared contracts (no I/O)
src/bithumb_spec/          # M1 — authenticated READ
src/bithumb_data/          # M2 — ingestion + immutable store
src/core/                  # === HASHED ARTIFACT ===
  simulator/               #   fill >= t+1, cost, FSM
  strategy/                #   baseline + modules
  validation/              #   preregistration + walk-forward
src/broker/                # M6A mock / M6B live — ISOLATED
src/artifact/              # Freeze/hash (build gate)
```

## Critical Pitfalls: 14 Implementation Traps

**Top 5 by impact:**

1. **Same-candle execution bias re-enters via helpers.** Prevent: single chokepoint `ExecutionSimulator.submit(intent, decided_at=t)`; static/lint check that no module reads candle close/high/low for the same index it used to generate the signal. Phase: M2 build, M5 regression.
2. **Multiple-testing tool mismatched to objective.** Prevent: DSR (Sharpe-based) not used for Calmar; `if dsr < P_threshold: reject` (DSR is a probability in [0,1], never "≤0"); unit-test with synthetic pass/fail fixtures. Phase: M3 (Gate-2).
3. **Two separate CIs instead of a paired-resampling test.** Prevent: one function taking aligned (strategy, buy-and-hold) arrays, drawing shared block indices per iteration, computing the Δmetric. Phase: M3 (Gate-2).
4. **Block-length sensitivity unchecked.** Prevent: before freezing block length in Gate-2, run E/R at 2–3 candidate lengths; confirm the verdict is stable; commit a sensitivity table. Phase: M3 (Gate-2).
5. **Holdout leakage via debugging/simulator changes.** Prevent: physically partition holdout data; log every access; treat any accidental pre-open view as leakage; enforce the material-change definition (burns the holdout → forward data only). Phase: M2 (physical separation) through GATE-3-FREEZE (audit access log).

**Other 9:** PBO over-trusted (narrow scope) | Look-ahead in indicators | Decimal misused (`Decimal(<float literal>)`) | Pagination/timezone bugs | 6h bin-origin mismatch (`resample('6h')` with no explicit origin) | Private WS v1-vs-v2 conflict (unresolved) | WebSocket reconnect silent sequence-gap desync | JWT clock-skew auth failures | Statistical vs. implementation overfitting conflated (a strategy can pass every preregistered test yet be unstable to simulator implementation details).

## Roadmap Implications: M0–M5 → M6A → Freeze → Holdout

| Phase | Purpose | Key Deliverables | Pitfalls Prevented |
|-------|---------|------------------|--------------------|
| M0 | Config, startup self-check, key policy, Decimal lint | Decision-Register schema, three-class key policy, risk-denominator vocabulary, Decimal lint rule, core/broker import boundary scaffolding | 8, 13 |
| M1 | BithumbSpec adapter, fee snapshot | JWT query of `/v1/orders/chance`, hashed fee/tick/min-order snapshot, per-channel rate limiting | 11, 13, 9 |
| M2 | Data pipeline + simulator (CORE DELIVERABLE) | Ingestion (pagination, dedupe, tz, gap, immutable Parquet), 6h-from-60m aggregation, Simulator (fill ≥ t+1, intrabar/gap, cost, FSM), chokepoint interface | 1, 7, 8, 9, 10, 14 |
| M3 | Preregistration (Gate-2) | Metric choice, fold structure, block-bootstrap length + sensitivity table, statistical test, E∧R table, per-stage test-scope documentation | 2, 3, 4, 5 |
| M4 | Baseline evaluation | Preregistered baseline rule run through M2+M3 over selection windows only | 6 (holdout guard) |
| M5 | Modules (one at a time) | Each module (ATR sizing, stops, regime, vol-target, kill-switch) evaluated in isolation; kept only if the E∧R gate passes | 1, 5, 6, 7 |
| M5-Freeze | Strategy lock | Freeze all strategy choices before live-broker code | — |
| M6A | Mock broker + idempotency + watchdog | Order placement/cancel, idempotency (v2 client_order_id; legacy intent-log), reconciliation, watchdog, recorded-response replay + schema + auth-construction tests | 12, 11, 6 |
| Gate-3-Freeze | Artifact hash + risk limits | Freeze manifest (six component hashes), material-change definition, Gate-3 risk limits, holdout data physically separated | 6 |
| Holdout | Evaluate frozen artifact | Frozen strategy through frozen simulator on holdout data against the frozen E∧R table (opened exactly once) | 6 |

## Confidence Assessment

- **Stack:** HIGH (core choices); MEDIUM-HIGH (Bithumb facts requiring M1 verification); MEDIUM (alternatives from web research)
- **Features:** HIGH (table-stakes and anti-features from the 8-audit-round docs); MEDIUM (ordering from docs + patterns)
- **Architecture:** HIGH (boundaries/responsibilities from RESEARCH.md/EXECUTION.md and project structure); MEDIUM (general pattern corroboration from web search only)
- **Pitfalls:** HIGH (pitfalls 1, 2, 3, 5, 6, 9, 14 cross-checked against authoritative docs); MEDIUM (4, 7, 8, 13 from web research); LOW (11 — factual WS-version conflict unresolved)
- **Roadmap phases:** MEDIUM-HIGH (dependencies flow naturally; sequencing matches the docs' build order; durations TBD)

## Sources

- `docs/RESEARCH.md`, `docs/EXECUTION.md` — authoritative, eight-round-audited (read, unmodified)
- `.planning/research/STACK.md`, `FEATURES.md`, `ARCHITECTURE.md`, `PITFALLS.md` — this pass
- Build-time verification required against `apidocs.bithumb.com` for all items flagged above

---
*Synthesized 2026-09-08. Orchestrator-persisted after a #222 synthesizer false-refusal (agent returned the document inline instead of writing it).*
