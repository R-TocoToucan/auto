# Bithumb Autotrading Bot — Execution Plan

> **What this is.** The ordered, checkable build plan for Claude Code. It
> implements the design in the companion **`RESEARCH.md`** (read that first), as
> corrected by eight rounds of independent audit; the evidence discussion is in
> **`RESEARCH_REVIEW.md`**.
>
> **Not ready to freeze.** The **Decision Register** at the bottom is split into
> three gates by *when each decision must be made*. Pre-build decisions (Gate 1)
> must be resolved and frozen before M0 build work begins — including the **stop mechanism**, because M2
> builds and tests it.
>
> **Design commitment for v1:** execution calibration uses **Option B** (all real
> orders in M6B; M2 calibration is observe-only and provisional). Option A (a
> pre-research live-order stage) is **not** part of v1 and is recorded only as a
> future alternative in `RESEARCH_REVIEW.md`. So: **no trade-permission key
> before M6B**, unconditionally, across all documents.
>
> **Status honesty.** `bot.py` is unchanged: it executes at the signal candle's
> close and evaluates stops on closes only, so its backtest numbers are biased
> and unreliable (in an undetermined direction). This is a plan, not validated
> behavior.

---

## Guiding constraints (apply to every milestone)

- **Correct prior:** assume net alpha ≤ 0 after costs until a preregistered, realistic protocol rejects it on data never used for any selection decision.
- **No look-ahead in fills:** a signal from candle *t* executes no earlier than the next tradable observation (≥ *t+1* open).
- **Fees are queried; spread and depth are observed; slippage and market impact are modeled and calibrated** against real fills — not "queried."
- **Timeframe:** native **240-minute (4h)** baseline. No native 360-minute (6h); aggregate 6h from **60m or trades** (never 240m). Fix a 24/7 annualization convention (Gate 2).
- **Count your experiments:** log every candidate evaluated, feeding the multiple-testing correction.
- **Every exit mechanism can fail** (stop-limit non-fill, client-side trigger miss, market-order slip, rejection). **Planned loss ≠ maximum market loss ≠ maximum operational loss** (see "Risk & capital isolation").

---

## The validation spine (read before the milestones)

**(a) Decisions are gated by timing (see the three-gate Register).** Anything that
shapes how M1/M2 are built — venue, market, timeframe, **stop mechanism**,
data/L2 source, simulator fidelity, order policy — is a **Gate 1 pre-build
decision**, frozen before M0 build work begins. Research-protocol choices are **Gate 2** (M3).
Pre-live choices are **Gate 3** (before M6/M7).

**(b) Three disjoint data roles.** Training (fit) → Selection windows (choose
modules) → **Final holdout (opened once, after the *software artifact* is frozen;
evaluated by the frozen final strategy).**

**(c) Freeze the artifact, not just the concept, before the holdout.** The
holdout is opened only after the strategy **and** the software that runs it are
frozen and hashed: source commit/hash, config hash, simulator version, dataset
hash, cost model, stop mechanism, statistical protocol. Mock/read-only
scaffolding (M6A) is therefore built **before** the holdout, so shared-code
defects can't force post-holdout changes. Only **M6B** (real orders) remains
after the holdout, because real fills are the one thing that cannot be known
earlier — and a material M6B correction burns the holdout (spine (d)).

**(d) Holdout-burn + valid replacement.** A *material* post-freeze change to the
execution/cost model **burns the holdout.** Under Option B the only clean
replacement is **forward data collected after the revised model is refrozen** —
an earlier historical period is *not* valid (later-calibrated assumptions would
model earlier data = temporal leakage). Reserved historical contingency holdouts
are **not** used for this purpose under Option B.

