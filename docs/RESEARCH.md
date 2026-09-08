# Bithumb Autotrading Bot — Research Brief

> **What this is.** The research half of the project: the strategy landscape,
> how to make the bot adapt, how to validate honestly, and the Bithumb/security
> facts that constrain the design. Build steps live in **`EXECUTION.md`**; the
> evidence audit is in **`RESEARCH_REVIEW.md`**. Eight rounds of independent audit
> drove the corrections marked throughout. **Not yet frozen — open decisions are
> tracked in the Decision Register in `EXECUTION.md`.**
>
> **Framing.** No strategy here "guarantees" profit. The correct prior is that
> **net alpha is zero or negative after all costs**, and the burden is on the
> data to reject that under a preregistered, realistic protocol whose final
> holdout is opened exactly once. A clarifying distinction: **execution realism
> does not create profit — it removes fictitious profit from the evaluation.
> Risk controls reduce loss severity and probability of ruin; they do not create
> positive alpha. Regime gating is itself an unvalidated module.** What follows
> is a set of hypotheses and safeguards, not a recipe for returns.
>
> **Read this first — the single most important correction.** The current
> `bot.py` computes its signal from a completed candle and then *fills at that
> same candle's close*. The signal itself is fine (it uses a closed candle); the
> impossibility is **assuming execution at an already-known closing price** —
> "same-candle execution bias." Every performance number the current backtest
> produces is therefore biased and unreliable (in an undetermined direction —
> the next tradable price may be better or worse), and **no indicator, regime
> layer, or ML model can fix a wrong fill model.** First build priority: a
> venue-aware conservative execution simulator (see §3 and `EXECUTION.md`
> M0–M2), not a better signal. **`bot.py` itself is still unchanged.**

---

## 0 — Where the current bot stands

The existing paper bot (`bot.py`) is a clean skeleton with defects that must be
fixed before its results mean anything:

- **Data:** Upbit public candles, polled. Signals use closed candles (good — no signal look-ahead), **but fills occur at the signal candle's close (bad — same-candle execution bias).** *(The current executable is an **Upbit** paper bot; this document describes a planned **Bithumb** rebuild.)*
- **Signal:** fast/slow EMA crossover (8/21) on one market (`KRW-BTC`).
- **Sizing:** fixed fraction of cash (`position_fraction = 0.30`), all-in/all-out, one position at a time. *(`position_fraction` ≠ `risk_per_trade` — see `EXECUTION.md` "Risk & capital isolation.")*
- **Exits:** fixed-% stop (3%) and take-profit (6%), plus a daily-loss halt (2%). **Evaluated only against the close**, so intrabar stop hits, gaps, and candles touching both stop and target are invisible.
- **Costs:** fee (0.05%) and slippage (0.05%) per side, both fixed constants.
- **Execution:** simulated only — no live orders, no keys.

**Known defects to fix before trusting any number** (detail in `RESEARCH_REVIEW.md`):
1. **Same-candle execution bias.** Signal from completed candle *t* must execute no earlier than the next tradable observation (≥ *t+1* open); better, replay Bithumb trades/order book conservatively. The resulting bias is not reliably optimistic — it can be either direction.
2. **Close-only stop/TP evaluation.** Needs full OHLC + a predeclared conservative intrabar rule (including an explicit gap rule — if the next available price has gapped past the stop, fill at that adverse price, not the trigger).
3. **Fee/slippage/impact handling.** Fees can be *queried*; spread and depth can be *observed*; **slippage and market impact must be *modeled* and then *calibrated* against real fills** — they are not simply "queried."
4. **Timeframe/indicator mismatch.** On 15-minute candles, "MA(200)" ≈ 50 h and "EMA 8/21" ≈ 2–5 h — not the daily/weekly horizons of the cited literature (§1.4).

---

## 1 — The strategy landscape (what your options are)

A useful organizing idea: **if an edge exists, its observed performance may be
regime-dependent.** This is weaker than it sounds — regimes are *latent*,
classification is error-prone, and a regime filter can **reduce** rather than
improve performance. The published crypto evidence for these edges is **mixed
and sample-dependent**, and — critically — **most of it studies multi-coin,
long/short, higher-timeframe portfolios that are a different strategy category
from a single-asset, long-only, intraday Bithumb spot bot.** Treat everything
below as **hypotheses to test on your own venue and timeframe**, not facts to
inherit.

