---
phase: quick-260912-erv
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/bithumb_bot/paper/runner.py
  - src/bithumb_bot/paper/state.py
  - src/bithumb_bot/paper/__init__.py
  - src/bithumb_bot/errors.py
  - tests/paper/conftest.py
  - tests/paper/test_runner.py
  - tests/paper/test_persistence.py
  - tests/paper/test_parity_with_backtest.py
  - tests/paper/test_warmup_isolation.py
  - tests/paper/test_resume_prefix_integrity.py
autonomous: true
requirements: [SCOPE-5]
must_haves:
  truths:
    - "After a run over a dataset with no forward-triggering price move, state.json shows paper_start_position_qty == '0', paper_start_cash_krw == format(config.starting_cash_krw, 'f'), paper_start_realized_pnl_krw == '0', paper_start_cumulative_fees_krw == '0', final_cash_krw == paper_start_cash_krw, final_position_qty == '0'. No warmup-sourced BUY, SELL, fee, cash delta, or realized P&L leaks into these values."
    - "The first entry in fills.jsonl, when a fill exists, is a BUY (side == 'buy'); a SELL entry in forward is always preceded in forward by a BUY entry — no fills.jsonl trace ever begins with a 'sell'."
    - "For any dataset whose warmup slice would emit a BUY signal at close of candle 1199, run_paper_session's forward_entries and final_cash_krw / final_position_qty are byte-identical to the same run over a synthetic dataset whose warmup is a flat sequence at the same price P as forward candle 1200's open — proving the warmup slice's transition-emitting shape does not leak portfolio effect."
    - "Resume after mutating ANY byte of ANY previously processed forward candle (open, high, low, close, volume, quote_volume, or open_time_utc) raises ProcessedPrefixMutatedError before any new line is appended to fills.jsonl / signals.jsonl / candle_fingerprints.jsonl; the exception message contains the offending candle's open_time_utc in ISO-8601 UTC."
    - "Resume after appending ONLY new forward candles (no mutation of any prior row) succeeds, returns resumed=True, and advances last_processed_open_utc to the last candle of the extended dataset."
    - "report.json and state.json continue to pin hysteresis_bps == '75' (string form), run_purpose == 'engineering_smoke', selection_eligible == false, holdout_eligible == false."
    - "AST scan of every .py under src/bithumb_bot/paper/ still contains zero references to bithumb_bot.broker; the import-linter contract 'Paper must not import broker' still reports KEPT."
  artifacts:
    - src/bithumb_bot/paper/runner.py
    - src/bithumb_bot/paper/state.py
    - src/bithumb_bot/errors.py
    - tests/paper/test_warmup_isolation.py
    - tests/paper/test_resume_prefix_integrity.py
  key_links:
    - src/bithumb_bot/paper/runner.py
    - src/bithumb_bot/paper/state.py
    - .planning/quick/260912-cao-implement-a-bounded-forward-paper-tradin/260912-cao-SUMMARY.md
---

<objective>
Fix two correctness defects landed by quick task 260912-cao in the bounded forward paper-trading runner. **D1:** hidden warmup buy leaks into the paper session — the runner currently calls `run_backtest` over the full [warmup + forward] window and only filters *events* by `source_open_time_utc`, so a warmup-window LONG transition materialises as a filled BUY that carries a position into forward, corrupting starting cash and inflating/deflating the first forward SELL's realised P&L. **D2:** resume verifies signal/fill prefix bytes but does not verify the underlying candle bytes — a silent mutation of a previously processed forward candle (e.g. stale mirror overwriting a corrected candle) slips through when derived signals happen to still match.

Purpose: `docs/IMPLEMENTATION_SCOPE.md` §5 "Paper operation" requires the paper portfolio to actually begin in CASH at `paper_start_ts` (D1) and requires resume to fail closed on ANY mutation of processed forward candles at candle-content granularity (D2). Neither invariant is currently enforced.

Output: `run_paper_session` produces (a) a paper session whose forward accounting starts from an unambiguously fresh portfolio at `paper_start_ts` regardless of what the strategy would have signalled during warmup, and (b) a per-candle SHA-256 fingerprint log (`candle_fingerprints.jsonl`) that resume walks and matches byte-for-byte against the current dataset, raising the new `ProcessedPrefixMutatedError` on the first divergence.
</objective>

<context>
@docs/IMPLEMENTATION_SCOPE.md
@.claude/CLAUDE.md
@.planning/quick/260912-cao-implement-a-bounded-forward-paper-tradin/260912-cao-PLAN.md
@.planning/quick/260912-cao-implement-a-bounded-forward-paper-tradin/260912-cao-SUMMARY.md
@src/bithumb_bot/paper/__init__.py
@src/bithumb_bot/paper/runner.py
@src/bithumb_bot/paper/state.py
@src/bithumb_bot/errors.py
@src/bithumb_bot/backtest/runner.py
@src/bithumb_bot/backtest/config.py
@src/bithumb_bot/execution/engine.py
@src/bithumb_bot/execution/stop.py
@src/bithumb_bot/execution/ledger.py
@src/bithumb_bot/execution/readiness.py
@src/bithumb_bot/strategy/baseline.py
@src/bithumb_bot/strategy/config.py
@src/bithumb_bot/market_data/candles.py
@src/bithumb_bot/market_data/dataset.py
@src/bithumb_bot/artifact/canonical.py
@src/bithumb_bot/cli/handlers/paper_run.py
@tests/paper/conftest.py
@tests/paper/test_runner.py
@tests/paper/test_persistence.py
@tests/paper/test_parity_with_backtest.py
</context>

<design_notes>

## Reuse discipline (unchanged, restated so the fix does not silently drift)

- MUST NOT modify `bithumb_bot.strategy`, `bithumb_bot.backtest.*` (runner, config, cost model), `bithumb_bot.execution.*` (engine, stop, ledger, readiness), or `bithumb_bot.broker` (which stays unimported from paper).
- MUST NOT change strategy parameters. `hysteresis_bps="75"`, `run_purpose="engineering_smoke"`, `selection_eligible=false`, `holdout_eligible=false` all remain pinned by the CLI handler untouched.
- Fees / slippage / rounding / min-order / notional cap / protective stop / stopped-out lockout / readiness — all remain inside `execute_intent`, `evaluate_protective_stop`, and `check_execution_readiness`, unmodified. This plan reuses those primitives; it does not re-derive them.
- Ponytail mode: no new dependencies. `hashlib` (SHA-256) + existing `pydantic`/`pyarrow`/`structlog`/`json`/`pathlib` surface only.
- No `git push`. Executor commits locally only.

