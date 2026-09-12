---
task: 260912-erv
title: Fix two correctness defects in the paper-trading runner (D1 warmup isolation, D2 immutable-prefix verification)
status: complete
one_liner: Fresh-portfolio forward-only loop severs the paper runner from run_backtest (D1), and a per-candle SHA-256 fingerprint sidecar (candle_fingerprints.jsonl) makes resume fail closed on ANY processed-candle content mutation before any new audit-trail line is appended (D2).
requirements: [SCOPE-5]
files_modified:
  - src/bithumb_bot/paper/runner.py
  - src/bithumb_bot/paper/state.py
  - src/bithumb_bot/errors.py
  - tests/paper/test_runner.py
  - tests/paper/test_parity_with_backtest.py
  - tests/paper/test_persistence.py
files_created:
  - tests/paper/test_warmup_isolation.py
  - tests/paper/test_resume_prefix_integrity.py
commits:
  - 6700aae: "docs(260912-erv): pre-dispatch plan for paper runner defect fix"
  - aad358f: "fix(quick-260912-erv): D1 warmup isolation — fresh-portfolio forward loop"
  - 5aa45d3: "fix(quick-260912-erv): D2 immutable-prefix verification on resume"
---

# Fix two correctness defects in the paper-trading runner — Summary

## Continuation note

This task was picked up mid-execution. T1 (D1 warmup isolation) had already
been committed by a prior executor as `aad358f`. T2 (D2 immutable-prefix
verification) had been PARTIALLY drafted, uncommitted, in the working tree:
`errors.py`'s `ProcessedPrefixMutatedError` class and `state.py`'s
`candle_fingerprint` / `read_fingerprints` / `append_fingerprint` helpers
were complete and correct, but `runner.py`'s diff turned out to contain
**only** import statements and docstring updates — the prior executor had
imported `ProcessedPrefixMutatedError`, `append_fingerprint`,
`candle_fingerprint`, and `read_fingerprints` into `runner.py` but never
actually called any of them. The resume-time verification and the
append-side fingerprint-writing were never wired into `run_paper_session`.
This executor completed that wiring, wrote the (previously nonexistent)
adversarial test file `tests/paper/test_resume_prefix_integrity.py`,
extended `tests/paper/test_persistence.py` per the plan, ran the full T2 +
T3 validation gates, and committed T2 as `5aa45d3`.

## What was built (T2 — this executor's work)

1. **`_verify_processed_prefix_fingerprints`** (`paper/runner.py`) — reads
   `candle_fingerprints.jsonl`, recomputes each recorded candle's
   fingerprint against the CURRENT dataset, and raises
   `ProcessedPrefixMutatedError` on the first mismatch or missing candle.
   Wired in via a new `_load_resume_context` helper that runs it
   immediately AFTER `_resume_divergence_check` (invocation-level drift —
   warmup hash / config / snapshot / market / unit / hysteresis / paper
   start ts — still surfaces first, unchanged) and BEFORE
   `_assert_prefix_replay` (the looser fills/signals byte-replay check)
   and BEFORE the fresh-portfolio forward loop runs. No new line is ever
   appended to `fills.jsonl` / `signals.jsonl` / `candle_fingerprints.jsonl`
   / `state.json` on a mutated-prefix resume.
2. **`_append_new_fingerprints`** (`paper/runner.py`) — appends one
   fingerprint line per NEW forward candle processed this invocation,
   BEFORE the corresponding fill/signal lines (fingerprint-first
   sequencing — a crash mid-write leaves the fingerprint log strictly
   `<=` the fills/signals log, which is fail-closed on the next resume).
3. Extracted `_load_resume_context`/`_append_new_fingerprints` out of
   `run_paper_session`'s body — the new wiring pushed the function over
   ruff's `PLR0915` (50-statement) threshold; extracting these two helpers
   brought it back under the limit without changing behavior.