### 1.1 The four market regimes (a framing device, not a law)

| Regime | Looks like | *May* suit | *May* struggle |
|---|---|---|---|
| **Trending / quiet** | Steady drift, low noise | Trend-following | Mean-reversion |
| **Trending / volatile** | Strong move, big candles | Trend-following, size/stop adjusted | Mean-reversion, tight stops |
| **Ranging / quiet** | Sideways, tight band | Mean-reversion (weak net of costs, single-asset) | Trend-following (whipsaw) |
| **Ranging / volatile** ("chop") | Sideways but wild | *Usually: don't trade* | Almost everything |

Your EMA-crossover bot is a trend-following system. The intuition that it profits
in trends and whipsaws in ranges is a hypothesis to verify, and the regime labels
themselves are noisy. In a volatile trend, one *plausible* response is a wider
stop and smaller size; whether that helps must be tested.

### 1.2 The strategy families

**A. Trend-following / momentum** *(what you have — a pragmatic hypothesis)*
- **Idea:** enter with an established move, exit on reversal.
- **Tools:** EMA/SMA crossovers, MACD, Donchian breakouts, ADX, or plain absolute momentum (price vs. long MA / sign of trailing return).
- **Evidence — read the category carefully (§1.5):** the well-cited crypto "momentum" results are **cross-sectional long/short** portfolios, a *different* strategy from a single-asset long-or-cash rule. **Single-asset time-series/absolute momentum has thinner directly-applicable evidence and should be presented as a pragmatic hypothesis**, not as inheriting those Sharpes/returns.

**B. Mean-reversion**
- **Single-asset tools:** Bollinger Bands, RSI extremes, z-score vs. a moving average. **Weak net of costs.**
- **The defensible form is cointegration pairs** — but pairs trading is inherently **long/short and market-neutral**, which a long-only Bithumb spot account cannot reproduce. The cited crypto pairs research uses long/short positions, limited periods, with survivorship exposure, and shows **transaction costs can erase conventional profits.** A separate research direction for a futures/long-short system, **not** validated alpha here. **Research-only, v2+.**

**C. Breakout** — Bollinger squeeze, Donchian breakout, ATR expansion, volume confirmation; captures the shift out of ranging/quiet.

**D. Market-making / grid** *(mention, not recommended)* — works in ranges, blows up in trends without inventory management; needs low latency. **Skip for v1.**

### 1.3 The strategy–regime fit matrix

```
                 QUIET vol            VOLATILE vol
   TRENDING   Trend-follow (full)   Trend-follow (smaller size, wider stop?)
   RANGING    Mean-reversion*       NO-TRADE (or tiny breakout probes)
```
\* A *hypothesis*; the robust (pairs) form isn't available long-only. v1 may run
**trend-or-cash only.** The regime filter adds parameters and researcher degrees
of freedom and can hurt performance, so it must clear the preregistered
acceptance gate within the selection windows (§3) — it is not assumed to help.

### 1.4 Timeframe first, indicators second

Identical indicator labels don't carry the same meaning across timeframes. On
15-minute candles: EMA 8 ≈ 2 h, EMA 21 ≈ 5.25 h, ATR/ADX 14 ≈ 3.5 h, MA 200 ≈
50 h. The cited momentum research operates at **daily-to-weekly** horizons, so a
15-minute EMA 8/21 system cannot claim its support. **Decide the horizon before
choosing indicator periods.**

**Candle intervals (corrected):** Bithumb/Upbit native minute intervals are
**1, 3, 5, 10, 15, 30, 60, and 240** — **no native 360-minute (6h) candle.** Use
the native **240-minute (4h)** interval as the initial higher-timeframe baseline.
A 6h candle **cannot be reconstructed from 240m candles** (6 is not a multiple of
4); if wanted, aggregate it from **60-minute candles or trade data**, with a
defined boundary, timezone, and missing/duplicate/incomplete-candle handling.
Also fix a **24/7 annualization convention** for 4h returns (crypto has no market
close) — an open decision in the Register. Whether a higher timeframe outperforms
15m **must be tested, not asserted.**

### 1.5 Honest state of the momentum evidence (category error corrected)

An earlier review claimed crypto momentum "has no momentum crashes" and called
the literature "lopsided." Both were withdrawn. **A further correction:** the two
2025 papers below both study **cross-sectional long/short** momentum, not
single-asset absolute momentum, and must not be filed under the latter.

