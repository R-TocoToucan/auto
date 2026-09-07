# Pitfalls Research

**Domain:** Single-asset (KRW-BTC), long-or-cash Bithumb spot autotrading bot — Python, backtest/validation-first (no live trading this milestone)
**Researched:** 2026-09-07
**Confidence:** MEDIUM overall (HIGH where cross-checked against `docs/RESEARCH.md`/`docs/EXECUTION.md`'s eight audit rounds; LOW on any specific current Bithumb numeric fact below — web sources are uncurated and must be reverified against `apidocs.bithumb.com` at implementation time, exactly as `RESEARCH.md` already instructs)

> **Relationship to `docs/RESEARCH.md` and `docs/EXECUTION.md`.** Those documents are authoritative and already catalog same-candle execution bias, close-only stops, DSR/Calmar mismatch, holdout-burn, Bithumb order-type/rate-limit/idempotency facts, and capital-isolation traps in detail. This file does not re-litigate those — it (a) adds implementation-level depth on *how* each mistake actually gets made in code, (b) flags one **direct factual conflict** found during current-fact verification, and (c) surfaces pitfalls the docs under-emphasize: WebSocket sequence-gap desync, PBO/CSCV blind spots, block-bootstrap block-length sensitivity, JWT clock-skew, and the distinction between *statistical* overfitting risk and *implementation* risk.

---

## Critical Pitfalls

### Pitfall 1: Same-candle execution bias re-enters through a "helper" function

**What goes wrong:**
The project's own founding defect (`bot.py` fills at the signal candle's close) gets *reintroduced* in the rebuild — not through the top-level backtest loop (which everyone will scrutinize) but through a shared helper: a `get_current_price()` utility that a later module (regime gate, ATR sizing, kill-switch) calls and that silently returns `candle[t].close` instead of the executed fill price, or a vectorized pandas backtest that computes `signal.shift(1) * returns` but mis-shifts a *second* signal (e.g., the regime filter) by zero periods.

**Why it happens:**
Vectorized backtests are especially prone to this: `df['position'] = df['signal']` (no shift) is a one-character bug that produces smooth, plausible-looking equity curves. It's also reintroduced whenever a new module (Layer 1/2 in `RESEARCH.md` §2) is bolted onto an already-correct core loop but computes its own trigger condition inline instead of routing through the single, tested execution-timing primitive.

**How to avoid:**
Enforce a single chokepoint: every module (signal, regime gate, stop, sizing) may only ever read `t`-indexed *closed-candle* features and may only ever request execution at `t+1` or later, through one shared `ExecutionSimulator.submit(intent, decided_at=t)` call — never by directly reading `df.loc[t, 'close']` as a proxy for a fill. Add a static/lint check (or a runtime assertion) that no module outside the simulator ever accesses a candle's `close`/`high`/`low` for the *same* index it used to generate a signal.

**Warning signs:** Backtest equity curve looks unrealistically smooth or the "no signal look-ahead but same-candle fill" bug from `bot.py` recurs in miniature — e.g., a new module's isolated OOS return is implausibly good on the *first* evaluation, before any tuning.