**"Material" is defined, not discretionary.** The holdout is burned by a change to
**any** of: signal or parameters; position sizing; stop/exit behavior; order timing
or type; the fee, spread, slippage, or impact model; simulator fill logic; shared
execution/research code; data cleaning or exclusion rules; or the benchmark or
statistical test. **Non-material** is a narrow allowlist: documentation-only
changes, or logging that provably cannot alter any decision or fill. Every change
is recorded with before/after hashes; anything not on the non-material allowlist is
material by default.

---

## Milestones

### Gate 1 — resolve pre-build decisions (before M0 build work)
Freeze, in `config`/docs, before building: venue; target market(s); timeframe;
**stop mechanism** (v2-only vs. v2 + legacy stop-limit); calibration option (v1 =
**Option B**); data source and **L2 source**; simulator fidelity level; backtest
order policy. These drive M1/M2 and cannot be deferred to M3.

### M0 — Freeze scope, document, set up safety rails + key-class policy
- [ ] **Authenticated-key classes (decompose "read-only"):**
  - *Public* — no key (market data).
  - *Account/read key* — JWT-authenticated, **no trade, no withdrawal**; needed because `/v1/orders/chance` and balances are **private endpoints** (M1 is not purely public). Verify at build time which Bithumb activation permission `/orders/chance` requires; do not assume "read-only" grants it.
  - *Trade key* — **only from M6B**, trade-only, **no withdrawal**, IP-restricted.
  - No key ever has withdrawal permission.
- [ ] Document exact Bithumb constraints: v2 order types; the Gate-1 stop-mechanism decision; min order size (KRW); price/qty tick and step. Fees queried at runtime.
- [ ] Repo hygiene: secrets in env/secret store (`.gitignore`d), exact-decimal money type, structured logging.
- [ ] **Define risk denominators, loss terminology, and capital-isolation model** (see "Risk & capital isolation").
- [ ] **Test:** startup self-check prints resolved Gate-1 decisions + key class + risk denominators; refuses to run if any missing.

### M1 — Bithumb spec / fee / tick / min-order adapter (authenticated read, no trade)
- [ ] Using the *account/read key*, `BithumbSpec` queries available-order-information (e.g. `/v1/orders/chance`, JWT-authenticated) for `bid_fee`, `ask_fee`, `maker_bid_fee`, `maker_ask_fee`, min order size, supported order types — at startup and periodically. Record fees per experiment. **`/v1/orders/chance` reports general account/market fees and does not prove the same rate applies to the legacy automatic-order product** — verify that separately if the legacy stop-limit is in scope.
- [ ] Encode price-tick / qty-step rounding and minimum-order rules.
- [ ] **Test:** fees/constraints read from the API (authenticated); rounding helpers pass boundary unit tests.

### M2 — Data + venue-aware conservative simulator (implements the Gate-1 stop mechanism)
*(the core deliverable)*