4. **`tests/paper/test_resume_prefix_integrity.py`** (new, 233 lines, 12
   tests across 6 classes):
   - `TestFingerprintKnownAnswerVector` — locks the canonical 9-field
     schema against silent drift via a hard-coded SHA-256 hex value.
   - `TestFingerprintOrderIndependent` — construction-kwarg order does not
     affect the fingerprint (`canonical_bytes` sorts keys).
   - `TestMutatedFieldFailsClosed` — 6 parametrized cases mutating
     `open`/`high`/`low`/`close`/`volume`/`quote_volume` on a genuinely
     already-processed forward candle via real dataset mutation, each
     asserting `ProcessedPrefixMutatedError` naming the offending
     `open_time_utc`, plus byte-for-byte-unchanged audit files after the
     failed resume. A 7th test covers `open_time_utc` via sidecar
     tampering — see "Deviation" below for why.
   - `TestMissingProcessedCandleFailsClosed` — dropping the last processed
     forward candle from the dataset raises `ProcessedPrefixMutatedError`
     naming its `open_time_utc`.
   - `TestAppendOnlyExtensionSucceeds` — append-extending with 3 new flat
     candles (no mutation of the processed prefix) succeeds, grows
     `candle_fingerprints.jsonl` by exactly 3 lines, and leaves the prior
     prefix byte-identical.
   - `TestCoincidentSignalMutationStillFailsClosed` — mutating only
     `volume` (which never feeds the `price_over_sma` signal path) still
     raises `ProcessedPrefixMutatedError`, proving D2 catches what the
     looser fills/signals prefix-replay check would miss.
5. Extended `tests/paper/test_persistence.py`'s
   `TestRestartProducesIdenticalStateAndNoDuplicateFills` with assertions
   that `candle_fingerprints.jsonl` exists after run 1, is byte-identical
   after the no-op run 2, and grows by exactly 5 lines after the
   append-extend run 3 (matching the existing fills/state assertions in
   that test).

## Deviation from plan (documented, not a rule-4 architectural change)

**`open_time_utc` mutation test methodology.** The plan's Task 2 action
text asks for a `TestMutatedFieldFailsClosed` parametrized over
`open, high, low, close, volume, quote_volume, open_time_utc` (7 fields),
implying all 7 are tested by mutating the dataset candle directly on a
second `run_paper_session` invocation. For the 6 OHLCV/volume fields this
works cleanly (contiguity / strictly-ascending-order checks never inspect
OHLCV values). For `open_time_utc` it is structurally impossible to
construct a schema-valid, still-resumable dataset with a single mutated
candle's `open_time_utc`:

- Shifting ONE candle's `open_time_utc` in isolation breaks
  `_dataset_shape_refusal`'s contiguity check (`InternalCandleGapError`) —
  an EARLIER, different refusal path.
- Compensating by shifting the entire tail (or the whole array) changes
  `paper_start_ts_utc` and/or the warmup slice hash, tripping
  `_resume_divergence_check`'s `ForwardDatasetDivergenceError` — also
  earlier and different.
- Swapping timestamps between two candles violates
  `CandleDataset`'s own strictly-ascending-by-position validator at
  construction time (a `pydantic.ValidationError`, before
  `run_paper_session` is ever reached).

This is correct defense-in-depth (D2 only needs to be the last line of
defense) rather than a gap. The `open_time_utc` case is instead exercised
via the realistic ALTERNATE threat the fingerprint sidecar itself is
designed to catch: `test_mutated_open_time_utc_in_sidecar_fails_closed`
directly corrupts the last recorded row's `open_time_utc` in
`candle_fingerprints.jsonl` (shifted by 1 microsecond so it matches no
candle in the unchanged dataset) and asserts the resume still raises
`ProcessedPrefixMutatedError` via
`_verify_processed_prefix_fingerprints`'s "missing from the current
dataset" branch — proving the sidecar's own recorded identity is
tamper-detected, which is the load-bearing property for this field.
Rule 4 does not apply here (no architectural change) — this is a test
construction choice fully within Task 2's stated area of executor
discretion for adversarial fixture design.

**Known, out-of-scope gap (not fixed, per explicit plan file-scope
boundary):** `src/bithumb_bot/cli/handlers/paper_run.py`'s `handler()`
does not catch the new `ProcessedPrefixMutatedError` in its
`except (...)` tuple, so a CLI invocation hitting this refusal would
propagate an uncaught traceback instead of a clean exit-1 message. The
plan's Task 2 `<files>` list and the plan's own success criteria
(`git diff --stat` shows changes ONLY under `src/bithumb_bot/paper/`,
`src/bithumb_bot/errors.py`, and `tests/paper/**`) explicitly exclude
`paper_run.py` from this task's scope, so this was documented rather than
fixed. Flagging for a follow-up quick task.

## Validation gate (T3) — verbatim results

**Full test suite:**
```
uv run pytest -q
829 passed, 1 skipped in 19.53s
```
(1 skip is the pre-existing Windows-symlink skip in
`tests/secrets/test_loader.py`; skip count unchanged.)

**mypy --strict:**
```
uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/errors.py src/bithumb_bot/cli/handlers/paper_run.py
Success: no issues found in 5 source files
```