**Phase to address:** M2 (build the chokepoint) and M5 (regression-test every new module against it before it's allowed to trade).

---

### Pitfall 2: Multiple-testing tool mismatched to the objective, or misread as a hard threshold

**What goes wrong:**
Two related but distinct errors, both already flagged in `RESEARCH.md` §3.3 as things to *avoid* — worth restating as concrete implementation bugs because they are exactly the kind of thing a generic stats library invites: (1) computing DSR (which is Sharpe-based) as the acceptance gate while the project's primary metric is Calmar, silently smuggling a Sharpe-shaped assumption (near-normal returns, no path dependence) into a path-dependent metric's evaluation; (2) coding the accept/reject check as `if dsr <= 0: reject` — DSR is a probability in **[0, 1]**, not a signed statistic, so `dsr <= 0` is almost always false and the gate never fires, silently accepting everything.

**Why it happens:**
Most public DSR implementations (blog posts, gists, the original Bailey & López de Prado formula) return a probability, but engineers pattern-match it against the far more familiar "t-stat > 0" or "Sharpe > 0" convention from other parts of the codebase and copy that comparison operator without re-deriving what the number means. The Calmar/Sharpe mismatch happens because DSR is the only "off-the-shelf" multiple-testing correction most people have heard of, so it gets reached for by default regardless of the primary metric.

**How to avoid:**
Write the acceptance check as `if dsr < P_threshold: reject` where `P_threshold` is the Gate-2-preregistered probability (e.g., 0.95), and unit-test it with a synthetic series where the expected verdict is known. If Calmar is the primary metric, either (a) demote DSR to an explicitly-labeled *secondary* Sharpe-based diagnostic and select/reject on a resampling procedure built for Calmar (e.g., paired block-bootstrap CI on the Calmar difference — see Pitfall 4), or (b) make the primary selection statistic Sharpe-based and report Calmar only descriptively. Document which of these two the project chose, in the same Gate-2 artifact that freezes `[P]`.

**Warning signs:** A code review question "does this DSR check ever evaluate to reject?" that nobody can answer without running it; a preregistration doc that names Calmar as primary but never explains why a Sharpe-based gate is being used to accept/reject it.

**Phase to address:** M3 (Gate 2 — write and unit-test the acceptance-table code before any candidate is evaluated). Verification: run the acceptance function against 2–3 synthetic pass/fail fixtures and confirm the expected Boolean.

---

### Pitfall 3: Two separate confidence intervals used instead of one paired-resampling test

**What goes wrong:**
`EXECUTION.md`'s Benchmark & acceptance logic already specifies the *correct* design (paired resampling on `Δmetric = metric(strategy) − metric(buy_and_hold)`), but the easiest thing to actually *code* — because most bootstrap libraries expose a single-series `bootstrap_ci(series)` function — is to compute `CI(strategy_metric)` and `CI(buy_and_hold_metric)` independently and eyeball whether they overlap. This is a materially different (and usually much more lenient) test: overlapping marginal CIs do not imply the *paired difference* is statistically indistinguishable from zero, especially when strategy and buy-and-hold returns are correlated period-by-period (they are, since the strategy is long-or-cash on the same asset).

**Why it happens:** Off-the-shelf bootstrap utilities resample one series at a time; building the *paired* version requires resampling the same block indices for both series simultaneously (so the pairing/correlation structure survives resampling) and only then taking the per-resample difference — an extra step nobody adds unless they've been told to.

**How to avoid:** Implement the paired-resampling test as a single function that takes the *aligned* (strategy, buy-and-hold) per-period excess-return arrays, draws one shared set of block-start indices per bootstrap iteration, computes `metric(strategy_sample) - metric(buy_and_hold_sample)` per iteration, and returns the CI/percentile of that *difference* series. Never expose or call a single-series `bootstrap_ci()` in the R-clause code path.

**Warning signs:** Any function signature in the codebase named `bootstrap_ci(series)` (singular) being called twice and the results compared for overlap.

**Phase to address:** M3 (Gate 2, write the paired-resampling procedure) — unit test with a synthetic case where the two series are perfectly correlated (paired test should show near-zero difference variance) vs. uncorrelated (marginal-CI-overlap method would incorrectly still show "no difference").

---

### Pitfall 4: Block-bootstrap block length picked once, never sensitivity-tested

**What goes wrong (under-emphasized in `RESEARCH.md`):** Both the E-clause and R-clause CIs depend on a block-bootstrap over autocorrelated 4h excess-return series. Block length has no universally correct value: too short and the resample fails to preserve volatility clustering/autocorrelation (CIs come out too tight, making marginal edges look "significant"); too long and there are too few effectively-independent blocks left to resample from (CIs become erratic and unstable, and can flip a pass/fail verdict on a re-run with a different random seed). Freezing one block length in Gate 2 without a documented sensitivity check risks freezing a length that happens to produce a favorable-looking CI.

**Why it happens:** Papers and library docs give heuristic rules of thumb (e.g., block length as a function of sample length or a simple ACF-based rule) but rarely spell out that the choice materially moves the answer; a single number gets pasted into config and nobody revisits it.

**How to avoid:** Before freezing `[block length]` in Gate 2, run the same E/R computation at 2–3 candidate block lengths (e.g., short/medium/long relative to the strategy's typical holding period) and confirm the accept/reject verdict is stable across them. Additionally check that mean lag-1 autocorrelation of the bootstrap resamples is close to that of the original aligned series — a large gap means the chosen block length is too short. Record the sensitivity check itself as a Gate-2 artifact, not just the final chosen number.

**Warning signs:** A Gate-2 doc with a single block-length number and no accompanying sensitivity table; an accept/reject verdict that changes between runs with a different bootstrap random seed.

**Phase to address:** M3 (Gate 2). Verification: sensitivity table committed alongside the frozen protocol.

---

### Pitfall 5: PBO/CSCV (or DSR) treated as validating the whole pipeline, not just rank-overfitting

**What goes wrong (under-emphasized):** PBO/CSCV is a powerful, principled diagnostic for *one specific* failure mode — that the best in-sample configuration is unlikely to still be the best out-of-sample. Teams that run it correctly sometimes then treat a "low PBO" result as a general seal of approval, when PBO says nothing about look-ahead bias, data leakage, invalid/leaky features, or generalization to regimes absent from the sample (e.g., a PBO computed on data covering only 2023–2026's mostly-trending BTC price action gives no information about performance in a prolonged chop regime never observed). Since v1 explicitly has a "trending/volatile chop" regime the strategy might fail in (`RESEARCH.md` §1.1/§1.3), a PBO pass must not be read as "chop-safe."

**Why it happens:** PBO/CSCV produces a single clean number (a probability), which invites treating it as a final verdict rather than one diagnostic among several that each cover a different failure mode.

**How to avoid:** Document explicitly, next to wherever PBO is reported, what it does and does not certify: it certifies "the winning configuration's rank is not a fluke of this particular in/out split," and nothing else. Keep look-ahead/leakage checks (code review + unit tests on the simulator, per Pitfall 1) and regime-coverage checks (`RESEARCH.md` §3.3 "no dependence on ... a handful of trades," multiple rising/falling/sideways episodes) as *separate*, independently-required gates — never let a good PBO substitute for either.

**Warning signs:** A milestone summary that cites PBO as the sole evidence a module "isn't overfit," with no separate mention of leakage review or regime coverage.

**Phase to address:** M3 (assign PBO its narrow diagnostic role explicitly in the Gate-2 protocol doc) and M5 (regime-coverage check run independently per module).

---

### Pitfall 6: Holdout leakage via debugging, logging, or shared-code changes made "just to look"

**What goes wrong:** The single most expensive mistake this validation design is trying to prevent is exactly the one that's easiest to make by accident *after* the holdout is opened once: a developer notices a bug or wants to add a log line and edits shared execution/cost code post-holdout without recognizing that `EXECUTION.md`'s material-change definition classifies almost everything as material by default (only documentation-only or provably-decision-inert logging is exempt). A second, subtler version: someone runs the M2 simulator or a debugging notebook *against the holdout date range* before the holdout is formally "opened," to "sanity check the data pipeline" — this is holdout leakage even if no metric is computed, because it lets implementation choices be (even subconsciously) informed by what that period's price action looks like.

**Why it happens:** The holdout period is just more rows in the same dataset; nothing prevents a `df.tail(500)` or a Jupyter cell from touching it while debugging an unrelated pipeline issue, because most people mentally model "leakage" as "used in a fit call," not "was ever rendered on a screen."

**How to avoid:** Technically partition the holdout data out of the working dataset entirely until Gate-3-freeze is complete — store it in a separate file/table that pipeline code does not have a path to until an explicit "open holdout" step, rather than relying on developer discipline to slice a shared DataFrame correctly every time. Log every access to the holdout file (even reads) from the moment it's separated, so any accidental pre-open touch is auditable. Treat "I looked at the raw candles to debug pagination" during M2 as requiring documentation of exactly which date ranges were viewed, and cross-check that range never overlaps the frozen holdout window.

**Warning signs:** Holdout date boundaries defined in code as a slice/filter on a dataset the debugger/notebook always has full access to, rather than a physically separate artifact; no access log for the holdout file.

**Phase to address:** M2 (physically separate the holdout data as soon as ranges are known) through GATE-3-FREEZE (audit that the holdout file's access log shows zero touches before the formal open).

---

### Pitfall 7: Look-ahead bias smuggled in through indicator warm-up, normalization, or resampling — not just fills

**What goes wrong:** `RESEARCH.md` covers same-candle *execution* look-ahead thoroughly, but a second, easier-to-miss class of look-ahead lives in feature computation: (a) computing ATR/ADX/long-MA "at time t" using a rolling window function that, due to an off-by-one in the window boundary, includes candle `t` when the design intends "as of the close of `t−1`"; (b) normalizing or scaling a feature (e.g., z-scoring an indicator, or picking ADX threshold bins) using statistics computed over the *entire* dataset including selection/holdout ranges, rather than only the training fold available at that point in walk-forward; (c) an aggregated 6h-from-60m candle whose final (rightmost) sub-bar hasn't closed yet being treated as a complete candle during live/near-real-time evaluation, effectively looking into a partially-formed future bar.

**Why it happens:** Pandas rolling/resample APIs default to *inclusive* right-edge windows unless told otherwise, and it's easy to write `df['atr'] = true_range.rolling(14).mean()` and use `df['atr'][t]` to size a trade decided at `t` without checking whether the window's last input row is `t` itself (same-bar) versus `t-1` (prior, correct). Global normalization is an easy default because it's simpler to write than fold-aware normalization.

**How to avoid:** For every feature, write down explicitly which candle's close each value "as of" — and unit-test with a synthetic series that the value at index `t` is unchanged by mutating candle `t`'s data (it should only depend on `<= t-1`, or on `t`'s *closed* OHLC if the feature is legitimately "computed from the just-closed candle t and used to decide at t+1"). Any indicator normalization/threshold calibration must be refit per walk-forward training fold, never fit once globally. For 6h-from-60m aggregation, only emit an aggregated bar once all six constituent 60m candles have closed timestamps strictly in the past relative to decision time.

**Warning signs:** A feature function with no explicit docstring stating its "as-of" candle; a single global `.fit()`/`.mean()`/`.std()` call over the whole feature DataFrame before any train/selection/holdout split; an aggregation function that emits a partial final bar during live polling.

**Phase to address:** M2 (data pipeline — write the "as-of" unit test harness once, reuse for every future feature) and M5 (apply it to each new module's features as they're added).

---

### Pitfall 8: Decimal used inconsistently, reintroducing float error through the back door

**What goes wrong:** The project correctly mandates exact-decimal money math, but `Decimal` is easy to use *incorrectly* in ways that silently reintroduce the exact bug it exists to prevent: constructing `Decimal(0.003)` directly from a float literal (this captures the float's binary imprecision, e.g. `Decimal(0.1) == Decimal('0.1000000000000000055511151231257827021181583404541015625')`) instead of `Decimal('0.003')` or `Decimal(str(x))`; parsing a Bithumb JSON response with a generic JSON library that decodes numeric fields as Python `float` before the code converts to `Decimal` (the precision loss already happened at the `json.loads` step, before `Decimal()` is ever called); mixing a bare `float` into an arithmetic expression with `Decimal` operands (raises `TypeError` in some combinations, silently coerces in others depending on operator order); and relying on the ambient/default `Decimal` rounding context (`ROUND_HALF_EVEN`) rather than an explicit, documented rounding mode per computation (fills should round conservatively, e.g. `ROUND_DOWN`/`ROUND_CEILING` chosen per the direction that avoids overstating an edge — the default context is process-global and any imported dependency can mutate it).

**Why it happens:** Every one of these compiles and often "just works" in a quick manual test with round numbers, so the bug only surfaces later on odd tick-size/quantity combinations — exactly the numbers real Bithumb responses will contain.

**How to avoid:** Parse all Bithumb REST/WS JSON with a decoder that keeps numeric price/qty/fee fields as strings (`parse_float=Decimal`-style hook, or manual string extraction) so `Decimal(str_value)` is always constructed from the original string, never from a float. Ban `Decimal(<float literal>)` via a lint rule or `flake8`/custom AST check. Set and document an explicit `decimal.localcontext(rounding=...)` around every fill/PnL calculation rather than depending on the global default. Add property-based tests (e.g., `hypothesis`) asserting a round-trip `str -> Decimal -> str` is lossless for representative Bithumb tick sizes.

**Warning signs:** Any `Decimal(` call in a code-review diff whose argument isn't already a string or another `Decimal`; a JSON-parsing utility with no explicit `parse_float` override; PnL calculations that don't specify a rounding mode.

**Phase to address:** M0 (repo hygiene: establish the exact-decimal type + lint rule before any code touches prices) — verify at M1 (spec/fee adapter, first real JSON parsing) and re-verify at M2 (cost model, PnL).

---

### Pitfall 9: Pagination, dedup, and timezone bugs in candle data that only show up at boundaries

**What goes wrong (depth beyond `RESEARCH.md`'s "paginate, dedupe, validate timezones, handle gaps"):** Bithumb's ≤200-candles-per-request minute-candle pagination is exactly the kind of interface where off-by-one bugs at page boundaries hide for a long time: (a) fetching page N and page N+1 by timestamp cursor and either double-counting or dropping the boundary candle depending on whether the cursor is inclusive or exclusive on the API side — must be verified empirically, not assumed from the docs; (b) two consecutive pages returning an overlapping candle with a **different** value (should never happen on a static already-closed candle, but if it does, that's a signal the API mutates recently-closed candles briefly after close — must be detected, not silently overwritten); (c) storing candle timestamps without an explicit, tested timezone (KST vs UTC) — Bithumb timestamps and candle boundaries are natively KST-aligned; a naive `datetime` (timezone-unaware) stored inconsistently between the historical bulk load and the live polling path will silently misalign 4h/6h bar boundaries between backtest and live even though each individually "looks right"; (d) a genuine exchange-side data gap (an exchange outage, not just a pagination bug) being silently forward-filled by a naive resample/reindex call, manufacturing a candle that implies no price movement when the truth is "unknown."

**Why it happens:** Pagination and pagination-boundary logic are usually tested only with hand-picked, well-behaved date ranges during development, and the exact inclusive/exclusive cursor semantics are rarely stated precisely enough in exchange docs to get right on the first try without empirical probing.

**How to avoid:** Write the candle store to be idempotent-append (upsert-by-timestamp with an integrity check that a re-fetched already-stored candle is byte-identical, raising/logging loudly if not); always store and process candle timestamps as explicit timezone-aware (`Asia/Seoul` or UTC, but one canonical choice, converted at the boundary) values, never naive; treat any true multi-candle gap (missing timestamps, not just a slow API) as data requiring an explicit, documented exclusion or interpolation *policy* (frozen in Gate 2's "data-quality exclusion rules"), never a default `ffill`.

**Warning signs:** A candle store with no uniqueness constraint on timestamp; any `fillna`/`ffill` call in the data pipeline without an adjacent comment justifying it as the frozen policy; backtest results that differ depending on which day pagination happened to start on.

**Phase to address:** M2 (`BithumbData` build) — unit test: known-answer replay across an artificially-paginated boundary must reproduce a single continuous, non-duplicated series identical to a non-paginated reference fetch.

---

### Pitfall 10: 6h aggregation correctness is necessary but not sufficient — boundary alignment must also match between backtest and live

**What goes wrong (depth beyond the "never aggregate 6h from 240m" rule already in the docs):** Even after correctly aggregating 6h bars from 60m candles or trades, a second failure mode remains: the aggregation's bin boundaries (e.g., which hour a 6h bar starts on — 00:00/06:00/12:00/18:00 KST vs. some other offset) must be identical between the historical backtest pipeline and any future live aggregation path. A generic `resample('6h')` call without an explicit `origin`/`offset` argument can silently bin differently depending on the first timestamp in whatever slice of data happens to be in memory — meaning a live-polling aggregator restarted mid-day could produce 6h bars offset from the ones the backtest was validated on, even though both individually "look correct" in isolation.

**Why it happens:** `resample()`'s default origin is derived from the data's own start timestamp unless overridden, so it silently varies run-to-run and pipeline-to-pipeline.

**How to avoid:** Freeze an explicit, documented bin origin (e.g., 00:00 KST) as a Gate-1/Gate-2 decision alongside the aggregation source choice, and pass it explicitly to every aggregation call (`origin=` or equivalent) rather than relying on the default. Unit-test that aggregating the same 60m data starting from three different arbitrary offsets produces byte-identical 6h bars.

**Warning signs:** A `resample()`/aggregation call with no explicit origin/offset parameter; 6h bars whose start times differ between two runs of the same aggregation code over the same underlying data.

**Phase to address:** M2, same test suite as Pitfall 9.

---

### Pitfall 11: Bithumb private WebSocket version — a direct factual conflict found during verification

**What goes wrong:** `docs/RESEARCH.md` states the private WebSocket path is `wss://ws-api.bithumb.com/websocket/v2/private` (v2). Current-fact web verification for this research pass turned up secondary sources (third-party gists, aggregator sites) describing the path as `wss://ws-api.bithumb.com/websocket/v1/private` (v1) with otherwise-consistent JWT-bearer authentication behavior (Authorization header built from access key + nonce + timestamp). **Neither source is being treated as ground truth here** — the web sources found are LOW-confidence, uncurated, and may themselves be stale or wrong, but the disagreement itself is real and must not be silently resolved by assumption in either direction.

**Why it happens:** Exchange API surfaces get versioned incrementally and inconsistently across REST vs. WS vs. legacy endpoints; third-party wrappers and blog posts lag official doc updates (or the official docs themselves may be mid-migration), so "v1 vs v2" claims from different points in time coexist online without dates attached.

**How to avoid:** Before M1 (which needs authenticated private REST) and definitely before M6A (dual-auth scaffolding) or any private-WS integration, pull the exact current path directly from `apidocs.bithumb.com` (not a third-party mirror) and record the verification date + the exact string in the M0/M1 build notes. Do not let this single unresolved fact block Gate-1 freezing of *other* decisions, but flag it explicitly as an open item until confirmed.

**Warning signs:** Any private-WS connection code hardcoding a path copied from this document, `RESEARCH.md`, or a third-party example without a same-day confirmation against the official docs.

**Phase to address:** M1 (if private WS is used for account/read data) or M6A (private WS scaffolding) — must be confirmed before either, not assumed from either document.

---

### Pitfall 12: WebSocket reconnect silently desyncs local state instead of failing loudly

**What goes wrong (depth beyond `RESEARCH.md`'s "handle ping/pong, reconnect backoff, duplicate/out-of-order events"):** The generic and well-documented failure mode across exchange WebSocket integrations is a reconnect handler that re-establishes the connection and resumes consuming the stream *without* checking whether any messages were missed during the gap — silently drifting the local order-book/position/order-state cache away from exchange truth. Because the stream itself gives no error when this happens (from the client's point of view, messages just keep arriving), this is a silent-corruption bug, not a crash — exactly the kind of defect the M6A "recorded-response replay" and reconciliation tests are supposed to catch, but only if the test suite specifically injects a gap.

**Why it happens:** The naive, and easiest-to-write, reconnect implementation just calls "reconnect and keep listening"; explicit sequence-gap detection requires knowing the exchange's exact sequencing/heartbeat contract and adds a REST round-trip on every detected gap, which is easy to skip under time pressure since it doesn't affect the "happy path" demo.

**How to avoid:** On every reconnect, explicitly compare the first post-reconnect message's sequence/update-id (or, if Bithumb's private stream doesn't expose one, the first event's timestamp/order-state) against the last-known local state; if a gap is possible, treat local state as stale and force a REST-based reconciliation (matches the idempotency reconciliation logic already required in M6A) before trusting the stream again — never assume "reconnected" means "caught up." Explicitly unit/integration test this by injecting an artificial gap into a recorded-response replay.

**Warning signs:** A reconnect handler with no explicit post-reconnect reconciliation step; M6A test coverage that tests reconnect "works" but never tests reconnect-with-a-gap.

**Phase to address:** M6A (mock/read-only broker scaffolding — this is exactly where reconnect logic is written and must be tested, per the doc's own "reconnect recovers from duplicate/missing/out-of-order messages" test requirement — this pitfall specifies *how* to actually implement that test).

---

### Pitfall 13: JWT clock skew / nonce handling causes intermittent, hard-to-reproduce auth failures

**What goes wrong (under-emphasized — `RESEARCH.md`/`EXECUTION.md` mention "clock sync" and "JWT nonce behavior" as things to test, without depth):** JWT-based auth (used for `/v1/orders/chance` and other private endpoints) typically validates an issued-at/expiry window against the *server's* clock; if the bot's host clock drifts even a few seconds from NTP-synced time, tokens can be intermittently rejected as "expired" or "not yet valid" in a way that looks like a flaky network issue rather than a clock problem, and — worse — during live trading (out of this milestone, but the account/read-key auth path is built now, in M1) an intermittently-failing auth call in the middle of a reconciliation flow could be misinterpreted as "no such order" rather than "couldn't check."

**Why it happens:** Development machines and CI runners aren't always NTP-disciplined the way production trading infrastructure is expected to be, so the bug is invisible in a dev environment and first appears under real deployment conditions.

**How to avoid:** Enforce NTP synchronization (or an explicit clock-skew check against a trusted time source at startup) as part of the M0 startup self-check, alongside the already-planned "prints resolved Gate-1 decisions + key class + risk denominators" check. Ensure the auth-failure path is distinguished in code from a legitimate "resource not found" response — an auth/clock failure must never be silently treated as "the order doesn't exist."

**Warning signs:** Intermittent 401-style errors that don't correlate with any code change; retry logic that treats an auth failure the same way as a "not found" response.

**Phase to address:** M0 (startup self-check should include a clock-skew check) and M1 (first real JWT-authenticated calls — verify explicit error-type discrimination).

---

### Pitfall 14: Statistical overfitting risk and implementation risk are treated as the same thing

**What goes wrong (under-emphasized in the docs' otherwise-thorough multiple-testing coverage):** A strategy can pass every preregistered statistical control (DSR/PBO/paired bootstrap) and *still* produce materially different results when run through a different — or even subtly modified — execution engine, because the multiple-testing corrections only address *selection* risk (picking a lucky configuration among many tried), not *implementation* risk (the same nominal strategy definition producing different P&L depending on rounding, fill-timing conventions, or gap-rule edge cases in the simulator itself). This project's M2 simulator is exactly this kind of implementation-risk surface, and it means a "material change" to the simulator (already correctly defined as holdout-burning in `EXECUTION.md`) is dangerous *precisely because* it can move results by more than the statistical noise band the multiple-testing correction was calibrated against — the two risks compound rather than substitute for each other.

**Why it happens:** Once a rigorous statistical protocol is in place, it's natural to treat "passed the preregistered gate" as the whole answer; the possibility that the simulator itself (not the strategy) is the source of an unstable result is easy to overlook because it doesn't show up as a multiple-testing symptom.

**How to avoid:** Treat simulator determinism/stability as its own explicit test category, separate from the statistical acceptance gate: re-run the frozen strategy through the frozen simulator multiple times (should be bit-identical, since it's deterministic) and, as a robustness check, perturb minor implementation choices that shouldn't matter economically (e.g., internal computation order, floating intermediate vs. Decimal-throughout) and confirm results don't move — if they do, that's implementation risk leaking into the reported edge.

**Warning signs:** A "material change" to the simulator that was expected to be economically negligible turning out to move the primary metric by more than the bootstrap CI width.

**Phase to address:** M2 (determinism/reproducibility tests on the simulator itself, independent of strategy-level statistical tests) and M5-FINAL-FREEZE (confirm the frozen artifact's hash reproduces identical holdout-eligible results on a clean re-run before the holdout is opened).

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|-----------------|------------------|
| Using `float` for intermediate research/EDA notebooks, `Decimal` only in the "real" pipeline | Faster exploratory iteration, less friction with plotting libs | Habit bleeds into production code paths; a `float`-based notebook result gets pasted into config without re-deriving in `Decimal` | Only in disposable, clearly-labeled scratch notebooks that never write to config or the frozen artifact |
| Skipping the paired-resampling implementation and comparing two single-series CIs "for now" | Ships M3 faster with off-the-shelf bootstrap libraries | Silently weakens the R-clause acceptance test (Pitfall 3); a strategy could pass acceptance that a correct paired test would reject | Never — this is exactly the class of shortcut the preregistration discipline exists to prevent |
| Hardcoding one block length without a sensitivity table | Saves a few reruns during Gate-2 prep | Frozen protocol may rest on an unstable, arbitrarily-favorable CI (Pitfall 4) | Never for the frozen Gate-2 protocol; fine as a placeholder during early prototyping before anything is preregistered |
| Treating the M6A mock broker's schema/lifecycle tests as "integration tested" | Feels like M6A is more complete than it is | False confidence going into M6B; real-exchange surprises (auth rejection, unexpected error codes) arrive later than they should | Never — `EXECUTION.md` already explicitly forbids calling M6A "integration testing"; keep that framing consistent everywhere else in the codebase and docs |
| Reusing one dataset object across the full pipeline and slicing by date for train/selection/holdout | Simpler code, one source of truth | Makes accidental holdout access (Pitfall 6) nearly effortless | Acceptable only if the holdout slice is additionally protected by a physical/technical barrier (separate file, access log), not slicing discipline alone |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|-----------------|-------------------|
| Bithumb legacy `/trade/stop_limit` | Assuming it accepts a `client_order_id` like v2 orders, or reusing the v2 idempotency helper unmodified | Build a dedicated idempotency protocol for this endpoint per `RESEARCH.md`/`EXECUTION.md` §5/M6A: persist intent → send once → reconcile across `watch`/`wait`/`done`/`cancel`/executions/balances before any retry |
| Bithumb general v2 orders | Assuming a submitted `client_order_id` is deduplicated exchange-side without testing it | Treat exchange-side dedup as unverified until confirmed by a real (non-duplicate) test; always reconcile before retry regardless |
| Bithumb private WebSocket | Hardcoding the v1 or v2 path from either this document or a third-party source without reconfirming | Pull the exact path from `apidocs.bithumb.com` at implementation time and record the date verified (Pitfall 11) |
| Bithumb REST rate limits | Treating "requests/sec" as one global budget shared across public/private/order/batch calls | Implement separate token buckets per channel (public REST, private REST, order create/cancel, batch, WS connection) — the WS ~10/sec figure found in current sources is a *connection*-establishment limit, not a per-message cap |
| Bithumb candle pagination | Assuming the pagination cursor's boundary inclusivity matches intuition without testing it empirically | Explicitly test whether consecutive pages overlap or gap at the boundary candle and build the dedupe/upsert logic to be correct either way (Pitfall 9) |
| Bithumb fee schedule (incl. legacy stop-limit) | Assuming `/v1/orders/chance`'s reported fee also applies to the legacy automatic-order product | Query/verify the legacy product's fee separately; do not infer it from the general endpoint |
| JWT auth (all private REST/WS) | Treating an auth failure the same as "resource not found" | Explicitly discriminate error types; add a clock-skew self-check (Pitfall 13) |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|-----------------|
| Vectorized pandas backtest with implicit look-ahead via `.rolling()`/`.resample()` defaults | Backtest runs fast and looks clean, but numbers don't survive a "shift everything by one and see if results change wildly" sanity check | Explicit "as-of" unit tests per feature (Pitfall 7); never trust a fast vectorized backtest without a slower, obviously-correct event-driven reference implementation cross-check on a small sample | As soon as any single feature's window boundary is off by one candle — doesn't "break" gradually, it's silently wrong from day one |
| Re-fetching the full candle history on every run instead of incrementally appending | Fine at prototype scale | Wastes API rate-limit budget (per-channel caps, Pitfall/Integration table) and slows iteration as history grows | Once history spans enough months that a full re-pull risks tripping the private/public REST rate limit during a backtest-iterate loop |
| Running the block bootstrap with a very large number of resamples on the full aligned series in a single-threaded loop | Fine for quick iteration during protocol design | Slows down Gate-2 sensitivity-check iteration (Pitfall 4) enough that the sensitivity table gets skipped "to save time" | Once the sensitivity check across multiple block lengths is needed — budget for it up front rather than treating it as optional polish |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Granting a single Bithumb key both read and trade scopes "to simplify setup" | Collapses the three-class key policy (`RESEARCH.md`/`EXECUTION.md` §5/M0); a bug in read-path code could accidentally exercise trade permissions it should never have | Enforce three distinct keys (public/none, account-read, trade — trade only from M6B) at the credential-provisioning layer, not just by convention in application code |
| Leaving withdrawal permission enabled on a key "since it's disabled in the UI anyway" | UI toggles and API-key-level permissions are not always the same control; an account-level UI setting may not constrain what the API key itself is scoped to | Verify withdrawal permission is disabled **on the key itself**, not just assumed from an account-level setting, and re-verify after any Bithumb account/security-settings change |
| Treating the sleeve config value (`sleeve_cap_krw`) as sufficient capital-at-risk protection | A config number does not stop a bug, stale order, or bad reconciliation from trading the full account balance (`EXECUTION.md` §"Risk & capital isolation") | Technically isolate funds (dedicated subaccount, or keep only sleeve-sized funds in the API-accessible account) — config values are a secondary/defense-in-depth control, never the primary one |
| Assuming "cancel all open orders on disconnect" is a complete kill switch | The disconnect that triggers the cancel can be the same failure that prevents the cancel from being sent/received | Require an independent watchdog / separate reconciliation process that does not share the failure mode of the primary connection (`RESEARCH.md`/`EXECUTION.md` §5) |
| Logging full request/response payloads (including signed headers, nonce, or JWT) for debugging | A leaked log file exposes replayable auth material even without leaking the secret key itself, depending on the signature scheme | Redact `Api-Sign`, JWT tokens, and nonce values from logs by default; only log them behind an explicit, opt-in debug flag never enabled outside a sandboxed dev session |

## "Looks Done But Isn't" Checklist

- [ ] **Execution simulator "no same-candle fill":** verify not just the main backtest loop but every module added in M5 routes through the single `ExecutionSimulator` chokepoint (Pitfall 1) — grep for any direct `close`/`high`/`low` access outside it.
- [ ] **DSR/PBO acceptance gate:** verify the actual comparison operator against the frozen threshold, with a unit test proving both a pass and a fail fixture produce the expected Boolean (Pitfall 2).
- [ ] **Paired resampling for R:** verify the implementation resamples shared block indices for both series, not two independent single-series CIs (Pitfall 3) — check for a function literally named `bootstrap_ci(series)` being called twice.
- [ ] **6h aggregation:** verify not just "aggregated from 60m, not 240m" but that the bin origin/offset is explicit and identical between backtest and any live-polling aggregator (Pitfall 10).
- [ ] **Decimal correctness:** verify no `Decimal(<float literal>)` calls exist anywhere in the codebase (Pitfall 8) and that JSON parsing of Bithumb responses never routes numeric fields through `float` before `Decimal` construction.
- [ ] **Candle pagination:** verify a known-answer replay test across an artificial page boundary reproduces a byte-identical series to an unpaginated reference fetch (Pitfall 9).
- [ ] **Holdout isolation:** verify the holdout data is a physically separate artifact with an access log showing zero touches before Gate-3-freeze, not merely a date-filtered slice of the working dataset (Pitfall 6).
- [ ] **M6A "integration testing" claims:** verify no internal doc, commit message, or status update calls M6A's mock/schema/replay tests "integration tested" — that term is reserved for M6B per `EXECUTION.md`.
- [ ] **WebSocket reconnect:** verify test coverage specifically includes an injected sequence gap during reconnect, not just a clean reconnect (Pitfall 12).
- [ ] **Private WS path:** verify the exact v1-vs-v2 path was reconfirmed against official docs on a specific date, not copied from either this file or `RESEARCH.md` without reconfirmation (Pitfall 11).

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|----------------|-----------------|
| Same-candle look-ahead discovered post-M4 | MEDIUM | If discovered before the strategy freeze, it's a normal bug fix within the selection windows — no holdout burned since holdout isn't open yet. Re-run affected M4/M5 evaluations after the fix. |
| DSR/Calmar mismatch or `dsr <= 0` bug discovered post-M3 preregistration but pre-holdout | LOW–MEDIUM | Amend the Gate-2 protocol doc with a dated correction before any candidate is evaluated under the corrected rule; if candidates were already evaluated under the buggy rule, re-run them — no holdout impact since it hasn't opened. |
| Holdout accidentally accessed pre-open (Pitfall 6) | HIGH | Treat as equivalent to a holdout burn even though no metric was computed — the safest assumption is that any view of the data could have subconsciously influenced a design choice. Under Option B, the only clean remedy is collecting a fresh forward-data holdout after the current one is retired, per `EXECUTION.md`'s holdout-burn rule. |
| Material simulator/cost-model bug found during M6B (post-holdout) | HIGH | This is explicitly a holdout-burning event per `EXECUTION.md` spine (d): re-freeze the corrected artifact and re-run on forward data collected after the refreeze; the already-opened historical holdout cannot be reused. |
| Private WS path assumption (v1 vs v2) turns out wrong after code is written against it | LOW | Isolated to the WS client's connection-string constant and auth-header construction if the auth scheme (JWT bearer) is otherwise identical between versions — low-cost fix if caught before M6A tests are written against the wrong path, higher if mock tests were built assuming wrong message framing. |
| Block-length or bootstrap-implementation flaw discovered after Gate-2 freeze but before holdout | MEDIUM | Amend the frozen statistical protocol with a documented correction and sensitivity table (Pitfall 4), re-run affected selection-window evaluations; still pre-holdout, so no burn. |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|-------------------|---------------|
| Same-candle bias re-entering via helper/vectorized shortcuts | M2 build, M5 regression | Static check / grep for direct candle-field access outside the execution chokepoint |
| DSR mismatched to Calmar / `dsr <= 0` bug | M3 (Gate 2) | Unit tests on synthetic pass/fail fixtures for the acceptance-table code |
| Two-CI substitute for paired resampling | M3 (Gate 2) | Code review confirming shared block-index resampling; correlated-vs-uncorrelated synthetic test |
| Block-length sensitivity unchecked | M3 (Gate 2) | Sensitivity table across 2–3 block lengths committed with the frozen protocol |
| PBO/CSCV over-trusted as full validation | M3 (protocol role assignment), M5 (regime coverage) | Explicit written scope note next to every PBO report; separate regime-coverage checklist |
| Holdout leakage via debugging/notebooks | M2 (physical separation) → GATE-3-FREEZE (audit) | Access log on the holdout artifact showing zero pre-open touches |
| Look-ahead in feature/indicator warm-up or normalization | M2 (harness), M5 (apply per module) | "As-of" unit test per feature: value at `t` invariant to mutating candle `t`'s not-yet-available fields |
| Decimal misuse reintroducing float error | M0 (lint rule), M1/M2 (first real parsing/PnL) | AST/lint check banning `Decimal(<float literal>)`; property-based round-trip tests |
| Pagination/dedup/timezone candle defects | M2 (`BithumbData`) | Known-answer replay across an artificial page boundary |
| 6h aggregation bin-origin mismatch | M2 | Aggregation determinism test across arbitrary starting offsets |
| Private WS v1-vs-v2 factual conflict | M1 or M6A (whichever integrates private WS first) | Dated confirmation against `apidocs.bithumb.com`, recorded in build notes |
| WebSocket reconnect silent desync | M6A | Injected-gap reconnect test, not just clean-reconnect test |
| JWT clock skew / nonce auth failures | M0 (self-check), M1 (first real calls) | Startup clock-skew check; explicit auth-failure vs. not-found error discrimination |
| Statistical vs. implementation overfitting risk conflated | M2 (determinism tests), M5-FINAL-FREEZE (reproducibility) | Bit-identical re-run test on the frozen simulator; perturbation test on economically-inert implementation choices |

## Sources

- `docs/RESEARCH.md` and `docs/EXECUTION.md` (project-authoritative; eight rounds of independent audit) — baseline this file supplements, not restates.
- Web search (LOW confidence, uncurated, cross-referenced where possible — reverify all Bithumb-specific facts against `apidocs.bithumb.com` at implementation time):
  - Bithumb REST/WS rate-limit figures and legacy stop-limit watching-order-limit/fee figures — third-party blog/changelog mirrors of Bithumb's own announcements.
  - Bithumb private WebSocket path — third-party gists/aggregators describing a `v1/private` path, in direct conflict with `RESEARCH.md`'s `v2/private` — flagged as unresolved (Pitfall 11), not adjudicated by this research pass.
  - Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality" (foundational DSR paper) and secondary summaries of common DSR implementation mistakes.
  - Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest Overfitting" (foundational PBO/CSCV paper) and secondary summaries of PBO/CSCV scope limitations.
  - General quantitative-finance literature/blog discussion of block-bootstrap block-length sensitivity for autocorrelated financial time series.
  - General crypto-exchange engineering discussion of WebSocket sequence-gap detection and reconnect-resync patterns (cross-exchange pattern, not Bithumb-specific — applied here by analogy and flagged as such).
  - General Python `Decimal`-vs-`float` engineering pitfalls (community/blog sources) — cross-checked against Python's own `decimal` module documentation behavior (float-constructor imprecision, ambient rounding context) which is HIGH-confidence stdlib behavior independent of any web source.
  - General JWT `exp`/`nbf`/clock-skew handling discussion — generic JWT engineering pattern, applied to Bithumb's JWT-based private-endpoint auth by analogy.

---
*Pitfalls research for: Bithumb autotrading bot (Python, single-asset long-or-cash, backtest/validation milestone)*
*Researched: 2026-09-07*
