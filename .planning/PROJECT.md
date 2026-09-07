# Bithumb Autotrading Bot

## What This Is

A ground-up, clean-room **Python** rebuild of a single-asset, long-or-cash
crypto trading bot targeting the **Bithumb** KRW spot venue. It replaces an
earlier Upbit paper bot (`bot.py`) whose backtest is invalid because it fills at
the signal candle's close ("same-candle execution bias"). The rebuild's premise
is honesty over optimism: the working prior is that **net alpha is ≤ 0 after
costs**, so the first-class deliverable is a *venue-aware conservative execution
simulator* and a *preregistered walk-forward validation protocol* — not a better
signal. It is for the developer/operator building and validating the strategy
before any real capital is ever risked.

## Core Value

**A trustworthy verdict on whether the strategy has real, cost-and-execution-
honest edge** — produced by a frozen artifact evaluated on a holdout opened
exactly once. Execution realism removes fictitious profit; it does not create
alpha. If everything else fails, the validation machinery must not lie.

## Requirements

### Validated

(None yet — ship to validate)

### Active

<!-- v1 scope = Gate 1 → M0–M5 → M6A → final freeze → one-time holdout.
     No real orders, no trade key, no real money in this milestone. -->

- [ ] Gate-1 pre-build decisions frozen in `config`/docs (venue, market,
      timeframe, stop mechanism, calibration Option B, data + L2 source,
      simulator fidelity, backtest order policy)
- [ ] M0 — scope freeze, safety rails, and three-class API key policy
      (public / account-read / trade), withdrawal permanently disabled; startup
      self-check refuses to run if any Gate-1 decision or denominator is missing
- [ ] M1 — authenticated read-only Bithumb spec/fee/tick/min-order adapter
      (`/v1/orders/chance` via JWT; per-experiment fee recording)
- [ ] M2 — data pipeline + **venue-aware conservative execution simulator** with
      temporal separation (fill ≥ t+1), intrabar + gap rule, the Gate-1
      stop-limit state machine, cost model, and observe-only provisional
      calibration *(the core deliverable)*
- [ ] M3 — preregister the full research protocol (Gate 2): metrics, folds,
      thresholds, statistical tests, E∧R Boolean acceptance table
- [ ] M4 — implement and evaluate the preregistered baseline candidate(s) within
      selection windows only
- [ ] M5 — add modules one at a time (ATR sizing, ATR vs fixed-% stop, trailing
      stop, vol-target overlay, regime gate, kill-switch), keep/drop by the gate
- [ ] M5 strategy freeze → M6A mock/read-only broker scaffolding + idempotency /
      reconciliation / watchdog code (no trade key) → final artifact freeze +
      hash → Gate-3 risk limits recorded
- [ ] Open the final holdout **once** and evaluate the frozen hashed artifact
      against the E∧R table

### Out of Scope

- **M6B real-exchange integration** — requires a live trade key + real fills;
  deferred to a later milestone (this build stays code-complete, no real orders)
- **M7 limited live pilot / M8 automated operation** — require funded isolated
  subaccount and live risk; out of this milestone
- **Multi-coin / cross-sectional universe** — v1 is single-asset KRW-BTC
  long-or-cash; survivorship/point-in-time work deferred to v2+
- **Shorting / long-short / market-neutral / pairs** — not reproducible on
  Bithumb KRW spot
- **Kelly sizing** — unjustifiable under a zero-or-negative alpha prior for v1
- **Market-making / grid** — blows up in trends without inventory management;
  skip for v1
- **Cross-border kimchi-premium arbitrage** — capital controls, non-atomic
  cross-exchange fills; at most log the premium as a candidate context feature
- **Porting the existing Upbit `bot.py`** — clean-room rebuild; reuse concepts,
  not code

## Context

- **Source of truth:** `docs/RESEARCH.md` (strategy landscape, validation
  theory, Bithumb integration facts, security) and `docs/EXECUTION.md` (ordered
  milestone spine + three-gate Decision Register). Both are the product of eight
  rounds of independent audit and are treated as authoritative. A referenced
  `docs/RESEARCH_REVIEW.md` (evidence audit) is not yet in this repo.
- **The defect being fixed:** the prior bot computed signals from closed candles
  (fine) but filled at that same candle's close (impossible). No indicator,
  regime layer, or ML model can fix a wrong fill model — hence simulator-first.
- **Bithumb facts that constrain design:** general v2 endpoint (`limit`,
  `price`, `market`, `best`) has **no stop-market**; a legacy automatic-order
  endpoint offers an exchange-side **stop-limit** (still not a guaranteed exit,
  separate `Api-Key`/`Api-Nonce`/`Api-Sign` auth, no `client_order_id`); private
  WS is **v2**; rate limits are **per-channel**; native candles 1/3/5/10/15/30/
  60/240 min (no native 6h — aggregate from 60m/trades, never from 240m).
- **GSD research supplements, does not override:** the project-research agents
  run to enrich stack/features/architecture/pitfalls, but `docs/` remains the
  frozen research + build spine.

## Constraints

- **Tech stack**: Python — clean-room rebuild; exact-decimal money type for all
  price/qty/PnL; structured logging; secrets in env/secret store (`.gitignore`d)
- **Venue**: Bithumb KRW spot only; long-or-cash single-asset (KRW-BTC baseline)
- **Timeframe**: native 240-minute (4h) higher-timeframe baseline; fix a 24/7
  annualization convention (Gate 2)
- **Security**: three key classes, no trade key before M6B (out of this
  milestone), withdrawal permission permanently disabled on every key,
  IP-restricted trade key, application-level idempotency with
  reconcile-before-retry and an independent watchdog
- **Validation discipline**: three disjoint data roles (train → selection →
  holdout); freeze *and hash* the artifact before the holdout; a material
  post-freeze execution/cost change **burns the holdout** (Option B: only forward
  data is a valid replacement)
- **Risk arithmetic**: planned loss ≠ max market loss ≠ max operational loss;
  the isolated 1–5% sleeve — not the stop formula — is the real capital cap
  (though live sleeve funding itself is out of this milestone)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Milestone scope = Gate 1 → M0–M5 → M6A → final freeze → one-time holdout | M6B/M7/M8 need a real trade key + money; keep this build code-complete and holdout-tested without live risk | — Pending |
| Fresh Python rebuild; do not port `bot.py` | `bot.py` is an Upbit bot with an invalid fill model; reuse concepts not code | — Pending |
| Calibration Option B (all real orders in M6B; M2 observe-only) | No trade-permission key before M6B, unconditionally | — Pending |
| Simulator-first, not signal-first | A wrong fill model invalidates every performance number regardless of signal quality | — Pending |
| `docs/RESEARCH.md` + `docs/EXECUTION.md` authoritative; GSD research supplements | 8 rounds of audit already invested; GSD agents enrich, not replace | — Pending |
| Gate-1/2/3 decisions resolved per-milestone at discuss/plan time | Each gate is frozen immediately before the stage it governs, per the Decision Register | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-09-07 after initialization*