- **Cross-sectional long/short — supportive:** Yang, A. (2025), "Cryptocurrency Market Risk-Managed Momentum Strategies," *Finance Research Letters* 85, 107879 — applies the Barroso–Santa-Clara risk-managed framework (reports robustness under short-sale constraints); risk management raises weekly returns ~3.18%→3.47%, Sharpe ~1.12→1.42.
- **Cross-sectional long/short — conflicting:** Grobys, Kolari, Sandretto, Shahzad & Äijö (2025), "Cryptocurrency momentum has (not) its moments," *Financial Markets and Portfolio Management* 39(4), 443–476 — a **top-30, weekly-rebalanced, zero-cost long/short winner-minus-loser portfolio**; momentum **insignificant** full-sample, with a **−255% weekly portfolio return** driven by the short leg. This is a long/short portfolio return, **not** a single coin's price move.

**Three distinct buckets, kept separate:**
1. *Cross-sectional long/short crypto momentum* — Yang and Grobys give **conflicting** evidence here; requires shorting (not available on Bithumb KRW spot).
2. *Single-asset time-series / absolute momentum* — needs its own directly-applicable evidence or stands as a **pragmatic hypothesis.**
3. *The proposed Bithumb BTC long-or-cash baseline* — **cannot inherit** the Sharpes, weekly returns, or crash properties of (1). It must earn its keep on your own data.

---

## 2 — How to make the bot adapt

After selecting the baseline within the preregistered selection process,
evaluate additional layers **one at a time**. Each is kept only if it clears the
acceptance gate on out-of-sample evidence **within the preregistered selection
windows** (never on the final holdout — §3). **Freeze the final strategy only
after all keep/drop decisions are complete** — then, and only then, open the
holdout once. **Each layer is an unvalidated module that can reduce
performance.**

### Layer 1 — Regime gating
Compute on each closed candle: **trend strength** (ADX; >25 / <20 — heuristics,
tuned in training folds only), **direction** (price vs. a long MA sized to the
timeframe), **volatility state** (ATR vs. its recent average). Gate: trend →
trend module; ranging & quiet → (research-only) MR; ranging & volatile → no new
entries. **Anti-whipsaw guardrails:** ≥2-candle confirmation, cooldown/min-hold,
max trades per day.

### Layer 2 — Adaptive sizing and exits
**ATR per-trade sizing — replace fixed `position_fraction`.** Size by a KRW risk
budget with consistent units (no mixing of fractions, currency, and per-coin
distances in one expression):
```
risk_fraction     = 0.0005 to 0.001                    # decimal fraction of total equity
risk_budget_krw   = total_equity_krw * risk_fraction
loss_per_coin_krw = entry_price
                    - conservative_stop_fill_price      # already reflects the gap/conservative fill
                    + entry_fee_per_coin
                    + exit_fee_per_coin
                    + impact_buffer_per_coin
quantity          = min( risk_budget_krw / loss_per_coin_krw,
                         sleeve_cap_krw / (entry_price + entry_fee_per_coin + entry_impact_per_coin) )
```
State explicitly whether fees are already inside `loss_per_coin_krw` (they are,
above) and whether the gap buffer is additive or folded into
`conservative_stop_fill_price` (folded in, above). Sizing and stop-distance are
*separate mechanisms* — vol targeting changes size, not stop width.

**Volatility targeting (portfolio-level overlay).** A *risk-control hypothesis*,
not a guaranteed Sharpe improver. Large-sample equity work (Cederburg, O'Doherty,
Wang & Yan 2020, 103 strategies) found volatility-managed portfolios did **not**
systematically outperform out-of-sample. In an unlevered long-only spot account
it may reduce realized volatility *and* return. Keep only if net performance
improves within the selection windows.

**Stops — competing hypotheses.** ATR-based and fixed-% stops are compared under
the same total risk budget, cost model, gap model, and sizing rule. ATR stops
adapt but can widen *after* volatility has risen and permit larger nominal losses
unless size is cut too. **Exit-guarantee reality (see §4):** Bithumb's general v2
endpoint has **no stop-market**; its legacy API offers an **exchange-side
stop-*limit*** which is still **not a guaranteed exit** (the limit can go unfilled
in a gap); a **client-side stop-market trigger** can fail to fire if the
process/network/API is down. None of the three guarantees a fill at the trigger.
Do **not** assume ATR > fixed-%, and do **not** write "always keep a hard stop."