**ruff check:**
```
uv run ruff check src/bithumb_bot/paper src/bithumb_bot/errors.py
Found 3 errors:
  N818   errors.py:202  CriticalCorruptionAlert should be named with an Error suffix
  PLR0912 runner.py:520 _run_forward_loop: too many branches (15 > 12)
  PLR0915 runner.py:520 _run_forward_loop: too many statements (52 > 50)
```
All 3 are PRE-EXISTING as of T1's commit `aad358f` (verified via
`git show aad358f:... | ruff check --isolated` — identical findings at
identical locations, before any T2 change). Zero NEW findings introduced
by T2. (`run_paper_session` itself briefly crossed the `PLR0915` threshold
during T2's wiring — 53, then 52 statements — and was brought back under
50 by extracting `_load_resume_context`/`_append_new_fingerprints`; see
"What was built" above.)

**Import-linter:**
```
uv run lint-imports
Core must not import broker KEPT
Paper must not import broker KEPT
Contracts: 2 kept, 0 broken.
```

**Grep gates (all required zeros):**
```
grep -c "TestWarmupExcludedFromForwardPnl" tests/paper/test_runner.py        -> 0
grep -c "run_backtest" tests/paper/test_parity_with_backtest.py             -> 0
grep -rc "from bithumb_bot.broker" src/bithumb_bot/paper/  (summed)         -> 0
grep -c "run_backtest" src/bithumb_bot/paper/runner.py                     -> 0
```

**State-file schema round-trip (manual smoke, ad-hoc script):**
```
schema_version: 2
paper_start_cash_krw: 20000000
paper_start_position_qty: 0
paper_start_realized_pnl_krw: 0
paper_start_cumulative_fees_krw: 0
fingerprint line count: 1  (== forward_candle_count: 1)
```

**`bt paper run --help`:** lists all five original flags (`--dataset`,
`--snapshot`, `--config`, `--state-dir`, `--out`) — unchanged.

**CLI end-to-end regression:** `tests/fixtures/paper/` does not exist in
this repo, so per the plan's fallback instruction an ad-hoc 1210-candle
KRW-BTC 4h dataset (1200 warmup + 10 forward) + the repo's existing
sanitized `observed_krw_btc.json` snapshot fixture + an engineering-smoke
research config were built under a project-relative temp directory
(`./_erv_smoke_tmp`, deleted after the check — never committed) and run
through the real `bt` executable twice:
```
Run 1: exit 0, resumed=False, forward_candle_count=10, new_fills_this_invocation=1
Run 2: exit 0, resumed=True,  forward_candle_count=10, new_fills_this_invocation=0
state.json byte-identical across the two runs: confirmed (diff -> no output)
```
(The pre-existing `tests/cli/test_handlers_paper_run.py` golden-path and
resumed-run tests, part of the 829-passed full-suite run above, already
cover this exact scenario end-to-end via `main()`; this ad-hoc script
additionally exercised the literal `bt` console-script entry point.)

**Git push discipline:**
```
git log --oneline -3
5aa45d3 fix(quick-260912-erv): D2 immutable-prefix verification on resume
aad358f fix(quick-260912-erv): D1 warmup isolation — fresh-portfolio forward loop
6700aae docs(260912-erv): pre-dispatch plan for paper runner defect fix
```
No `git push` was invoked at any point in this session. The repo's remote
(`origin`) has no `master` branch at all (its default branch is `main`;
`git log origin/master..HEAD` errors with "unknown revision" because that
ref doesn't exist) — this local `master` branch has never been pushed.
`git log origin/main..HEAD` confirms `5aa45d3`/`aad358f`/`6700aae` (this
task's commits) plus the prior `260912-cao` task's commits are all local-
only, ahead of `origin/main`.

## Self-Check

- `src/bithumb_bot/paper/runner.py` — FOUND, contains
  `_verify_processed_prefix_fingerprints`, `_load_resume_context`,
  `_append_new_fingerprints`.
- `src/bithumb_bot/paper/state.py` — FOUND, contains `candle_fingerprint`,
  `read_fingerprints`, `append_fingerprint`, all exported in `__all__`.
- `src/bithumb_bot/errors.py` — FOUND, contains `ProcessedPrefixMutatedError`,
  exported in `__all__`.
- `tests/paper/test_resume_prefix_integrity.py` — FOUND, 12 tests, all pass.
- `tests/paper/test_persistence.py` — extended, still 4 tests, all pass.
- Commit `5aa45d3` — FOUND in `git log --oneline`.
- Commit `aad358f` (T1, pre-existing) — FOUND in `git log --oneline`.

## Self-Check: PASSED
