# Feature Research

**Domain:** Single-asset (KRW-BTC), long-or-cash crypto backtest + validation system for Bithumb spot (no live trading in this milestone)
**Researched:** 2026-09-07
**Confidence:** HIGH (synthesized directly from `docs/RESEARCH.md` / `docs/EXECUTION.md`, the product of eight rounds of independent audit and treated as this project's authoritative source; no external ecosystem claim in this file contradicts those docs)

## Feature Landscape

### Table Stakes (Users Expect These)

"Users" here = the operator/developer who must trust the verdict. Missing any of these makes the validation verdict untrustworthy, not just less polished — this is the crypto-backtesting equivalent of "a community platform without user profiles."

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Three-class API key policy (public / account-read / trade) + withdrawal permanently disabled on every key | Without this, "no live risk" is a promise, not an enforced boundary; the trade key literally cannot exist before M6B | LOW | M0. Withdrawal-disabled is permanent on *every* key class, forever, not just pre-M6B |
| Exact-decimal money type for all price/qty/PnL | Float arithmetic on money silently corrupts every downstream metric (Sharpe, drawdown, fee totals) | LOW | Use `Decimal`, not float, project-wide from M0 |
| Queried (not assumed) fees, recorded per experiment; non-promotional cost stress scenarios | A promo fee-free window already expired once; hard-coding it would silently understate real cost | LOW–MEDIUM | M1, via `/v1/orders/chance`. Never multiply a possibly-zero fee for stress testing — use non-promo reference + absolute bp scenarios |
| Temporal separation: fill no earlier than *t+1* | This is the exact defect (same-candle execution bias) that invalidated the prior bot; the whole rebuild exists to fix it | LOW–MEDIUM | M2. Must be structurally impossible to violate, not just usually avoided |
| Full-OHLC intrabar evaluation + explicit gap rule for stops/exits | Close-only stop checking hides intrabar stop hits, gaps, and candles that touch both stop and target | MEDIUM | M2. Predeclare the worse-outcome assumption before ever running a candidate |
| Stop-limit state machine (`watch → wait → done/cancel`) if the legacy stop-limit is in scope | A stop-limit is not a market order; naively reusing the market-exit gap rule can fabricate a fill that never happened (gap-below-limit ⇒ no fill, retained exposure) | HIGH | M2, only if Gate-1 selects v2+legacy. Deterministic candle-only rule must be preregistered, not left to implementation discretion |
| Venue-aware conservative execution simulator (candle-only minimum fidelity) | This is the explicit "core deliverable" of the milestone — everything else evaluates against it | MEDIUM–HIGH | M2. "Conservative," not "venue-faithful" — aggregated public depth can't reveal queue position or hidden liquidity, so bias every unknown toward the worse outcome |
| Robust data pipeline: pagination (≤200/req), dedupe, timezone validation, gap handling, immutable store with integrity checks | Bithumb's REST candle endpoint paginates and can silently duplicate/gap; an unvalidated store poisons every backtest run on top of it | LOW–MEDIUM | M2. 6h aggregation (if used) must come from 60m/trades, never 240m — 6 isn't a multiple of 4 |
| Full preregistration of the research protocol before evaluating any candidate | Without this, every "result" is contaminated by hindsight — the entire point of the holdout depends on this ordering | MEDIUM–HIGH | M3 (Gate 2): metric, folds, thresholds, tests, and the E∧R Boolean table all frozen before M4 runs |
| Nested walk-forward across three disjoint data roles (train → selection → holdout) | Prevents the selection process from leaking into the number that's supposed to confirm it | MEDIUM–HIGH | M3 design, M4/M5 execution. Selection only inside train+selection windows, never the holdout |
| One-time holdout, opened exactly once, against a frozen+hashed artifact | This is the entire credibility mechanism of the project; a holdout that can be "peeked and rerun" is not a holdout | MEDIUM | Final artifact freeze happens *after* M6A so shared-code bugs can't force a post-holdout redo |
| Multiple-testing correction matched to the primary objective (DSR if Sharpe-based; PBO/CSCV; an appropriate resampling procedure if the objective is Calmar) | Every extra candidate evaluated inflates the odds of a false positive; DSR misapplied to a non-Sharpe objective would validate a lie | MEDIUM–HIGH | M3/M4/M5. Log every candidate ever evaluated — the multiple-testing correction needs the count |
| Block bootstrap on the aligned per-period excess-return series, with a **paired** resampling test for the R (risk-adjusted) benchmark | A single terminal cumulative return, or two separate CIs compared informally, is not equivalent to a paired test on Δmetric — especially wrong for path-dependent metrics like Calmar/max-drawdown | MEDIUM–HIGH | M3 design (block length, confidence level, autocorrelation, 24/7 annualization convention), M4/M5/holdout execution |
| Explicit Boolean E∧R acceptance table | Removes discretion at the moment discretion is most tempting (after seeing a good-looking number) | LOW | M3. Accept only on E AND R; anything else is Reject |
| Baseline-first evaluation (cash, buy-and-hold, preregistered baseline) before any added module | Without a floor to beat, "the bot made money" is meaningless — must beat both doing nothing and just holding | LOW–MEDIUM | M4 |
| Module-at-a-time addition, kept only if it clears the acceptance gate within selection windows | Prevents the "kitchen sink" strategy where nobody can tell which piece (if any) is actually responsible for performance | MEDIUM | M5. A module that fails the gate is dropped, not retuned — retuning-until-it-passes is exactly the leak this rule prevents |
| Kill-switch (max-drawdown) + tiered daily-loss halt + independent watchdog | "Cancel all orders on disconnect" fails precisely when the connection is the problem; a config value doesn't stop a bug from trading the whole account | MEDIUM | M5 (halts) / M6A (watchdog). Halts operate on **bot sleeve equity**, not total equity |
| Structured alerting (fills, errors, kill-switch trips, daily P&L) | An unattended failure mode with no alert is functionally the same as no kill-switch at all | LOW | M6A-adjacent; cheap to build, expensive to skip |
| Idempotency protocol per order-endpoint type (v2 `client_order_id` reconcile-before-retry; legacy intent-persist-then-reconcile-across-all-states for the no-`client_order_id` stop-limit) | The two Bithumb order surfaces have genuinely different failure semantics; one dedup strategy does not cover both | HIGH | M6A. Never auto-resubmit on timeout — the first request may have succeeded |
| Risk-denominator vocabulary + unit-safe sizing (`planned_stop_loss` ≠ `max_market_loss` ≠ `max_operational_loss`) | Conflating "the stop distance" with "the maximum possible loss" is the single easiest way to under-estimate real risk | LOW–MEDIUM | M0 definitions, used everywhere sizing appears |
| Capital-isolation mechanism design (technically isolated sleeve, aggregate-exposure enforcement) | A sleeve that's only a config percentage, not an isolated subaccount/enforced cap, is not actually a cap | MEDIUM | Designed in this milestone (M0/Gate 3); funding the sleeve with real money is out of scope until M7 |
| Startup self-check that refuses to run with any missing Gate-1 decision or denominator | Converts "we forgot to decide X" from a silent bug into a hard stop | LOW | M0 |

### Differentiators (Competitive Advantage — here, "must earn acceptance," not "nice UX")

In this domain, a differentiator isn't a growth lever — it's an unvalidated hypothesis that is *kept only if it clears the preregistered E∧R gate on out-of-sample selection-window evidence.* Each one can legitimately make the final strategy worse; that's the point of gating them individually.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| ATR-based per-trade position sizing (replacing fixed `position_fraction`) | Sizes risk in KRW terms consistently, rather than a fixed % of cash regardless of volatility | MEDIUM | M5. Must keep fees/gap buffer inside the unit-safe formula (see docs); sizing and stop-distance stay separate mechanisms |
| ATR stop vs. fixed-% stop (compared as competing hypotheses) | Volatility-adaptive stop *might* reduce whipsaw-driven losses in choppy regimes | MEDIUM | M5. Compared under one shared risk budget/cost/gap/sizing rule — do not assume ATR wins |
| Trailing stop (e.g., chandelier) | Lets winners run further than a fixed stop in sustained trends | LOW–MEDIUM | M5. Independent exit hypothesis, evaluated on its own merits |
| Volatility-target overlay (portfolio-level) | Could reduce realized volatility of the equity curve | MEDIUM | M5. Cited large-sample equity evidence found vol-managed portfolios did *not* systematically outperform OOS — keep only if net-positive in selection windows, expect it may reduce return too |
| Regime gating (ADX trend strength + long-MA direction + ATR volatility state, with anti-whipsaw guardrails) | If regime-dependent edge is real, gating could avoid "ranging/volatile" chop where almost nothing works | HIGH | M5. Many degrees of freedom (thresholds, MA horizon, confirmation, cooldown, max trades/day) — itself overfit-prone; must clear the gate, is not assumed to help |
| Kimchi-premium context feature (logged, not traded) | A documented, real KRW/global price divergence; *might* carry predictive information as an input feature | LOW (as a logged feature) | Log-only for v1. Nonlinear/threshold mean-reversion evidence exists but does not establish a profitable signal — treat strictly as an unproven candidate feature, never as arbitrage |
| L2 order-book-calibrated slippage/depth modeling | More realistic cost model than candle-only spread assumptions, if slippage is depth-dependent | HIGH | M2, **required only if** the cost model makes slippage a function of depth; depends on the Gate-1 L2 source decision (forward collection, verified third-party historical dataset, or candle-only + separate forward validation) — Bithumb's public order-book endpoint is snapshot-only, not a historical archive |
| Rule-based strategy selection / meta-adaptation (the regime gate acting as selector) | A ceiling on adaptivity that's simpler than an HMM, at the cost of still-many tunable parameters | MEDIUM–HIGH | Layer 3, only attempted if Layers 1–2 (regime gate, sizing/exits) already pay off in walk-forward. Statistical regime models (HMM) are explicitly a step beyond this, not v1 |

### Anti-Features (Deliberately Excluded for v1)

These read as reasonable feature requests in isolation. Each is excluded here for a specific, documented reason — not because it's a bad idea in general.

| Feature | Why Requested | Why Problematic (for this project, now) | Alternative |
|---------|---------------|------------------------------------------|-------------|
| Shorting / long-short / pairs trading | The strongest published crypto momentum and mean-reversion evidence is cross-sectional long/short | Not reproducible on Bithumb KRW spot at all (no shorting on spot); the cited pairs/momentum literature is a *different strategy category* and cannot be inherited | Research-only direction for a future futures/long-short system, v2+ |
| Kelly sizing | Theoretically size-optimal if the edge and loss distribution are known | Unjustifiable under this project's working prior of zero-or-negative net alpha; also hard to estimate reliably from thin single-asset data | Fixed risk-fraction (0.05–0.10% of equity) sizing until robust, uncertainty-adjusted return/loss estimates exist |
| Market-making / grid | Looks attractive in range-bound markets, low apparent drawdown in backtests | Blows up in trending markets without real inventory management; needs latency this project isn't built for | Skip entirely for v1; trend-following long-or-cash is the only strategy family in scope |
| Multi-coin / cross-sectional universe | More assets = more diversification, more data | Requires survivorship-bias-free, point-in-time universe construction that isn't built; adding coins without it silently reintroduces look-ahead bias | Single-asset KRW-BTC baseline only; multi-coin deferred to v2+ with its own PIT/survivorship work |
| ML / deep-learning price prediction as a primary signal ("price oracle") | Promises to "learn" edge without hand-specifying it | Reported results in this space are overwhelmingly forecast-accuracy metrics, not net-of-cost P&L, and are leakage/survivorship-prone; a wrong fill model can't be fixed by a smarter model anyway | If ML is used at all, restrict to a narrow sub-problem (e.g., regime classification) under purged/embargoed CV — never as the entry/exit signal itself |
| RL for sizing/execution | Sounds like a natural fit for sequential decision-making | Research-grade, hard to validate honestly, easy to overfit to simulator quirks | Research direction only, explicitly out of v1 |
| Cross-border kimchi-premium arbitrage | The premium is real and sometimes large | Requires atomic cross-exchange execution that doesn't exist; Korean capital controls and cross-exchange transfer/timing risk make it non-reproducible safely | Log the premium as a context feature only (see Differentiators); no arbitrage trading |
| Live trading / real trade key / real orders (M6B, M7, M8) | The natural "next step" after a good backtest | This milestone's entire premise is validating honestly *before* risking capital; a trade key literally cannot exist under the M0 three-class key policy before M6B | Explicitly deferred milestones, gated behind the one-time holdout and Gate-3 risk limits |
| Porting/reusing the existing Upbit `bot.py` | Avoids "reinventing" already-written code | `bot.py`'s core defect (same-candle execution bias) is structural to how it fills orders; porting the code risks porting the bug | Clean-room rebuild; reuse concepts (e.g., the general strategy shape), never the execution code |
| Statistical regime models (HMM) as a v1 feature | More adaptive than fixed ADX/MA thresholds | More overfit-prone; only justified after a simpler rule-based regime gate is already solidly validated in walk-forward | Defer past the rule-based regime gate (Layer 1); only revisit if that layer clears the gate |

## Feature Dependencies

```
Gate-1 pre-build decisions (venue, market, timeframe, stop mechanism, L2 source, order policy)
    └──requires──> M0 key-class policy + risk denominators + startup self-check
                       └──requires──> M1 authenticated fee/tick/min-order adapter
                                          └──requires──> M2 conservative execution simulator
                                                             ├──requires──> Stop-limit state machine (only if legacy stop-limit selected at Gate 1)
                                                             ├──requires──> L2 calibration (only if cost model makes slippage depth-dependent; needs Gate-1 L2 source)
                                                             └──requires──> M3 preregistration (Gate 2: metrics, folds, tests, E∧R table)
                                                                                └──requires──> M4 baseline evaluation (cash + buy-and-hold + baseline candidate)
                                                                                                   └──requires──> M5 module-by-module additions:
                                                                                                                      ATR sizing ──precedes──> Volatility-target overlay (overlay sits on top of sizing)
                                                                                                                      ATR stop vs fixed-% stop (independent of sizing choice, same risk budget)
                                                                                                                      Trailing stop (independent exit hypothesis)
                                                                                                                      Regime gating (Layer 1) ──precedes──> Layer 3 rule-based selection/meta-adaptation
                                                                                                                      Regime gating ──precedes──> HMM/statistical regime models (v2+, not attempted in v1)
                                                                                                                      Kill-switch / tiered halts

M5 strategy freeze ──requires──> M6A mock/read-only broker + idempotency + reconciliation + watchdog
                                       └──requires──> M5-FINAL-FREEZE (hash complete artifact, source+config+sim+dataset+cost model+stop mechanism+stat protocol)
                                                          └──requires──> GATE-3-FREEZE (all pre-live risk limits recorded, before holdout result is known)
                                                                             └──requires──> One-time holdout opened once, evaluated against frozen E∧R table

Kimchi-premium context feature ──requires──> external (Upbit/global) price feed ingestion (a new data-source dependency, not currently in scope of M1/M2 Bithumb-only pipeline)
Idempotency protocol ──depends-on──> Gate-1 stop-mechanism decision (v2-only vs. v2+legacy — the two protocols differ structurally)
```

### Dependency Notes

- **Stop-limit state machine requires the Gate-1 stop-mechanism decision:** if Gate 1 selects v2-only (client-side trigger, no legacy automatic order), the entire `watch → wait → done/cancel` state machine and its dedicated idempotency protocol are unnecessary — build only what Gate 1 actually selects.
- **L2 calibration requires the Gate-1 L2 source decision:** Bithumb's public order-book endpoint is a snapshot API, not a historical archive, so L2 fidelity is only buildable once Gate 1 has resolved *which* of forward collection / third-party historical dataset / candle-only-plus-forward-validation will supply it. Do not assume L2 fidelity is available by default.
- **Regime gating precedes Layer 3 meta-adaptation:** the roadmap should not schedule rule-based strategy selection or HMM regime models before the underlying regime gate (Layer 1) and adaptive sizing/exits (Layer 2) have themselves cleared the acceptance gate — Layer 3 is explicitly conditional on Layers 1–2 "paying off."
- **Volatility-target overlay enhances ATR sizing, doesn't replace it:** the overlay is a portfolio-level layer on top of the per-trade sizing mechanism; sequence sizing before the overlay in any phase plan.
- **Preregistration (M3) requires the simulator (M2) to exist first:** you cannot fix statistical tests, fold boundaries, or the E∧R table's numeric thresholds without the cost model and stop-mechanism behavior already built — M3 is not a paperwork step that can be pulled earlier.
- **Final artifact freeze requires M6A, not just M5:** because the holdout evaluates the frozen *artifact* (source+config+data+broker scaffolding), not just the strategy concept, mock/read-only broker code must exist and be hashed before the holdout opens — this is why M6A is sequenced before the freeze rather than after.
- **Kimchi-premium feature conflicts with "Bithumb-only" pipeline scope:** logging it requires ingesting a second exchange's price feed, which is a genuinely new data dependency the M1/M2 milestones don't otherwise need — flag this explicitly if it's scheduled into a phase, rather than assuming it's free.

## MVP Definition

### Launch With (v1 = this milestone: Gate 1 → M0–M5 → M6A → final freeze → one-time holdout)

- [ ] Three-class key policy + permanent withdrawal-disable — the safety boundary this whole milestone depends on
- [ ] Exact-decimal money math — every metric downstream is wrong without it
- [ ] Queried fees + non-promotional cost stress scenarios — cheap, and cost realism is the whole point
- [ ] Temporal separation (fill ≥ t+1) + intrabar/gap rule — the specific defect being fixed
- [ ] Conservative execution simulator (candle-only minimum, stop-limit state machine if Gate 1 selects it) — the core deliverable
- [ ] Robust data pipeline (pagination, dedupe, gap handling) — nothing above it is trustworthy otherwise
- [ ] Full preregistration (Gate 2) before any candidate is evaluated
- [ ] Nested walk-forward + one-time frozen holdout + matched multiple-testing correction — the actual validation machinery
- [ ] Baseline-first evaluation (cash, buy-and-hold, baseline candidate) via the Boolean E∧R table
- [ ] Kill-switch, tiered halts, independent watchdog, structured alerting — required before any code that places even mock orders (M6A)
- [ ] Idempotency + reconciliation protocols for whichever order surface(s) Gate 1 selects

### Add After Validation (v1.x = M6B onward, explicitly out of this milestone)

- [ ] Real trade key + minimum-size live integration tests (M6B) — triggered only after the holdout is opened and evaluated
- [ ] Limited live pilot with isolated sleeve (M7) — triggered only if the frozen strategy passes the holdout under the E∧R table
- [ ] Any module (ATR sizing, ATR/trailing stops, vol-target overlay, regime gate) that fails its M5 acceptance gate stays dropped, not retried, in v1.x

### Future Consideration (v2+)

- [ ] Multi-coin / cross-sectional universe — defer until survivorship-bias-free, point-in-time universe machinery exists
- [ ] Shorting / long-short / pairs — defer to a separate futures/long-short research track; not reproducible on Bithumb spot
- [ ] Statistical regime models (HMM) — defer until the simpler rule-based regime gate is solidly validated
- [ ] ML applied to a narrow sub-problem (e.g., regime classification) under purged/embargoed CV — defer until there's a validated non-ML baseline to compare against
- [ ] RL for sizing/execution — research direction only
- [ ] Cross-border kimchi arbitrage — capital controls and non-atomic cross-exchange fills make this a non-starter; premium logging (v1) is the ceiling

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Three-class key policy + withdrawal disable | HIGH | LOW | P1 |
| Exact-decimal money math | HIGH | LOW | P1 |
| Conservative execution simulator (temporal separation + gap rule) | HIGH | MEDIUM–HIGH | P1 |
| Stop-limit state machine (conditional on Gate 1) | HIGH (if selected) | HIGH | P1 (conditional) |
| Data pipeline integrity | HIGH | LOW–MEDIUM | P1 |
| Preregistration + nested walk-forward + one-time holdout | HIGH | MEDIUM–HIGH | P1 |
| Multiple-testing correction (DSR/PBO/CSCV, matched to objective) | HIGH | MEDIUM–HIGH | P1 |
| Kill-switch + watchdog + alerting | HIGH | MEDIUM | P1 |
| Idempotency/reconciliation protocols | HIGH | HIGH | P1 |
| ATR sizing | MEDIUM | MEDIUM | P2 |
| ATR vs fixed-% stop | MEDIUM | MEDIUM | P2 |
| Trailing stop | MEDIUM | LOW–MEDIUM | P2 |
| Volatility-target overlay | LOW–MEDIUM | MEDIUM | P2 |
| Regime gating | MEDIUM | HIGH | P2 |
| L2 calibration | MEDIUM (conditional) | HIGH | P2 (conditional on cost model needing it) |
| Kimchi-premium logging | LOW | LOW | P3 |
| Rule-based meta-adaptation (Layer 3) | LOW (v1) | HIGH | P3 |

**Priority key:**
- P1: Must have — the validation verdict is untrustworthy without it
- P2: Should have — evaluated one at a time, kept only if it earns its place via the acceptance gate
- P3: Nice to have — logged/attempted only if P1/P2 are solid and time remains

## Standard Practice Comparison

Not a consumer product, so "competitors" is replaced with how this project's choices compare to common retail-bot and standard quant-research practice.

| Practice | Typical retail crypto bot | Standard institutional quant research | This project's approach |
|----------|---------------------------|----------------------------------------|--------------------------|
| Fill timing | Signal-candle close (same-candle bias) | Next-bar or order-book replay | Next-tradable-observation (≥t+1), gap-rule-aware — matches institutional practice, not retail default |
| Stop/TP evaluation | Close-only | Full OHLC + intrabar rule | Full OHLC + predeclared gap rule; stop-limit gets its own state machine |
| Fees/slippage | Fixed constants, often optimistic | Queried fees, modeled+calibrated slippage | Queried fees; slippage/impact modeled then calibrated against observed (later real) fills |
| Validation | Single in-sample backtest, "looks good" | Preregistered, nested walk-forward, holdout, multiple-testing correction | Full preregistration + one-time holdout + DSR/PBO/CSCV or Calmar-appropriate equivalent |
| Position sizing | Fixed % of cash | Risk-budget-based, unit-consistent | ATR-based KRW risk-budget sizing (P2, gated) replacing fixed-fraction |
| Safety | "Cancel orders on disconnect" | Independent watchdog, reconciliation, capital isolation | Independent watchdog + isolated sleeve + three-tier loss vocabulary |

## Sources

- `docs/RESEARCH.md` — project's authoritative strategy landscape, adaptation layers, validation theory, Bithumb integration facts, security model (product of eight rounds of independent audit; cites Yang 2025 *Finance Research Letters*, Grobys et al. 2025 *Financial Markets and Portfolio Management*, Cederburg et al. 2020, Seo/Koo/Yang 2024 *Economic Modelling* on the kimchi premium)
- `docs/EXECUTION.md` — project's authoritative milestone spine, three-gate Decision Register, E∧R acceptance logic, risk/capital-isolation model
- `.planning/PROJECT.md` — active/out-of-scope requirements for this milestone, confirming no live-trading features belong in v1

---
*Feature research for: single-asset Bithumb KRW spot backtest + validation system*
*Researched: 2026-09-07*