## D1 — Warmup isolation: chosen approach

The engine's `run_backtest` cannot be told "start portfolio fresh at candle N". Adding a `warmup_only`/`skip_signals_before_utc` flag to `run_backtest` violates the hard "do not modify the backtest engine" constraint. The strategy is stateless (targets derive from a rolling SMA + hysteresis on the candle window itself), so warming an in-memory cache is a no-op. The only remaining reuse-preserving path is:

1. Call `bithumb_bot.strategy.generate_signals(dataset.candles, config.strategy)` — this is the ONLY function that knows how to compute SMA + hysteresis, and it MUST see the full warmup history so its SMA at forward candles is correct.
2. From the raw transition stream, derive a **forward-only** signal stream that starts from an implicit `prev_target = "CASH"` at `paper_start_idx = WARMUP_CANDLE_COUNT` and emits transitions only when the underlying per-candle target diverges from that fresh baseline as the loop walks forward candles. This yields a correct forward transition sequence for a fresh-portfolio session (see worked traces below).
3. Run a **fresh-portfolio loop over the forward candles only**, calling `execute_intent` and `evaluate_protective_stop` (both unmodified, imported from `bithumb_bot.execution`) — the same primitives `run_backtest` calls internally. The loop's event order is identical to `run_backtest`'s (execute pending intent at open → evaluate stop → consume close-boundary signal → maybe emit next-candle pending intent). Duplicating ~85 lines from `backtest/runner.py`'s main loop is the smallest change that keeps the engine primitives unmodified.

The duplicated loop is explicitly documented in `paper/runner.py`'s module docstring as necessary to isolate warmup portfolio effects while preserving the "no engine mods" constraint. Fee/slippage/rounding/stop/cap semantics are UNCHANGED because they live inside the primitives, not the loop.

### Per-candle target derivation (helper)

```
signals_by_source: dict[datetime, TargetState] = {s.source_open_time_utc: s.target_state for s in all_signals}

def _target_at(candle_idx: int, sorted_signal_open_times: list[datetime], signals_by_source) -> TargetState:
    """Target state ACTIVE at candle_idx, i.e. target set by the latest signal
    whose source_open_time <= candles[candle_idx].open_time_utc. 'CASH' if
    no prior signal exists."""
    open_time = dataset.candles[candle_idx].open_time_utc
    # binary search or reverse iteration — the whole thing is bounded by len(all_signals)
    ...
```

### Forward-only signal generation

```
forward_signals: list[StrategySignal] = []
prev_target: TargetState = "CASH"
for i in range(WARMUP_CANDLE_COUNT, len(dataset.candles)):
    curr_target = _target_at(i, ...)
    if curr_target != prev_target:
        forward_signals.append(
            StrategySignal(
                source_open_time_utc=dataset.candles[i].open_time_utc,
                signal_ts_utc=dataset.candles[i].open_time_utc + timedelta(minutes=dataset.unit_minutes),
                target_state=curr_target,
            )
        )
        prev_target = curr_target
```

### Fresh-portfolio forward loop (mirror of run_backtest lines 152–346, adapted)

```
state = LedgerState(cash_krw=config.starting_cash_krw, position_qty=Qty(Decimal("0")))
pending_intent: OrderIntent | None = None
active_stop: ProtectiveStop | None = None
stopped_out_lockout = False
invalid_reason: str | None = None
refusal_code: str | None = None
processed_last: datetime | None = None
forward_signals_by_source = {s.source_open_time_utc: s.target_state for s in forward_signals}

# Pre-flight verification_status['market_buy_fee_reservation'] — SAME
# guard as run_backtest lines 224–245. Return refusal (invalid_reason /
# refusal_code) if not usable; do not proceed.

for i in range(WARMUP_CANDLE_COUNT, len(dataset.candles)):
    view = dataset  # full dataset is safe: primitives look up by open_time_utc; strategy is stateless.
                    # (Optional: build a truncated view via dataset.model_copy(update={"candles": dataset.candles[: i + 1]})
                    #  if we want strict "no future candles visible" belt-and-suspenders — RECOMMENDED.)
    candle = dataset.candles[i]

    # (1)-(3) fire pending intent at this candle's open
    if pending_intent is not None and pending_intent.signal_ts_utc <= candle.open_time_utc:
        try:
            state, entry = execute_intent(state, pending_intent, view, snapshot, config.execution)
        except _DOMAIN_REFUSALS as exc:
            invalid_reason = str(exc); refusal_code = type(exc).__name__; break
        pending_intent = None
        if entry.side == "buy":
            stop_price_d = entry.fill_price.value * (Decimal("1") - config.protective_stop_fraction)
            active_stop = ProtectiveStop.from_entry(entry, stop_price=Money(stop_price_d))
        else:
            active_stop = None

    # (4)-(5) evaluate active stop against current candle
    if active_stop is not None:
        try:
            evaluation = evaluate_protective_stop(state, active_stop, view, snapshot, config.execution)
        except _DOMAIN_REFUSALS as exc:
            invalid_reason = str(exc); refusal_code = type(exc).__name__; break
        if evaluation.triggered:
            state = evaluation.new_state
            active_stop = None
            stopped_out_lockout = True

    # (6)-(8) consume forward-only signal
    target = forward_signals_by_source.get(candle.open_time_utc)
    if target == "LONG":
        if state.position_qty.value == 0 and not stopped_out_lockout and pending_intent is None:
            target_debit = state.cash_krw.value * config.target_sleeve_fraction
            intended_pre_fee = target_debit / (Decimal("1") + fee_rate)
            if intended_pre_fee > config.execution.max_notional_krw.value:
                invalid_reason = ...; refusal_code = "NotionalCapExceededError"; break
            pending_intent = OrderIntent.buy_from_signal(candle, Money(intended_pre_fee))
    elif target == "CASH":
        if state.position_qty.value > 0:
            if not (pending_intent is not None and pending_intent.side == "sell"):
                pending_intent = OrderIntent.sell_from_signal(candle, state.position_qty)
        elif stopped_out_lockout:
            stopped_out_lockout = False

    processed_last = candle.open_time_utc

# ...persist candle fingerprint(s), append fills/signals, save state.
```