**Trailing stop** (e.g. chandelier) — another exit hypothesis to test.

**Keep the daily-loss halt**; add a **max-drawdown kill-switch** — but see §5 on
why "cancel all orders on disconnect" is not a complete safety control.

**Kelly: removed for v1.** With a prior of zero-or-negative net alpha, even ¼–½
Kelly is unjustifiably aggressive and hard to estimate. Reconsider only after
robust, uncertainty-adjusted estimates of expected return and the loss
distribution exist.

### Layer 3 — Strategy selection / meta-adaptation (only if Layers 1–2 pay off)
- **Rule-based switching (a reasonable v1/v2 ceiling — but not "hard to overfit").** The regime gate *is* the selector. It is simpler than an HMM or neural model, but it still carries many degrees of freedom (ADX threshold, MA horizon, ATR window, confirmation period, cooldown, min-hold, max trades/day) and can be overfit. Simpler ≠ safe.
- **Statistical regime models (HMM):** more adaptive, more overfit-prone; only after the rule-based version is solidly profitable in walk-forward.
- **ML / RL — honest box:** deep-learning price prediction and LLM sentiment are **Tier-3**. Most reported results are *forecast-accuracy* metrics, not net-of-cost P&L, and are prone to leakage/survivorship bias. If used at all, apply ML to a **sub-problem** (regime classification, pair-spread modeling) under **purged & embargoed** CV *where label horizons overlap* — never as a black-box price oracle. RL for sizing/execution is a research direction only.

---

## 3 — Validation: the part that decides if any of this is real

Build the conservative simulator and **preregister the whole protocol before
evaluating any candidate** (see `EXECUTION.md` M3 and the Decision Register).

### 3.1 Execution realism (do this first)
- **Temporal separation:** signal candle ≠ execution event (minimum: next-candle open; better: conservative order-book replay).
- **Intrabar ambiguity + gap rule (market/marketable exits):** predeclare the worse-outcome assumption when a bar spans stop and target; fill gap-throughs at the next adverse price, not the trigger.
- **Stop-LIMIT needs its own state machine, not the market-exit rule.** If the legacy stop-limit is in scope, model `WATCHING → TRIGGERED/WAITING → PARTIAL/DONE/CANCEL`: trigger-and-limit crossed in one candle, price gapping *below* a sell limit (→ **no fill, continued exposure** — here the conservative outcome is *not* "fill at the next adverse price"), unfilled across candles, partial-then-decline, cancel-vs-fill races, a triggered order becoming an ordinary resting limit, automatic-order-specific fees, and whether later recovery fills the still-open limit. The conservative outcome for a stop-limit may be **no fill and retained exposure**, which is worse for risk than an assumed adverse fill.
- **Costs:** **fees are queried; spread and depth are observed; slippage and market impact are modeled and calibrated** against real fills. Do not stress by multiplying a possibly-zero promo fee — use non-promotional reference fees, absolute basis-point scenarios, and total round-trip cost scenarios.
- **Signal-independent execution calibration (conditioned on execution policy and size) — the observe-only part belongs in M2, before strategy evaluation** (see §3.5) so the holdout isn't compromised later; real-order measurement is M6B.
- **Data integrity:** Bithumb minute candles ≤200/req → paginate, dedupe, validate timezones, handle gaps; backtest on **Bithumb** data.

### 3.2 Baseline-first, then earn every addition (within selection windows)
Start with cash, buy-and-hold, and the preregistered baseline candidate(s). Add
any module only if it clears the acceptance gate on out-of-sample evidence
**within the preregistered selection windows.**