Venue-aware and conservative, **not** "venue-faithful": aggregated public depth
means the simulator generally cannot know queue position, cancellations ahead of
it, hidden liquidity, same-timestamp ordering, or the impact of an unsubmitted
order. Fidelity levels:
- [ ] **Candle-only:** marketable (taker) orders vs. a spread/slippage model; conservative non-fill assumptions for resting orders.
- [ ] **L2:** approximate spread/depth from order-book snapshots with probabilistic/conservative queue modeling. **Required if the cost model makes slippage a function of depth.** **The cited official Bithumb public order-book endpoint provides current snapshots and does not provide a historical L2 archive** (this does *not* mean none exists anywhere — verified third-party historical Bithumb order-book datasets may be available). So the Gate-1 L2 source is one of: forward collection (REST/WS), a **verified third-party historical dataset**, or **candle-only historical backtest + a separate forward L2 validation period.** Record source, timestamp resolution, update sequencing, clock sync, min collection period.
- [ ] **Observe-only provisional calibration (pre-holdout, Option B):** observe live books/trades **without orders**; compare hypothetical marketable-order cost to sim assumptions; set conservative bounds. Provisional — real-order truth is M6B and may burn the holdout.
- [ ] `BithumbData`: native candles (1/3/5/10/15/30/60/240 min), REST pagination (≤200/req), dedupe, timezone validation, gap handling; immutable store with integrity checks. 6h aggregated from **60m or trades** (never 240m), unit-tested.
- [ ] **Temporal separation:** fill no earlier than *t+1*; same-close fill impossible by construction.
- [ ] **Market/marketable-exit intrabar + gap rule:** full OHLC; predeclare worse-outcome when a bar spans stop and target; gap-through fills at the next adverse price, not the trigger.
- [ ] **Stop-LIMIT state machine (built here because the mechanism was frozen in Gate 1):** model `watch → wait → done/cancel` (Bithumb's actual states), covering: trigger and limit both crossed in one candle; gapping **below** a protective sell limit (→ **no fill, retained exposure** — the conservative outcome is *not* "adverse fill"); unfilled across candles; partial-then-decline; cancel-vs-fill races; a triggered order becoming an ordinary resting limit; automatic-order-specific fees; whether later recovery fills the still-open limit. **Deterministic candle-only rule (preregistered, not left to implementation):** for a protective sell stop-limit, when the intrabar path cannot establish trigger-before-fill ordering, **assume no fill** unless lower-frequency trade data establishes the sequence.
- [ ] **Cost model:** fees from M1; slippage/impact modeled and calibrated; scenarios per the cost-scenario section.
- [ ] **Test:** known-answer replay; same-close impossible; gap-through market exit fills adverse; **stop-limit gap-below-limit → no fill + retained position**; ambiguous intrabar path → no fill; 6h aggregation matches hand-checked bars.

### M3 — Preregister the research protocol (Gate 2) — BEFORE any candidate
- [ ] Resolve **Gate 2** items (metrics, parameter ranges, folds, thresholds, statistical tests). Gate 1 is already frozen.
- [ ] **Primary decision metric** + tie-breaking rule.
- [ ] **Acceptance logic as an explicit Boolean table** with **both E and R fully defined** (see "Benchmark & acceptance logic" — R now carries its own uncertainty + materiality bounds).
- [ ] **Baseline candidate set + exact rule + parameter ranges** (one rule, or two candidates both counted in multiple testing).
- [ ] **Selection design:** nested walk-forward; training/selection/holdout lengths, step size, fold count/construction; signal timing; backtest order policy (matches M2 temporal separation).
- [ ] **Assign each statistical test to a stage:** PBO/CSCV — selection-stage diagnostic on the full candidate return matrix; DSR — selection-stage Sharpe correction *iff* the statistic is Sharpe-based (probability in [0,1]; reject unless DSR ≥ [P]; depends on sample length, observed Sharpe, skew/kurtosis, variance of Sharpe across trials, effective independent-trial count — not the raw candidate count; if the primary metric is **Calmar**, use a Sharpe-based selection statistic, a defined narrower DSR role, or a Calmar-appropriate resampling procedure); block bootstrap — selection **and** final-holdout CIs, separately declared roles, on the aligned per-period excess-return series; final holdout — **one** confirmation test on the frozen artifact.
- [ ] **Purged/embargoed CV only where label horizons overlap.**
- [ ] **Survivorship/point-in-time** for any multi-coin work.
- [ ] Write the **holdout-opened-once**, **artifact-freeze-and-hash**, **holdout-burn**, and **forward-data-replacement** rules.

### M4 — Implement and evaluate the preregistered baseline candidate(s)
- [ ] Implement the M3-fixed candidate(s) through the M2 simulator with M1 costs.
- [ ] Evaluate **within selection windows** against the Boolean table. Record metrics + candidate count.
- [ ] **Test:** results produced strictly under the frozen M3 protocol; nothing changed after seeing them.

### M5 — Add modules one at a time (within selection windows)
- [ ] **ATR per-trade sizing** (unit-safe formula), hard max-position cap.
- [ ] **ATR stop vs. fixed-% stop** — competing hypotheses under one risk budget/cost/gap/sizing rule.
- [ ] **Trailing stop** (e.g. chandelier).
- [ ] **Volatility-target overlay** — may reduce risk *and* return; keep only if net-positive within selection windows.
- [ ] **Regime gate** (ADX + long-MA + ATR-vs-average) with anti-whipsaw guardrails — an unvalidated module with many DOF that can reduce performance; must clear the gate.
- [ ] Declare an **interaction-testing / module-ordering policy** (Gate 2) so add-order doesn't become a covert search.
- [ ] **Max-drawdown kill-switch** + tiered daily-loss halt.
- [ ] **Test:** each module added independently per the declared policy; any failing the gate within selection windows is **dropped**, not retuned.

### M5-STRATEGY-FREEZE — freeze the strategy definition (not yet the full artifact)
- [ ] All keep/drop decisions complete → freeze the **strategy**: signal, parameters, sizing, stop/exit behavior, cost model, statistical protocol. Broker/integration code is **not** yet written, so this is a strategy freeze, not the final artifact freeze.

### M6A — Mock / read-only scaffolding (no trade key) — BEFORE the final freeze and holdout
- [ ] `BithumbBroker` against a **mock/paper broker**: place/cancel v2 `limit`, `price` (market buy), `market` (market sell), `best`; Post-Only where intended; partial fills/rejects in simulation.
- [ ] **Scope of M6A testing (no trade key, so NOT full integration):** JSON **schema** tests; **authentication construction** tests for **both** schemes the design uses — **JWT** for current v1/v2 private endpoints **and, if the legacy stop-limit was selected at Gate 1, the legacy `Api-Key`/`Api-Nonce`/`Api-Sign` scheme** (known-answer signature tests, nonce monotonicity + persistence-across-restart tests, clock behavior, encoding/delimiter tests, request construction, and static configuration checks); **mock** lifecycle tests; **recorded-response replay**; and public/read-only connectivity. **M6A can only verify how requests are *built*, not that a future trade key is *authorized*** — live credential-permission verification and exchange acceptance are **deferred to M6B**. Do not call M6A "integration testing" of submission/trigger/cancel/fill/private-stream.
- [ ] Implement (do not choose — Gate 1) the frozen stop mechanism's client code, its state handling, and idempotency/reconciliation/watchdog logic. **All of this is source code, so it must be written HERE, before the final freeze.**
- [ ] **Idempotency — two protocols:**
  - *v2 orders* accept `client_order_id`: unique per intended order; no assumed exchange dedup; reconcile before retry.
  - *Legacy `/trade/stop_limit` has no `client_order_id`*: persist an order *intent* → send **once** → store `order_id` → on lost response, **reconcile across all relevant states before any retry** (see the fuller list next) → never auto-resubmit when the first request may have succeeded.
  - **Legacy reconciliation must inspect all of:** `watch`, `wait`, `done` and `cancel` history, executions/trades, and **balances + reserved balances** (a triggered order leaves `watch`, becomes `wait`, then `done`/`cancel`). Matching on market/side/quantity/trigger/limit/time can collide with a legitimately identical order → **enforce at most one unresolved identical legacy intent per (market, side, parameter) tuple**; if still ambiguous, **stop and require manual reconciliation** (no auto-retry).
  - A timeout never triggers an unconditional retry.
- [ ] Durable transactional state; restart never duplicates orders; startup reconciliation.
- [ ] **Independent watchdog / separate reconciliation**; assume a cancel can fail; test that a dead stop monitor is detected.
- [ ] **Test:** reconnect recovers from duplicate/missing/out-of-order messages; forced restart creates no duplicate order; mock lifecycle + schema + token tests pass.

### M5-FINAL-FREEZE — freeze and hash the complete artifact (AFTER all pre-holdout code, i.e. after M6A)
- [ ] Now that all pre-holdout code exists (strategy + simulator + M6A broker/idempotency/reconciliation/watchdog), record the **final** hashes: source commit/hash, config hash, simulator version, dataset hash, cost model, stop mechanism, statistical protocol. **This is the artifact the holdout evaluates** — no source change after this point without burning the holdout.
- [ ] **Package boundary:** if any broker code is deliberately excluded from the hashed artifact on the grounds that it cannot affect the backtest, that boundary must be **defined and enforced** (e.g. the backtest imports nothing from the live-broker package), not assumed.

### GATE-3-FREEZE — resolve and record all pre-live risk limits BEFORE the holdout is opened
- [ ] Decide and record **all Gate 3 risk limits and escalation policies now, before the holdout result is known**: capital-isolation mechanism, tiered daily-loss halts, incremental size-ladder policy, M7 min duration + min real fills, live-escalation rules, re-validation cadence, M8 capital limit. These can all be declared in advance, so there is **no "where possible" discretion** — an impressive holdout result must not be able to influence how aggressively the live experiment is sized. *(The forward-data replacement plan after a holdout burn is NOT a Gate-3 item — it is part of the statistical protocol, frozen in **Gate 2**.)* Purely operational checks (e.g. confirming the subaccount is actually funded and isolated) may be physically executed immediately before M6B, but the **limits and policies themselves are frozen here.**

### ── final artifact frozen + hashed, Gate 3 limits recorded → open the final holdout ONCE ──
- [ ] Evaluate the **frozen final strategy on the final hashed artifact** on the holdout, exactly once, against the Boolean table.

### M6B — Extremely small live integration tests (trade-only key, withdrawal disabled) — AFTER the holdout
- [ ] Trade-only, withdrawal-disabled, IP-restricted key; separate process/credentials.
- [ ] **Real exchange integration** (the part M6A could not do): actual submission, trigger, cancellation, fill, private-stream, reconciliation, and the stop mechanism's real behavior — using **minimum-size** orders.
- [ ] **Live credential-permission verification for both auth paths** (JWT for v1/v2; legacy `Api-Sign` for the stop-limit if in scope): prove the trade key is actually authorized to submit each order type it will use. **Failure here blocks M7.** It does **not** burn the holdout *unless* resolving it changes the frozen execution mechanism, shared code, or modeled behavior (per the material-change definition).
- [ ] **No intentional duplicate-`client_order_id` test in production** (uniqueness is required; a "non-marketable" Post-Only can turn marketable in flight; cancels can fail). Test **ambiguous-timeout recovery, reconcile-before-retry, restart recovery, lost-response handling** against real non-duplicate orders. True duplicate tests only in a dedicated sandbox or with explicit Bithumb guidance.
- [ ] **Do not extrapolate minimum-order slippage to sleeve size.** Minimum orders validate plumbing, latency, state transitions, and basic fee accounting — **not** production-size market impact or the partial-fill distribution of larger orders. Any size increase is a **strictly capped incremental ladder** (Gate 3), separately justified.
- [ ] **Holdout discipline:** if M6B forces a **material** M2 simulator/cost change (per the material-change definition), that **burns the holdout** → re-freeze and re-run on **forward** data (spine (d)).
- [ ] **Test — separate the claims:** minimum-size orders validate **submission, authentication (both schemes), fees, latency, state transitions, cancellation, reconciliation, and observed slippage *at that size*.** **Partial-fill behavior is NOT validated by a minimum-size order** (it may simply produce no partial fill — absence of a partial fill is not evidence the model is right); it remains unvalidated until naturally observed or safely tested at a larger **preregistered** size. Passing M6B must **not** imply production-size partial-fill behavior is validated.

### M7 — Strictly limited pilot (Gate 3; only if everything above passed)
- [ ] Sleeve **1–5% of total equity**, **technically isolated** (see capital isolation); planned risk **0.05–0.10% of total equity per trade** (planned, not maximum).
- [ ] Tiered halts; manual approval after ~3–5% sleeve drawdown; predeclared **minimum duration** and **minimum number of real fills** before any judgement (Gate 3).
- [ ] Track first live fills against conservative bounds before any size increase.
- [ ] Scheduled re-validation at a preregistered cadence; post-holdout changes start a new research cycle with forward data.

### M8 — (only if ever justified) restricted automated operation (Gate 3)
- [ ] Only after a sustained M7 pilot matches expectations; capped capital; unchanged safety architecture.

---

## Benchmark & acceptance logic (explicit Boolean table; R now fully defined)

- **E (economic):** net excess return over cash after realistic costs, with the preregistered block-bootstrap CI on the aligned per-period (strategy − cash) excess-return series **excluding zero**.
- **R (risk-adjusted vs. buy-and-hold):** a preregistered improvement in the primary metric that also (i) meets a minimum economically material improvement threshold, and (ii) — for the drawdown-reduction clause — stays within an exact preregistered drawdown/return-sacrifice boundary. **The uncertainty test must be a *paired* comparison on the difference** `Δmetric = metric(strategy) − metric(buy_and_hold)`, via **paired resampling over aligned periods** with a preregistered interval/test on Δmetric — **not** two separate confidence intervals (which is not an equivalent comparison, and is especially wrong for path-dependent metrics like Calmar and max-drawdown). The exact paired-resampling procedure is frozen in Gate 2. A statistically or economically trivial improvement does **not** pass R.

**Both mandatory. Accept only on E AND R; fail either → reject:**

| E | R | Decision |
|---|---|---|
| Pass | Pass | **Accept** |
| Pass | Fail | **Reject** |
| Fail | Pass | **Reject** |
| Fail | Fail | **Reject / abandon** |

The "primary metric adjudicates" note applies **only within R's first clause**.

---

## Risk & capital isolation — planned loss ≠ market loss ≠ operational loss

Percentages are against **total account equity** unless stated. A position at 30%
of equity with a 3% stop has a *planned* loss of 3% of the position = **30% × 3%
= 0.9% of total equity** (not "of the position value").

**Three loss concepts:**
```
planned_stop_loss   # 0.05–0.10% of equity, IF the modeled exit works
max_market_loss     # on the intended open position; unlevered long-only ≈ whole position
max_operational_loss# under defective automation: ALL assets the API key can trade
```

**The sleeve is only a real cap if it is *technically isolated*.** A configuration
value does not stop a bug, stale order, or bad reconciliation from trading the
whole account; disabling withdrawals does **not** stop the bot from spending all
available KRW. Therefore:
- Use a **dedicated subaccount or separate trading account** where supported;
- otherwise keep **only sleeve-sized funds** in the API-accessible account;
- enforce **aggregate exposure** across open positions **and** outstanding buy orders **and** reserved cash;
- treat the **operational blast radius** as *all assets accessible to the API key*, and size that, not just the intended position.

**Unit-safe sizing (fees on both sides; sleeve cap includes entry cost):**
```
risk_fraction     = 0.0005 to 0.001                 # decimal fraction of total equity
risk_budget_krw   = total_equity_krw * risk_fraction
loss_per_coin_krw = entry_price
                    - conservative_stop_fill_price   # includes gap/conservative fill
                    + entry_fee_per_coin
                    + exit_fee_per_coin
                    + impact_buffer_per_coin
quantity          = min( risk_budget_krw / loss_per_coin_krw,
                         sleeve_cap_krw / (entry_price + entry_fee_per_coin + entry_impact_per_coin) )
# aggregate exposure (positions + reserved cash + outstanding buys) must also stay within the sleeve
```

| Control | Value | Denominator |
|---|---|---|
| Planned risk per trade | 0.05%–0.10% | total account equity |
| Sleeve = capital-at-risk cap (**technically isolated**) | 1%–5% | total account equity |
| Manual-approval halt | ~3%–5% drawdown | bot sleeve equity |
| Max-drawdown kill | ~10%–15% from peak | bot sleeve equity |

`position_fraction` (size) ≠ `risk_per_trade` (planned loss-at-stop).

---

## Cost scenarios — do not multiply a promotional zero
Separate scenarios: (1) the actually queried fee (recorded per experiment); (2) a
**non-promotional reference fee** (Bithumb standard KRW rate); (3) explicit
**absolute basis-point** scenarios; (4) **total round-trip cost** (fee + spread +
slippage + market impact). Verify the legacy automatic-order fee separately if in
scope.

---

## Decision Register — three gates (resolve each gate before its build stage)

Named consistently across all four documents as the **Decision Register**.

**Gate 1 — pre-build (before M0):** venue; target market(s); timeframe;
**stop mechanism — the *complete exit*, not just an API surface:**
- *If v2-only:* which protective exit — client-side trigger → market sell, client-side trigger → limit/best, candle-close signal exit, or no protective stop; plus trigger-price source, WebSocket-vs-polling, stale-price/disconnect rules, fallback order type, timeout/retry behavior, partial-fill handling, and independent-watchdog responsibility.
- *If v2 + legacy stop-limit:* trigger-to-limit offset, partial-fill residual policy, cancellation/replacement rules, and whether a client-side market fallback exists.

calibration option
(v1 = Option B); data source; **L2 source** + timestamp resolution + update
sequencing + clock sync + min collection period; simulator fidelity level;
backtest order policy.

**Gate 2 — research protocol (M3):** primary metric; tie-breaking; exact baseline
rule + parameter ranges; training/selection/holdout lengths; walk-forward step
size; fold count + construction; signal timing; **E and R thresholds** (R's
uncertainty test, materiality floor, drawdown/return-sacrifice boundary);
module-acceptance `[X]` + max-drawdown-worsening cap `[Y]`; DSR threshold `[P]` +
objective match; PBO threshold; bootstrap block length + selection + confidence
level + statistic; per-stage test assignment; number of distinct regimes +
definitions; min effectively-independent trade count; module testing order +
interaction-testing policy; data-quality exclusion rules; non-promotional
reference fee; absolute bp cost scenarios; conservative execution bounds (numeric);
max acceptable decision + order latency; **forward-data replacement plan after a
holdout burn** (part of the statistical protocol, frozen here in Gate 2 before
the holdout is opened); **the paired-resampling procedure for R's Δmetric.**

**Gate 3 — pre-live risk limits (frozen before the holdout is opened, applied before M6B/M7):** capital-isolation mechanism (subaccount vs.
segregated funds); tiered daily-loss halt levels; incremental size-ladder policy;
M7 min duration + min real fills; live-escalation rules; re-validation cadence;
M8 capital limit.

---

## Build order at a glance

```
Gate 1 (pre-build: venue, market, timeframe, STOP MECHANISM, Option B, L2 source, order policy)
   │
   ▼
M0 (key classes) ─► M1 (authenticated read) ─► M2 (sim + Gate-1 stop-limit state machine + observe-only calib)
   │
   ▼
M3 PREREGISTER research protocol (Gate 2)  ─►  M4 baseline  ─►  M5 modules
   │
   ▼
M5 strategy freeze  ─►  M6A (mock/read-only scaffolding + broker/idempotency/watchdog code; dual-auth tests)
   │
   ▼
M5-FINAL-FREEZE (hash the COMPLETE artifact, after M6A code)
   │
   ▼
GATE-3-FREEZE (all risk limits + size ladder recorded — before the holdout result is known)
   │
   ▼
── open the final holdout ONCE (final hashed artifact) ──
   │
   ▼
M6B real-exchange integration (min size; no dup-ID test; no size extrapolation)   [material change ⇒ BURN holdout ⇒ forward data]
   │
   ▼
M7 limited pilot (Gate 3; isolated sleeve)  ─►  M8 (maybe)
```