Import `_DOMAIN_REFUSALS` from `bithumb_bot.backtest.runner` (already public via `run_backtest`'s `raise Xxx from exc` handling; alternatively re-declare the SAME tuple locally — either is acceptable, executor's discretion).

### Worked trace (matches must_have truth #2)

Fixture: warmup ends with a spike at candle 1199 (LONG target). Forward candle 1200 stays LONG, forward 1201 drops hard (CASH), forward 1202 stays CASH.

- `generate_signals(all)` emits: LONG@1199 (warmup), CASH@1201 (forward). Two raw signals.
- `_target_at(1200) = LONG`, `_target_at(1201) = CASH`, `_target_at(1202) = CASH`.
- Forward-only stream starts `prev_target=CASH`:
  - i=1200: curr=LONG ≠ CASH → emit LONG@1200. prev=LONG.
  - i=1201: curr=CASH ≠ LONG → emit CASH@1201. prev=CASH.
  - i=1202: curr=CASH == CASH → no signal.
- Forward loop: LONG@1200 → BUY intent, fills at 1201 open. CASH@1201 → SELL intent (position > 0), fills at 1202 open.
- Forward entries: `[buy@1201, sell@1202]`. First entry side == "buy" ✓ (truth #2). Any SELL is preceded by a BUY ✓.

Fixture with all-flat forward: no forward transition emitted. Forward entries empty. `final_cash_krw == starting_cash_krw`, `final_position_qty == "0"` ✓ (truth #1).

### state.json shape changes (D1)

New required fields (append to `PaperState`, extra="forbid" already enforced so these MUST be present):

```
"paper_start_cash_krw":            "<Decimal string> = format(config.starting_cash_krw.value, 'f')"
"paper_start_position_qty":        "0"
"paper_start_realized_pnl_krw":    "0"
"paper_start_cumulative_fees_krw": "0"
```

These four fields are literal invariants (three are constant `"0"`; one is a copy of `starting_cash_krw`). They exist so the D1 test has an unambiguous, machine-checkable contract for the paper-start portfolio snapshot. `schema_version` bumps to `2` to signal the shape change. Loading a `schema_version == 1` state file raises `ForwardDatasetDivergenceError` (state.json from the pre-fix binary is not resume-compatible with the post-fix binary — refuse rather than silently upgrade).

## D2 — Immutable-prefix verification: chosen approach

**Storage:** new append-only file `candle_fingerprints.jsonl` under `--state-dir`, one JSON object per line, one line per PROCESSED forward candle (in the same chronological order as candles are processed by the forward loop). Schema (frozen contract — test verifies this exact shape):

```
{"open_time_utc": "<ISO-8601 UTC>", "sha256": "<64-char hex>"}
```

**Fingerprint canonicalization:** SHA-256 of `canonical_bytes` of a dict with these keys in this exact set (sorting is handled by `canonical_bytes`'s sort_keys=True):

```
{
  "market":         candle.market,          # str
  "unit_minutes":   candle.unit_minutes,    # int
  "open_time_utc":  candle.open_time_utc.isoformat(),   # str
  "open":           candle.open,            # Decimal-string as stored on the Candle model
  "high":           candle.high,            # Decimal-string
  "low":            candle.low,             # Decimal-string
  "close":          candle.close,           # Decimal-string
  "volume":         candle.volume,          # Decimal-string
  "quote_volume":   candle.quote_volume,    # Decimal-string
}
```

Helper lives in `paper/state.py` as `candle_fingerprint(candle: Candle) -> str` and is unit-tested directly (known-answer vector).

**Resume-time verification** (added inside `run_paper_session` BEFORE any new line is appended anywhere and BEFORE the fresh-portfolio forward loop runs):

```
recorded = read_jsonl(state_dir / "candle_fingerprints.jsonl")
current_by_open_time = {c.open_time_utc: c for c in dataset.candles}
for row in recorded:
    open_time = datetime.fromisoformat(row["open_time_utc"])
    if open_time not in current_by_open_time:
        raise ProcessedPrefixMutatedError(
            f"previously processed forward candle at {open_time.isoformat()} is "
            "missing from the current dataset — refusing to append onto a "
            "candle prefix whose content changed"
        )
    if candle_fingerprint(current_by_open_time[open_time]) != row["sha256"]:
        raise ProcessedPrefixMutatedError(
            f"previously processed forward candle at {open_time.isoformat()} has "
            "mutated (SHA-256 mismatch) — refusing to append onto a candle "
            "prefix whose content changed"
        )
```

The verification runs BEFORE the existing `_assert_prefix_replay` fills/signals check — a mutated candle whose derived signal happens to coincide would otherwise slip past the fills/signals replay. Both checks then run: a bug in either surfaces the tighter one first.

**Append-side:** in the same loop pass that appends new lines to `fills.jsonl` / `signals.jsonl`, also append one fingerprint line per NEW forward candle processed this invocation (indices `prior_forward_candle_count .. len(dataset.candles) - 1`). Sequencing: fingerprint FIRST, then fills, then signals, then `save_state` last — so a crash mid-write leaves the fingerprint log strictly `<=` the fills/signals log, which is fail-closed on next resume (a fingerprint with no corresponding fill is a strictly stricter constraint than the reverse).

**No embedded prefix hash in state.json.** A rolling hash bundled into state.json was considered but rejected: per-candle records are localizable (the error message names the offending open_time), debuggable, and the append-only file has exactly the same durability guarantees as fills.jsonl / signals.jsonl (already tested).

## New exception (errors.py)

```
class ProcessedPrefixMutatedError(BithumbBotError):
    """Raised on resume when a previously processed forward candle no longer
    matches its recorded per-candle SHA-256 fingerprint (or is missing from
    the current dataset entirely).

    Distinct from ForwardDatasetDivergenceError (which fires on
    invocation-level input drift: warmup slice hash, config, snapshot,
    market, unit, hysteresis) and from FillReplayDivergenceError (which
    fires when the derived fills/signals stream diverges from the on-disk
    audit trail). This exception fires strictly on CANDLE-CONTENT
    mutation of the processed prefix, catching the case where a stale
    mirror or manual edit rewrites a completed candle whose derived
    signal/fill happens to coincidentally still match.

    Message names the offending candle open_time_utc; no dataset bytes are
    spliced into the exception surface.
    """
```

Add to `__all__` alphabetical position. Constructor takes a plain string message (no keyword args — matches sibling paper-runner exceptions).

## Test discipline — REPLACE, do not delete

The current `tests/paper/test_runner.py::TestWarmupExcludedFromForwardPnl::test_warmup_sourced_entry_excluded_from_forward_activity` asserts the D1 defect as intended behavior (see its `assert len(result.backtest_result.entries) == 2`, `assert result.backtest_result.entries[0].side == "buy"`, and `assert result.forward_entries[0].side == "sell"` — a warmup-sourced BUY carried forward and a forward SELL of a non-existent-in-forward position). It MUST be REPLACED by a stricter test (in `tests/paper/test_warmup_isolation.py`) that asserts the D1 invariants, NOT silently removed. The plan lists the exact test class/method to replace so the executor and any plan-checker can verify the substitution happened.

The current `tests/paper/test_parity_with_backtest.py::TestParityWithBacktestEngineDeterministic::test_forward_entries_match_filtered_backtest_entries` and the property test `test_parity_property_random_forward_tail` both assert `actual.forward_entries == filter(expected.entries by source_open_time_utc >= paper_start_ts_utc)`. This parity contract is INVALID under D1's fix (the paper runner no longer calls `run_backtest`, and the filtered result would include warmup-inherited SELLs the paper runner deliberately no longer produces). The property test is REPLACED with a **determinism** property (same-inputs → byte-identical outputs across two invocations) and a **swap-equivalent-warmup** property (a warmup with a would-be BUY signal produces the SAME forward_entries as a synthetic flat-at-paper-start warmup — must_have truth #3).

Nothing in `tests/paper/test_persistence.py::TestRestartProducesIdenticalStateAndNoDuplicateFills` or `TestDatasetFaultsFailClosed` implicitly assumes the D1 defect — those tests are ADDITIVE (D2 mutation cases live in the new `tests/paper/test_resume_prefix_integrity.py`, leaving the existing persistence tests as regression coverage for the append-extend and gap/duplicate/incomplete paths).

</design_notes>

<tasks>

<task type="auto">
  <name>Task 1: D1 fix — warmup isolation via forward-only signals + fresh-portfolio loop; replace defect-tolerant tests; add adversarial warmup-isolation tests</name>
  <files>src/bithumb_bot/paper/runner.py, src/bithumb_bot/paper/state.py, src/bithumb_bot/paper/__init__.py, tests/paper/conftest.py, tests/paper/test_runner.py, tests/paper/test_parity_with_backtest.py, tests/paper/test_warmup_isolation.py</files>
  <action>
    - **Do NOT modify** `bithumb_bot.strategy`, `bithumb_bot.backtest.*`, `bithumb_bot.execution.*`, or `bithumb_bot.broker`.
    - In `src/bithumb_bot/paper/state.py`: extend the `PaperState` pydantic model (`extra="forbid"`, `strict=True`, frozen) with four new required string fields — `paper_start_cash_krw`, `paper_start_position_qty`, `paper_start_realized_pnl_krw`, `paper_start_cumulative_fees_krw` — each validated as a Decimal-parsable string via the existing `_require_decimal_string` validator (reuse the existing `_validate_decimal_string` field_validator, extend its field list). Bump `schema_version` semantic to `2`; add a top-of-file docstring note explaining that a `schema_version == 1` file on disk is NOT resume-compatible (loader must surface this as `ForwardDatasetDivergenceError`, message: `"state.json schema_version=1 predates the D-erv warmup-isolation fix — delete state.json to start a fresh session"`). The `load_state` function raises `ForwardDatasetDivergenceError` (not a pydantic validation error) when it observes `schema_version != 2`.
    - In `src/bithumb_bot/paper/runner.py`: rewrite `run_paper_session` per `<design_notes>` D1 approach:
      - Keep ALL existing pre-flight (state_dir validation, candle count, tz-aware UTC, incomplete-candle refusal, warmup_sha256 / config_sha256 / snapshot_sha256 / paper_start_ts_utc / market / unit / hysteresis divergence checks against prior state) — UNCHANGED.
      - Remove the call to `run_backtest(dataset, snapshot, config)`. Replace with the fresh-portfolio forward loop sketched in `<design_notes>` (mirrors `run_backtest` lines 152–346, calling `execute_intent` and `evaluate_protective_stop` from `bithumb_bot.execution` unmodified). The loop iterates from `WARMUP_CANDLE_COUNT` to `len(dataset.candles) - 1`, starts `state = LedgerState(cash_krw=config.starting_cash_krw, position_qty=Qty(Decimal("0")))`, and consumes signals from `forward_signals_by_source` (not `signals_by_source` over all candles).
      - Include the buy-fee-model verification pre-flight (`snapshot.verification_status["market_buy_fee_reservation"]` + `config.execution.allow_provisional_fee_model`) VERBATIM from `run_backtest` (copy-paste with a code comment: `# duplicated from backtest/runner.py: fee-model pre-flight lives inside run_backtest which we no longer call — same semantics preserved`).
      - Import `_DOMAIN_REFUSALS` from `bithumb_bot.backtest.runner` (add to module imports). It is a module-level tuple, importable without cycles.
      - Add a private helper `_forward_only_signals(all_candles, all_signals, paper_start_idx, unit_minutes) -> list[StrategySignal]` implementing the algorithm from `<design_notes>` (per-candle target derivation + fresh-CASH baseline transition emission). Order the returned list by `source_open_time_utc` ascending.
      - `PaperSessionResult` field `backtest_result: BacktestResult | None` is REPURPOSED to hold a synthesised `BacktestResult` built from the forward loop's terminal state (so downstream `paper_run.py` handler that reads `session.backtest_result.final_cash_krw` etc. keeps working). Populate: `final_state`, `entries=tuple(forward loop's collected entries)`, `final_cash_krw`, `final_position_qty`, `active_protective_stop`, `pending_intent`, `stopped_out_lockout`, `processed_first_open_utc`, `processed_last_open_utc`, `invalid_reason`, `refusal_code`, `used_provisional_fee_model`, `signals_generated=len(forward_signals)`. The handler in `src/bithumb_bot/cli/handlers/paper_run.py` does NOT need modification — verify by grep.
      - Build the new `PaperState` with the four new invariant fields populated: `paper_start_cash_krw = format(config.starting_cash_krw.value, "f")`, the other three literal `"0"`. Keep `final_cash_krw` / `final_position_qty` populated from the forward loop's terminal state (same shape as before).
      - Update the module docstring to explain the D1 fix: the paper runner no longer calls `run_backtest`; it calls `generate_signals` for SMA/hysteresis computation and calls `execute_intent`/`evaluate_protective_stop` primitives (unmodified) inside a fresh-portfolio forward loop. Cite the "no engine mods" constraint as the rationale for the ~85-line loop duplication.
    - In `src/bithumb_bot/paper/__init__.py`: no export changes needed (already re-exports `PaperSessionResult`, `run_paper_session`, `PaperState`). Verify.
    - **In `tests/paper/test_runner.py`: REPLACE the class `TestWarmupExcludedFromForwardPnl` in place** with a stricter class `TestWarmupSignalDoesNotLeakIntoForward` covering exactly the D1 invariants: (a) construct the same fixture (warmup spike at index 1199 that would emit a warmup LONG signal, forward extreme drop at 1201) and assert `result.forward_entries[0].side == "buy"` (a BUY at forward index 1201, sourced from the newly emitted forward LONG@1200 in the forward-only stream — NOT a SELL of a warmup-inherited position), `result.forward_entries[1].side == "sell"` (sell at 1202 from CASH@1201), and `all(e.source_open_time_utc >= result.paper_start_ts_utc for e in result.forward_entries)`. (b) A second sub-test with a fixture where warmup would emit LONG but forward is all-flat: assert `result.forward_entries == ()`, `state.json.final_cash_krw == format(config.starting_cash_krw.value, "f")`, `state.json.final_position_qty == "0"`. **The old class MUST be gone from `test_runner.py`; grep-verify: `grep -c "TestWarmupExcludedFromForwardPnl" tests/paper/test_runner.py` returns 0.**
    - **In `tests/paper/test_parity_with_backtest.py`: REPLACE the class `TestParityWithBacktestEngineDeterministic` and the property test `test_parity_property_random_forward_tail`** with a new class `TestPaperSessionDeterminism` asserting: (a) two consecutive invocations against the SAME `(dataset, snapshot, config, tmp_path)` yield byte-identical `state.json`, `fills.jsonl`, `signals.jsonl`, `candle_fingerprints.jsonl`, and `PaperSessionResult.forward_entries`; (b) a Hypothesis property test (`max_examples=15`, `deadline=None`, function-scoped-fixture health-check suppressed — same shape as the existing property test) that draws random small forward tails (`length 1–10`, prices in `[90, 110]` — same as the existing generator) and asserts determinism of the forward loop across two invocations. The `filter(run_backtest.entries)` comparison is REMOVED because it no longer expresses the correct contract (paper runner and `run_backtest` deliberately diverge on warmup-sourced fills). **Grep-verify: `grep -c "run_backtest" tests/paper/test_parity_with_backtest.py` returns 0.**
    - Create `tests/paper/test_warmup_isolation.py` with a class `TestWarmupIsolationSwapEquivalent` implementing must_have truth #3: build two datasets `A` and `B` sharing the same forward candles and satisfying `all_close_prices_warmup_A[-1] == all_close_prices_warmup_B[-1] == forward_candle[0].open`; warmup A contains a spike-then-return pattern at index 1199 (would emit LONG signal in warmup); warmup B is all-flat at the terminal price P. Run `run_paper_session(A, snapshot, config, tmp_A, now_utc=...)` and `run_paper_session(B, snapshot, config, tmp_B, now_utc=...)`. Assert: `result_A.forward_entries == result_B.forward_entries` (byte-equal). Assert `result_A.backtest_result.final_cash_krw == result_B.backtest_result.final_cash_krw` and `.final_position_qty` equal. **Note in the test docstring:** because SMA over 1200 candles is sensitive to ANY variation, the two warmups will NOT yield literally identical SMA at every forward index — the test's contract is that the *forward-visible LEDGER* is identical, which is the invariant that matters (warmup portfolio effect is zero). The fixture achieves this by choosing prices such that neither warmup's SMA-at-forward-boundary crosses the hysteresis band relative to the forward candles, so no forward transition depends on which of A or B was passed. See the fixture doc for the exact price recipe: warmup A = `[P] * 1198 + [3P (spike), P (return)]`; warmup B = `[P] * 1200`; both average to approximately P; adjust the spike magnitude in the fixture to keep both SMAs within the hysteresis band. Add a second class `TestPaperStartInvariants` asserting must_have truth #1: over an all-flat dataset (no forward signals possible), `state.json` shows `paper_start_cash_krw == format(config.starting_cash_krw.value, "f")`, `paper_start_position_qty == "0"`, `paper_start_realized_pnl_krw == "0"`, `paper_start_cumulative_fees_krw == "0"`, `final_cash_krw == paper_start_cash_krw`, `final_position_qty == "0"`. Add a third class `TestFirstFillIsBuy` asserting must_have truth #2: over a dataset where forward triggers a LONG-then-CASH sequence, `fills.jsonl` line 1 has `"side": "buy"`, line 2 has `"side": "sell"`; over a dataset where warmup would trigger LONG-CASH-LONG but forward is flat, `fills.jsonl` is empty.
    - Update `tests/paper/conftest.py` if needed: add a `flat_warmup_and_forward_dataset` fixture returning `(dataset, snapshot, config)` where the forward window is all flat (no signals fire) — used by `TestPaperStartInvariants`. Do NOT remove or modify the existing `paper_fixture` — it is still used by `TestRestartProducesIdenticalStateAndNoDuplicateFills` and the parity test replacement.
  </action>
  <verify>
    <automated>uv run pytest tests/paper/test_runner.py tests/paper/test_warmup_isolation.py tests/paper/test_parity_with_backtest.py tests/paper/test_persistence.py -x -q</automated>
    <automated>uv run mypy --strict src/bithumb_bot/paper</automated>
    <automated>uv run python -c "import subprocess, sys; r = subprocess.run(['grep','-c','TestWarmupExcludedFromForwardPnl','tests/paper/test_runner.py'], capture_output=True, text=True); assert r.stdout.strip() == '0', 'defect-tolerant test class still present'"</automated>
    <automated>uv run python -c "import subprocess, sys; r = subprocess.run(['grep','-c','run_backtest','tests/paper/test_parity_with_backtest.py'], capture_output=True, text=True); assert r.stdout.strip() == '0', 'parity test still references run_backtest — the defect-tolerant contract was not replaced'"</automated>
  </verify>
  <done>
    `run_paper_session` no longer calls `run_backtest` (grep-verified). The fresh-portfolio forward loop uses `execute_intent` and `evaluate_protective_stop` unmodified. `PaperState` carries the four new `paper_start_*` fields and its `schema_version` is `2`. `TestWarmupExcludedFromForwardPnl` is gone from `test_runner.py`; `TestWarmupSignalDoesNotLeakIntoForward` (with the two sub-tests specified above) is present and passing. `test_parity_with_backtest.py` no longer references `run_backtest`; `TestPaperSessionDeterminism` (with hand-crafted + Hypothesis property tests) is present and passing. `tests/paper/test_warmup_isolation.py` exists with three classes covering must_have truths #1, #2, #3, all passing. `mypy --strict src/bithumb_bot/paper` is clean. Every pre-existing test outside `tests/paper/` still passes (regression-clean).
  </done>
</task>

<task type="auto">
  <name>Task 2: D2 fix — per-candle SHA-256 fingerprint log + resume-time immutable-prefix verification; add adversarial mutation tests; new ProcessedPrefixMutatedError exception</name>
  <files>src/bithumb_bot/errors.py, src/bithumb_bot/paper/state.py, src/bithumb_bot/paper/runner.py, tests/paper/test_persistence.py, tests/paper/test_resume_prefix_integrity.py</files>
  <action>
    - **Do NOT modify** `bithumb_bot.strategy`, `bithumb_bot.backtest.*`, `bithumb_bot.execution.*`, or `bithumb_bot.broker`. `hashlib` (via `sha256_hex` from `bithumb_bot.artifact.canonical`) is the only signing primitive.
    - In `src/bithumb_bot/errors.py`: add a new exception class `ProcessedPrefixMutatedError(BithumbBotError)` per the `<design_notes>` skeleton (docstring explains the distinction from `ForwardDatasetDivergenceError` and `FillReplayDivergenceError`; constructor takes a positional string message, no kwargs). Insert alphabetically. Add to `__all__` in the correct alphabetical position (between `ProhibitedCredentialDetectedError` and `PublicRestErrorResponseError`).
    - In `src/bithumb_bot/paper/state.py`: add a public helper function `candle_fingerprint(candle: Candle) -> str` (import `Candle` from `bithumb_bot.market_data.candles`) that computes SHA-256 of `canonical_bytes` of the exact 9-key dict specified in `<design_notes>` D2 (`market`, `unit_minutes`, `open_time_utc.isoformat()`, `open`, `high`, `low`, `close`, `volume`, `quote_volume`). Add to `__all__`. Add a docstring naming the fields, the order-independence (`canonical_bytes` sorts keys), and the string-form Decimal preservation (D-49 discipline: prices/quantities on `Candle` are already Decimal-string; the fingerprint MUST NOT coerce them through `float`).
    - Also in `src/bithumb_bot/paper/state.py`: add two helpers:
      - `read_fingerprints(state_dir: Path) -> list[dict[str, str]]` — thin wrapper over the existing `read_jsonl(state_dir / "candle_fingerprints.jsonl")`.
      - `append_fingerprint(state_dir: Path, candle: Candle) -> None` — computes the fingerprint and calls `append_jsonl(state_dir / "candle_fingerprints.jsonl", {"open_time_utc": candle.open_time_utc.isoformat(), "sha256": candle_fingerprint(candle)})`.
    - In `src/bithumb_bot/paper/runner.py`: import `ProcessedPrefixMutatedError` from `bithumb_bot.errors` and `candle_fingerprint`, `read_fingerprints`, `append_fingerprint` from `bithumb_bot.paper.state`. Insert D2 verification INSIDE `run_paper_session`, positioned:
      - AFTER the existing `ForwardDatasetDivergenceError` check (warmup/config/snapshot/paper_start_ts_utc drift) — so an invocation-level input change surfaces THAT exception first (already-tested behavior preserved).
      - BEFORE the existing `_assert_prefix_replay` call over `fills.jsonl` / `signals.jsonl` — so a candle-content mutation that coincidentally produces the same derived signals still fails on THIS check.
      - BEFORE the fresh-portfolio forward loop from Task 1 runs — so no new lines are appended to any of `fills.jsonl`, `signals.jsonl`, `candle_fingerprints.jsonl`, or `state.json` on a mutated-prefix resume.
      - Implementation exactly as sketched in `<design_notes>` D2 "Resume-time verification". If `read_fingerprints` returns empty and prior state exists with `forward_candle_count > 0`, raise `ProcessedPrefixMutatedError(f"prior state.json reports forward_candle_count={prior_state.forward_candle_count} but candle_fingerprints.jsonl is empty — audit trail incomplete")`. If the recorded fingerprint count exceeds the number of matching-open_time candles found in the current dataset, raise with a message naming the first missing open_time.
    - Extend the append-side of the forward loop: for each new forward candle processed (indices `prior_forward_candle_count .. len(dataset.candles) - 1` — the range of indices whose fingerprint is NOT already recorded), call `append_fingerprint(state_dir, dataset.candles[idx])` BEFORE any corresponding fill/signal line is appended (fingerprint-first sequencing per `<design_notes>` D2). If the loop refuses mid-processing (a `_DOMAIN_REFUSALS` exception), do NOT rewind fingerprints already written for candles that DID process cleanly — a partially-processed forward window's fingerprints legitimately reflect what was processed; the next invocation's D2 check will accept them because those candles' bytes were verified as unchanged.
    - `save_state` at end-of-invocation writes `state.json` last (unchanged), preserving fingerprint-first sequencing across a crash.
    - In `tests/paper/test_persistence.py`: extend `TestRestartProducesIdenticalStateAndNoDuplicateFills::test_second_run_is_a_no_op_third_run_only_appends` with an assertion that `candle_fingerprints.jsonl` exists after run 1, is byte-identical after run 2 (no-op), and grew by exactly 5 lines after run 3 (append-extend). Do NOT modify the existing gap/duplicate/incomplete sub-tests.
    - Create `tests/paper/test_resume_prefix_integrity.py` with the following classes:
      - `TestFingerprintKnownAnswerVector`: hand-craft a `Candle` with fixed field values and assert `candle_fingerprint(c)` equals a hard-coded 64-char hex string (compute it once during test authoring by running the helper in a REPL, then paste; this locks the canonical schema — any future accidental change to the fingerprint dict shape or field order breaks this test).
      - `TestFingerprintOrderIndependent`: build two Candles with the same field values and assert `candle_fingerprint(c1) == candle_fingerprint(c2)`.
      - `TestMutatedFieldFailsClosed`: parametrise over each of `open`, `high`, `low`, `close`, `volume`, `quote_volume`, `open_time_utc` (7 cases). For each: run `run_paper_session` over a fixture dataset that processes ≥ 2 forward candles; then build a NEW dataset object where ONE previously processed candle has the parametrised field flipped by a minimal amount (e.g. `open`: `"100000000"` → `"100000001"` — a 1-unit change, so the derived SMA and signal ARE affected only imperceptibly, but the fingerprint MUST diverge); attempt to resume by calling `run_paper_session` again with the mutated dataset over the same `state_dir`; assert `pytest.raises(ProcessedPrefixMutatedError)` with a `match=` regex naming the offending candle's ISO-8601 `open_time_utc`; assert `fills.jsonl`, `signals.jsonl`, `candle_fingerprints.jsonl`, and `state.json` byte-hashes are UNCHANGED after the failed resume (proving fail-closed: no partial write).
      - `TestMissingProcessedCandleFailsClosed`: after a successful run, build a NEW dataset where a previously processed forward candle is DELETED entirely (surrounding candles renumbered contiguously so the dataset remains schema-valid); assert the resume raises `ProcessedPrefixMutatedError` with a message identifying the missing open_time.
      - `TestAppendOnlyExtensionSucceeds`: run once with dataset D, then run again with dataset D+ (D plus new candles appended) with EVERY previously processed candle byte-identical; assert `resumed=True`, `new_fills_this_invocation >= 0`, `candle_fingerprints.jsonl` grew by exactly the number of new forward candles processed, existing fingerprint lines byte-equal to the previous file's prefix.
      - `TestCoincidentSignalMutationStillFailsClosed`: mutate a processed candle's `volume` field (which does NOT affect the SMA-of-closes signal path); assert the resume still raises `ProcessedPrefixMutatedError` (proving D2 catches mutations that are invisible to the fills/signals replay check).
    - Reuse fixture helpers from `tests/paper/conftest.py` (add small helpers as needed for the "flip one field" pattern — e.g. `mutate_field(dataset, idx, field, new_value) -> CandleDataset`).
  </action>
  <verify>
    <automated>uv run pytest tests/paper/test_persistence.py tests/paper/test_resume_prefix_integrity.py -x -q</automated>
    <automated>uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/errors.py</automated>
    <automated>uv run python -c "from bithumb_bot.errors import ProcessedPrefixMutatedError, BithumbBotError; assert issubclass(ProcessedPrefixMutatedError, BithumbBotError); assert 'ProcessedPrefixMutatedError' in __import__('bithumb_bot.errors', fromlist=['__all__']).__all__"</automated>
    <automated>uv run python -c "from bithumb_bot.paper.state import candle_fingerprint, append_fingerprint, read_fingerprints; print('ok')"</automated>
  </verify>
  <done>
    `ProcessedPrefixMutatedError` exists in `errors.py` and is exported in `__all__`. `candle_fingerprint`, `append_fingerprint`, `read_fingerprints` exist in `paper/state.py` and are exported. `run_paper_session` performs the D2 verification BEFORE the fills/signals replay check and BEFORE the forward loop, raising `ProcessedPrefixMutatedError` on the first mutated or missing processed candle. Fingerprint-first sequencing is honored on the append side. Every `tests/paper/test_resume_prefix_integrity.py` class passes (fingerprint known-answer vector, order independence, seven mutated-field cases, missing-candle case, append-only extension, coincident signal mutation). Extended `test_persistence.py` assertion on `candle_fingerprints.jsonl` byte-equality across no-op resume + growth-by-5 across append-extend passes.
  </done>
</task>

<task type="auto">
  <name>Task 3: Full-suite validation gate — pytest, mypy --strict, ruff, import-linter, no-broker contract, no-git-push discipline</name>
  <files>(no source edits — verification only; failures here trigger targeted fixes to files owned by T1/T2)</files>
  <action>
    Run the full validation gate and record the results verbatim in the executor's SUMMARY. Every check below MUST pass before the plan is considered done. Any failure sends the executor back to T1 or T2 to fix the root cause — do NOT patch symptoms in this task.

    - **Full pytest suite:** `uv run pytest -x -q` — every existing test outside `tests/paper/` still green (regression-clean); every test inside `tests/paper/` (including the two new files) green. Skip count must NOT increase beyond the pre-existing 1 Windows-symlink skip.
    - **mypy --strict on all touched source dirs:** `uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/errors.py src/bithumb_bot/cli/handlers/paper_run.py` — clean.
    - **ruff check parity:** `uv run ruff check src/bithumb_bot/paper src/bithumb_bot/errors.py` — no NEW findings beyond the pre-existing tolerated PLR structural-complexity findings on `paper_run.py::handler` and `test_runner.py` documented in 260912-cao-SUMMARY.md §"Accepted, Not Fixed". If T1/T2 introduce ANY new lint category, fix it in the owning task (do NOT suppress).
    - **import-linter:** re-run the same subprocess invocation the existing `tests/import_boundary/test_paper_no_broker.py` uses to verify both `Core must not import broker` and `Paper must not import broker` contracts report `KEPT`. Do NOT modify `pyproject.toml`'s `[tool.importlinter]` block.
    - **No broker import in paper (grep sanity):** `grep -rn "from bithumb_bot.broker\|import bithumb_bot.broker" src/bithumb_bot/paper/` returns zero matches (per must_have truth #7).
    - **State-file schema round-trip:** manual smoke check documented in SUMMARY — build a fixture dataset, run `run_paper_session`, `cat state_dir/state.json | jq .schema_version` returns `2`, the four `paper_start_*` fields are present with the specified values, `candle_fingerprints.jsonl` line count equals `state.json`'s `forward_candle_count`.
    - **`bt paper run --help` still lists all five original flags** (`--dataset`, `--snapshot`, `--config`, `--state-dir`, `--out`) — the CLI verb signature is UNCHANGED by this plan.
    - **CLI end-to-end regression:** `uv run bt paper run --dataset tests/fixtures/paper/synthetic_dataset.json --snapshot tests/fixtures/paper/snapshot.json --config tests/fixtures/paper/config.toml --state-dir <fresh_tmp> --out <fresh_tmp>/report.json` exits 0; a SECOND invocation with the SAME dataset (but a fresh `--out`) exits 0 with `new_fills_this_invocation=0` and byte-identical `state.json`. Same fixtures as the 260912-cao SUMMARY's smoke run; if the fixture files no longer exist, build ad-hoc equivalents (a 1210-candle KRW-BTC 4h dataset + observed snapshot + engineering-smoke research config) and note this in SUMMARY.
    - **`git push` is NOT invoked at any point.** All commits are local. Executor confirms in SUMMARY that `git log --oneline origin/master..HEAD` shows exactly the new commit(s) locally-committed but unpushed.
  </action>
  <verify>
    <automated>uv run pytest -x -q</automated>
    <automated>uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/errors.py src/bithumb_bot/cli/handlers/paper_run.py</automated>
    <automated>uv run ruff check src/bithumb_bot/paper src/bithumb_bot/errors.py</automated>
    <automated>uv run pytest tests/import_boundary/test_paper_no_broker.py -x -q</automated>
  </verify>
  <done>
    Full test suite green (regression-clean). mypy --strict clean on all edited source. ruff check introduces no new findings beyond documented, tolerated 260912-cao residuals. `Paper must not import broker` and `Core must not import broker` import-linter contracts both `KEPT`. Grep confirms zero `bithumb_bot.broker` references in `src/bithumb_bot/paper/`. `bt paper run --help` still names all five original flags. CLI smoke run passes end-to-end with byte-identical state.json across resumes. Executor's SUMMARY records the exact command outputs (pytest counts, mypy file count, ruff finding count, import-linter KEPT confirmation) and confirms no `git push` occurred.
  </done>
</task>

</tasks>

<verification>

Overall plan-level checks (belt-and-suspenders — every task's verify block runs these subsets, but here for the executor's end-of-plan self-audit):

1. `uv run pytest -x -q` — 100% green.
2. `uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/errors.py src/bithumb_bot/cli/handlers/paper_run.py` — clean.
3. `uv run ruff check src/bithumb_bot/paper src/bithumb_bot/errors.py` — no new findings beyond documented 260912-cao residuals.
4. `grep -rn "from bithumb_bot.broker\|import bithumb_bot.broker" src/bithumb_bot/paper/` — zero matches.
5. `grep -c "TestWarmupExcludedFromForwardPnl" tests/paper/test_runner.py` — returns `0` (defect-tolerant test class fully removed and replaced).
6. `grep -c "run_backtest" tests/paper/test_parity_with_backtest.py` — returns `0` (parity contract no longer expressed against `run_backtest`).
7. `grep -c "run_backtest" src/bithumb_bot/paper/runner.py` — returns `0` (paper runner no longer calls the backtest engine; it calls execution primitives directly).
8. `python -c "from bithumb_bot.errors import ProcessedPrefixMutatedError; print(ProcessedPrefixMutatedError.__doc__)"` — prints the docstring (i.e., class exists and is importable).
9. Smoke: build a 1210-candle KRW-BTC 4h fixture, run `bt paper run` twice against the same `--state-dir` with fresh `--out` each time — both exit 0; second run reports `new_fills_this_invocation=0`; `state.json` byte-identical.
10. `git log --oneline origin/master..HEAD` shows local commits only; NO `git push` occurred at any point.

</verification>

<success_criteria>

- **D1 fixed:** the paper portfolio at `paper_start_ts` is unambiguously fresh (`cash == starting_cash_krw`, `position == 0`, `realized_pnl == 0`, `cumulative_fees == 0`) — recorded in `state.json` as four explicit `paper_start_*` invariant fields (must_have truth #1), and the first entry in `fills.jsonl` is always a BUY when a fill exists (must_have truth #2). Warmup-emitted transitions produce zero portfolio effect: two datasets differing ONLY in whether the warmup slice contains a signal-emitting price pattern produce byte-identical `forward_entries` (must_have truth #3).
- **D2 fixed:** every processed forward candle's SHA-256 fingerprint is persisted to `candle_fingerprints.jsonl`; resume verifies each fingerprint byte-for-byte against the current dataset BEFORE any new line is appended; any mismatch (7 field types tested individually) or missing candle raises the new `ProcessedPrefixMutatedError` naming the offending `open_time_utc` (must_have truth #4); append-only extension with no mutation continues to succeed (must_have truth #5).
- **No engine mods.** `git diff --stat` shows changes ONLY under `src/bithumb_bot/paper/`, `src/bithumb_bot/errors.py`, and `tests/paper/**`. Zero lines changed in `src/bithumb_bot/strategy/`, `src/bithumb_bot/backtest/`, `src/bithumb_bot/execution/`, `src/bithumb_bot/broker/`, or `src/bithumb_bot/cli/handlers/paper_run.py`.
- **No new dependencies.** `pyproject.toml`'s dependency lists are unchanged. `hashlib` is stdlib (already used via `sha256_hex`).
- **Report + state.json still pin** `hysteresis_bps == "75"`, `run_purpose == "engineering_smoke"`, `selection_eligible == false`, `holdout_eligible == false` (must_have truth #6). The CLI handler's pin logic is UNMODIFIED.
- **Import boundary preserved.** `Paper must not import broker` import-linter contract remains `KEPT` (must_have truth #7).
- **REPLACE-not-DELETE discipline enforced.** `TestWarmupExcludedFromForwardPnl` (the specific defect-tolerant test class) is REPLACED by `TestWarmupSignalDoesNotLeakIntoForward`; the class name no longer appears in `test_runner.py`. `test_parity_with_backtest.py`'s `run_backtest` reference is REMOVED and replaced by `TestPaperSessionDeterminism`; the string `run_backtest` no longer appears in that file. Both replacements are grep-verifiable.
- **No `git push`.** Executor commits locally only.

</success_criteria>

<output>
Quick-mode convention: no formal SUMMARY.md is REQUIRED to satisfy the GSD plan-checker, but the executor SHOULD write `260912-erv-SUMMARY.md` alongside this plan recording (a) commit SHAs, (b) verbatim output of the four verify commands in Task 3, (c) any documented deviations, and (d) confirmation that no `git push` occurred — matching the 260912-cao-SUMMARY.md precedent set by the prior quick task.
</output>