### 3.3 Preregistration + walk-forward + multiple-testing honesty
- **Preregister first** (metric, benchmarks, tests, numeric gates) — *before* running any candidate.
- **Nested walk-forward:** training → selection folds → roll; all selection inside training+selection windows.
- **One final holdout, opened exactly once, after freezing** — never used to fit, tune, or *select*. **The subject evaluated on it is the *frozen final strategy*, not a "baseline."**
- **Multiple-testing tool — choose deliberately, and match it to the objective.** The **Deflated Sharpe Ratio** is a *Sharpe-based* procedure: it depends on sample length, the observed Sharpe, skewness, kurtosis, the *variance of Sharpe estimates across trials*, and the *effective* number of independent trials — **not merely the raw candidate count**, and it returns a probability in [0,1] (reject unless DSR ≥ a preregistered threshold; **never "DSR ≤ 0"**). **If the primary metric is Calmar, DSR is a mismatch** — either make the primary selection statistic Sharpe-based, give DSR an explicit narrower supporting role, or use a resampling/multiple-testing procedure appropriate to the Calmar objective. **PBO** requires the CSCV return matrix of all candidates. **White's Reality Check** tests data-snooping.
- **Block bootstrap** on the **aligned per-period excess-return series** (not a single terminal cumulative return): define cash (exchange-held zero-yield KRW vs. an external opportunity-cost rate), block length/selection, confidence level, the test statistic, autocorrelation treatment, and the 24/7 4h annualization convention. All open decisions in the Register.
- **Purged/embargoed CV only where label/event horizons overlap.**
- **Robustness:** no dependence on one year, one coin, or a handful of trades; regime coverage across multiple rising/falling/sideways episodes with enough effectively-independent trades.

### 3.4 Survivorship & point-in-time
Any multi-coin work must include delisted assets, historical listings, and
point-in-time universe membership.

### 3.5 The live-vs-backtest gate (staged — paper fills aren't real fills; holdout-safe)
Execution calibration is **signal-independent but conditioned on execution policy
and size** — it does not depend on the entry signal, but execution cost *does*
depend on market/side, order type and urgency, order size, spread and volatility,
time of day, trade frequency/clustering, and maker/taker policy. So the
**observe-only** part (books/trades without orders → hypothetical marketable-order
cost vs. the sim's assumptions → conservative bounds) is done **in M2, before the
holdout is opened.** But **real fills are measured only in M6B, after the holdout**
(v1 uses Option B: no trade key before M6B). **Minimum-size real orders validate
plumbing, latency, state transitions, and basic fee accounting — they do NOT
validate production-size market impact or the partial-fill distribution of larger
orders, and must never be extrapolated to sleeve size.** **Critical:** if M6B
forces a material simulator/cost change, that **burns the holdout** — the frozen
artifact must be re-frozen and re-evaluated on **forward** data collected after the
refreeze (previously-inspected history cannot be relabeled as a new holdout; under
Option B, forward data is the only valid replacement). A paper order does not
reveal queue position, partial-fill behavior, cancel-vs-fill races, latency, or
market impact, so "paper fills within the sim distribution" is not by itself valid
execution validation.

---

## 4 — Bithumb integration facts (corrected)

> Confirm against current official docs (`apidocs.bithumb.com`) at implementation
> time. A real integration test is mandatory; don't trust this list or any
> third-party wrapper blindly.

- **Order types — two APIs, and a stop DOES exist (corrected):**
  - **General v2 endpoint (`/v2/orders`):** `limit`, `price` (market buy), `market` (market sell), `best`. **No stop-market here.**
  - **Legacy v1.2.0 automatic-order endpoint (`POST /trade/stop_limit`):** an **exchange-side stop-*limit*** with `watch_price` (trigger) and `price` (limit submitted on trigger), for buy and sell. Its request exposes only `order_currency`, `payment_currency`, `watch_price`, `price`, `units`, `type` — **no `client_order_id`** (see §5 for the separate idempotency protocol this forces). *(A 2020 Bithumb announcement stated a maximum of 25 "watching" orders and a 0.25% fee; these are **dated historical figures** — the current watching-order limit and the account-specific automatic-order fee must be verified at implementation time, and `/v1/orders/chance` reporting general fees does not prove the same rate applies to this legacy product.)*
  - **So "there is no native stop order" is false**; the accurate statement is **"no *guaranteed* stop-market exit."** A stop-*limit* can trigger and then go **unfilled** in a gap or illiquid market — it is not a guaranteed exit. A **client-side stop-market trigger** is a third option that can fail to fire during an outage.
  - **Open design decision (Register):** use v2 only, or combine v2 with the legacy automatic-order endpoint. If the latter, separately test the automatic order's lifecycle, auth, cancellation, reconciliation, and WebSocket behavior.
- **IOC / FOK / Post-Only are execution conditions, not loss protection.** IOC can leave a partial position; a filled FOK can lose money immediately; Post-Only may never fill or may fill just before an adverse move. None can invalidate a trade because it later became unprofitable.
- **Fees — query them; stress non-promotional total cost.** At runtime, query the available-order-information endpoint (e.g. `/v1/orders/chance`) for `bid_fee`, `ask_fee`, `maker_bid_fee`, `maker_ask_fee`, minimum order size, and supported order types, and **record the fees used for every experiment.** Do not assume a promotional rate persists or "stress" a possibly-zero fee by multiplication. *(Dated historical note: a fee-free event ending 18:00 KST 2026-09-06 applied to ordinary KRW-market API transactions while excluding certain separate products — auto-trading, TWAP, automatic orders, arbitrage, automatic repayment. Expired; recorded only as history.)*
- **Spot only, long-or-cash:** no shorting on KRW spot. Long/short or market-neutral strategies are **not reproducible** here.
- **APIs:** REST for orders/balances/candles; WebSocket for real-time data.
  - Public WS: `wss://ws-api.bithumb.com/websocket/v1`
  - **Private WS: `wss://ws-api.bithumb.com/websocket/v2/private`** *(v2).*
  - **Rate limits are per-channel, not one global cap.** Separate limits for public REST, private REST, batch requests, repeated same-asset requests, and WebSocket connections. The **~10/sec is the WebSocket connection limit**, not a universal request limit. Use **separate token buckets and backoff per channel.**
  - Handle ping/pong, reconnect backoff, duplicate/out-of-order events, REST reconciliation after reconnect.
- **Minute candles:** ≤200 per request → paginate, dedupe, validate timezones, handle gaps. Native intervals: 1/3/5/10/15/30/60/240 min (no native 360; 6h aggregates from 60m/trades, never from 240m).
- **On "maker orders have no slippage" (corrected):** misleading. Maker vs. taker fees may or may not differ for *your* account (query them). Maker orders may not fill; the market may move while resting; fills can occur precisely due to adverse flow (adverse selection). Post-Only converts explicit cost into **non-fill, opportunity, and adverse-selection risk** — a maker-only exit is especially unsafe in a fast decline.

---

## 5 — Security & operational safety

- **API keys — three classes, never one blanket "trade-only" key:** a *public* path (no key) for market data; an *account/read key* (JWT-authenticated, no trade, no withdrawal) for M1's private endpoints like `/v1/orders/chance`; and a *trade key* (trade-only, IP-restricted) **only from M6B**. **Withdrawal permission is kept disabled permanently on every bot key** — no key ever gets it. If the legacy stop-limit is in scope, note it uses a **separate `Api-Key`/`Api-Nonce`/`Api-Sign` signature scheme**, not JWT, so two auth adapters are needed. Store secrets in env/secrets, never in `config.json`/source; `.gitignore` them; separate credentials/processes where supported.
- **Exact-decimal money math** for all price/qty/PnL.
- **Application-level idempotency — two protocols, because the endpoints differ:**
  - *v2 orders* accept a `client_order_id`: assign a unique one, but do **not** assume the exchange deduplicates by it unless tested; reconcile before retry.
  - *Legacy `/trade/stop_limit` has **no `client_order_id`***, so it needs its own ambiguous-response protocol: persist an order *intent* before submission → send **once** → store the returned `order_id` → if the response is lost, **reconcile across all relevant states before any retry** — `watch`, `wait`, `done`/`cancel` history, executions/trades, **and balances + reserved balances** (a triggered order leaves `watch`, becomes `wait`, then `done`/`cancel`) → match conservatively on market/side/quantity/trigger/limit/submission-window, **enforcing at most one unresolved identical legacy intent per (market, side, parameter) tuple** so an accidental duplicate can't hide behind a legitimately identical order → if still ambiguous, **stop and require manual reconciliation** (never auto-resubmit when the first request may have succeeded).
  - In both cases a timeout **never** triggers an unconditional retry. **Do not run an intentional duplicate-`client_order_id` test in production** — the API already requires uniqueness, a "non-marketable" Post-Only can turn marketable in flight, and a cancel can fail; test ambiguous-timeout recovery, reconcile-before-retry, restart recovery, and lost-response handling against real non-duplicate orders instead (a true duplicate test only in a dedicated sandbox or with explicit Bithumb guidance). **Restarting must never create duplicate orders**; reconcile local state against live balances/open orders on startup.
- **The kill switch is not complete on its own.** "On connection loss, cancel all open orders" can fail *because* the connection is down. Assume a cancel can itself fail. Require an **independent watchdog / separate reconciliation process**, and test that a dead client-side stop monitor is detected.
- **Also test:** clock sync, JWT nonce behavior, per-channel rate-limit handling, exchange error classification.
- **Alerting:** notifications for fills, errors, kill-switch, daily P&L.
- **Start tiny and gated:** sleeve **1–5% of total equity**; **planned** risk **0.05–0.10% of total equity per trade** (if the sleeve is too small at the chosen stop, actual planned risk stays **below** target — never lever up or breach the sleeve cap); tiered halts; **manual approval after ~3–5% sleeve drawdown.** These cap the cost of discovering a defect; they don't make the strategy safe or profitable.
- **Planned loss is NOT maximum loss (loss prevention was the point of this project).** The 0.05–0.10% figure is the planned loss **if the modeled exit functions.** It is not a guaranteed maximum: a stop-limit can trigger and stay unfilled, a client-side stop can miss during an outage, a market order can slip far past the model, and orders can be rejected. Distinguish three: **`planned_stop_loss`** (if the exit works), **`max_market_loss`** (on the intended position; unlevered long-only ≈ the whole position), and **`max_operational_loss`** (under defective automation: **all assets the API key can trade**). Stress tests must include delayed exit, stop-limit non-fill, API outage, and near-total loss of the open position.
- **The sleeve is a real cap only if it is *technically isolated*.** A configuration value does not stop a bug, stale order, or bad reconciliation from trading the whole account, and disabling withdrawals does **not** stop the bot from spending all available KRW. So: use a **dedicated subaccount or separate trading account** where supported; otherwise keep **only sleeve-sized funds** in the API-accessible account; enforce **aggregate exposure** across open positions, outstanding buy orders, and reserved cash; and treat the **operational blast radius** as *all assets accessible to the API key*. The **isolated** 1–5% sleeve — not the stop-distance formula — is the real top-level capital-loss cap.

---

## 6 — The Kimchi premium (research hypothesis)

Korean venues persistently diverge from global prices (the "kimchi premium").
**Precision:** the premium's mean-reversion is **nonlinear/threshold-dependent** —
it may revert outside estimated bands while behaving closer to a random walk
within a central range (Seo, Koo & Yang 2024, "Nonlinear Dynamics of Kimchi
Premium," *Economic Modelling* 135, 106726). That does **not** establish a
profitable sentiment factor. Cross-border arbitrage is out of scope (capital
controls, transfer/timing risk), and no order condition makes two independent
exchanges' matching engines atomic. At most, **log the premium as a candidate
context feature** and treat any predictive value as an unproven hypothesis.

---

### One-paragraph summary
First fix the foundation: the current backtest has same-candle execution bias (it
fills at the signal candle's close) and checks stops only against the close, so
its numbers are biased and unreliable (in either direction) — build a **venue-aware
conservative** execution simulator (with an explicit gap rule) and do
signal-independent observe-only execution calibration **before** any new signal work and
before the holdout is opened, and pick a coherent timeframe using Bithumb's
**native 240m (4h)** candle (6h from 60m/trades, not 240m). Then **preregister the
whole protocol before evaluating any candidate**, test the frozen final strategy
against cash and buy-and-hold under a nested walk-forward with a multiple-testing
tool matched to the objective (DSR is Sharpe-based and returns a probability — not
"≤0"; if the metric is Calmar, use an appropriate procedure), and add modules only
if they clear the acceptance gate within selection windows — the **final holdout is
opened once, and any material post-holdout simulator change burns it.** Treat
momentum carefully: the well-cited results are **cross-sectional long/short**
(Yang and Grobys, conflicting), a different category from the **single-asset
long-or-cash** baseline you'd actually run, which inherits none of their numbers.
On Bithumb the general v2 endpoint has **no stop-market**, but the legacy API has
an **exchange-side stop-*limit*** (still not a guaranteed exit), and a client-side
stop is a third, also-not-guaranteed option; fees must be **queried and stressed
at non-promotional total cost**; the private WS is **v2**; rate limits are
**per-channel**; IOC/FOK/Post-Only are execution conditions. Keep risk tiny with
**explicit denominators, correct arithmetic, and unit-consistent sizing**, remove
Kelly for v1, keep withdrawal permission disabled permanently on every bot key, use application-level idempotency with
reconcile-before-retry and an independent watchdog, and assume even a cancel can
fail. Execution realism removes fictitious profit and risk controls reduce ruin —
neither creates alpha.

**Next:** `EXECUTION.md` for the ordered build plan and Decision Register;
`RESEARCH_REVIEW.md` for the evidence audit.
