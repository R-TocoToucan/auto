---
phase: quick-260912-erv
verified: 2026-09-12T00:00:00Z
status: human_needed
score: 7/7 must-haves verified
behavior_unverified: 0
overrides_applied: 0
human_verification:
  - test: "Decide whether to open a follow-up quick task so `src/bithumb_bot/cli/handlers/paper_run.py`'s `handler()` catches `ProcessedPrefixMutatedError` in its `except (...)` tuple (currently it will propagate as an uncaught traceback instead of a clean `bt paper run: refusal (...)`, exit 1)."
    expected: "A human decision: either accept the current CLI behavior (an uncaught exception on a mutated-prefix resume) as acceptable for this milestone stage, or schedule a follow-up quick task to add the one missing exception type to the tuple at paper_run.py lines 452-457."
    why_human: "This is a scope-boundary judgment call, not a code-correctness question. The plan's Task 2 `<files>` list and its own success criteria (`git diff --stat` shows changes ONLY under `src/bithumb_bot/paper/`, `src/bithumb_bot/errors.py`, and `tests/paper/**`) explicitly exclude `paper_run.py` from this task's scope, so leaving it unwired is plan-compliant, not a defect of this task. But it is a real, live gap in CLI-level error handling the human should consciously accept or schedule."
---

# Quick Task 260912-erv: Fix two correctness defects in the paper runner — Verification Report

**Task Goal:** Fix D1 (warmup isolation) and D2 (immutable-prefix verification) correctness defects in the paper-trading runner; replace defect-tolerant tests; add adversarial tests for both defects; run all validation gates; do not push.
**Verified:** 2026-09-12
**Status:** human_needed
**Commits under test:** `aad358f` (T1 — D1 warmup isolation), `5aa45d3` (T2 — D2 immutable-prefix verification)

## Goal Achievement

### Observable Truths (from PLAN.md `must_haves.truths`)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `paper_start_*` invariants (fresh portfolio at `paper_start_ts`) | ✓ VERIFIED | `PaperState` (state.py:82-85) carries `paper_start_cash_krw`/`paper_start_position_qty`/`paper_start_realized_pnl_krw`/`paper_start_cumulative_fees_krw` as required string fields, validated as Decimal via `_validate_decimal_string` (state.py:101-112). Populated in runner.py:879-882 (`paper_start_cash_krw=format(config.starting_cash_krw.value, "f")`, other three literal `"0"`). `tests/paper/test_warmup_isolation.py::TestPaperStartInvariants::test_all_flat_dataset_yields_clean_paper_start_fields` asserts all four fields plus `final_cash_krw == paper_start_cash_krw` and `final_position_qty == "0"` over an all-flat dataset. **PASSED** (run below). |
| 2 | First fill is always BUY; no leading SELL | ✓ VERIFIED | `tests/paper/test_warmup_isolation.py::TestFirstFillIsBuy` — two sub-tests: `test_forward_long_then_cash_writes_buy_then_sell_to_fills_jsonl` (fills[0].side=="buy", fills[1].side=="sell") and `test_warmup_inherited_long_never_writes_a_leading_sell` (warmup-only spike, forward CASH reversal — fills[0].side=="buy", never a bare leading sell). Both **PASSED**. |
| 3 | Swap-equivalent warmup produces identical forward events | ✓ VERIFIED | `tests/paper/test_warmup_isolation.py::TestWarmupIsolationSwapEquivalent::test_swap_equivalent_warmups_yield_identical_forward_ledger` — dataset A (warmup spike emitting a real LONG transition) vs dataset B (flat warmup, no signal) share the same forward candle; asserts `result_a.forward_entries == result_b.forward_entries` and matching `final_cash_krw`/`final_position_qty`. **PASSED**. |
| 4 | `ProcessedPrefixMutatedError` on any processed-candle mutation, naming the offending `open_time_utc` | ✓ VERIFIED | Exception class exists at `src/bithumb_bot/errors.py:444`, subclasses `BithumbBotError`, exported in `__all__`. `tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed` parametrizes over `open, high, low, close, volume, quote_volume` (6 cases, direct candle mutation) plus a 7th case (`test_mutated_open_time_utc_in_sidecar_fails_closed`, sidecar-corruption route for `open_time_utc` — documented deviation, see below) — 7 field-coverage total. `TestCoincidentSignalMutationStillFailsClosed` covers the coincident-signal case (mutating `volume`, which does not feed the SMA/signal path, still fails closed). `TestMissingProcessedCandleFailsClosed` covers the missing-candle case. All raise with `match=` on the offending ISO-8601 `open_time_utc`, and each test asserts on-disk `state.json`/`fills.jsonl`/`signals.jsonl` bytes are byte-identical before/after the failed resume (fail-closed, no partial write). All **PASSED**. |
| 5 | Append-only extension succeeds, cursor advances correctly | ✓ VERIFIED | `tests/paper/test_resume_prefix_integrity.py::TestAppendOnlyExtensionSucceeds::test_append_extend_with_unchanged_prefix_succeeds` — appends 3 new flat candles, asserts `resumed=True`, `forward_candle_count` grows by 3, `candle_fingerprints.jsonl` grows by exactly 3 lines with the prior prefix byte-identical, and `last_processed_open_utc` advances to the new last candle. Also covered redundantly by extended `test_persistence.py` (append-extend by 5). Both **PASSED**. |
| 6 | Pinned values (`hysteresis_bps=="75"`, `run_purpose=="engineering_smoke"`, `selection_eligible=false`, `holdout_eligible=false`) preserved | ✓ VERIFIED | Grep confirms `run_purpose="engineering_smoke"` / `selection_eligible=False` / `holdout_eligible=False` hardcoded in `runner.py:874-876` (state) and `paper_run.py:147-149` (report) / `paper_run.py:508-509` (stdout). `hysteresis_bps` computed from `config.strategy.hysteresis_bps` and pinned-checked against `_PINNED_HYSTERESIS_BPS = Decimal("75")` in `paper_run.py:416` (unchanged, pre-existing pin logic — not touched by this task, confirmed via non-modification diff below). |
| 7 | Import boundary preserved — zero `bithumb_bot.broker` references under `paper/` | ✓ VERIFIED | `grep -rc "from bithumb_bot.broker" src/bithumb_bot/paper/*.py` → 0 for every file. `uv run lint-imports` → `Paper must not import broker KEPT`, `Core must not import broker KEPT` (`Contracts: 2 kept, 0 broken.`). |

**Score:** 7/7 truths verified (0 present-behavior-unverified).

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/bithumb_bot/paper/runner.py` | D1 fresh-portfolio forward loop, D2 resume verification wired before fills/signals replay and before the forward loop | ✓ VERIFIED | `_run_forward_loop` (fresh `LedgerState`, iterates `WARMUP_CANDLE_COUNT..len(candles)`, calls unmodified `execute_intent`/`evaluate_protective_stop`). `_verify_processed_prefix_fingerprints` called from `_load_resume_context` (runner.py:663), which runs AFTER `_resume_divergence_check` and is itself called BEFORE `_buy_fee_preflight`/forward loop/`_assert_prefix_replay` (runner.py:783 vs. 853/861). `run_backtest` no longer imported/called (`grep -c "run_backtest" src/bithumb_bot/paper/runner.py` → 0; `BacktestResult` dataclass is still imported/reused as an output-shape container only). |
| `src/bithumb_bot/paper/state.py` | Extended `PaperState` with 4 `paper_start_*` fields, `schema_version=2`, `candle_fingerprint`/`read_fingerprints`/`append_fingerprint` helpers | ✓ VERIFIED | All present (state.py:82-112 for fields/validators, 198-242 for helpers), all exported in `__all__` (state.py:245-254). `load_state` refuses `schema_version != 2` with `ForwardDatasetDivergenceError` (state.py:146-151). |
| `src/bithumb_bot/errors.py` | New `ProcessedPrefixMutatedError` | ✓ VERIFIED | Present at line 444, subclasses `BithumbBotError`, in `__all__` at correct alphabetical position (between `ProhibitedCredentialDetectedError`/`ObsoleteVerificationBundleError` region — confirmed alphabetically placed among the surrounding classes). |
| `tests/paper/test_warmup_isolation.py` | 3 classes covering truths #1-#3 | ✓ VERIFIED | `TestWarmupIsolationSwapEquivalent`, `TestPaperStartInvariants`, `TestFirstFillIsBuy` (4 test methods total) — all pass. |
| `tests/paper/test_resume_prefix_integrity.py` | Adversarial tests for truths #4-#5 | ✓ VERIFIED | 6 classes / 12 test methods (known-answer vector, order-independence, 6 parametrized mutated-field cases + 1 sidecar-mutation case = 7 field-coverage, missing-candle, append-only-extension, coincident-signal) — all pass. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `run_paper_session` | D2 fingerprint verification | `_load_resume_context` → `_verify_processed_prefix_fingerprints` called before `_buy_fee_preflight`/forward loop/`_assert_prefix_replay` | ✓ WIRED | Confirmed by reading runner.py: line 783 (`_load_resume_context(...)`) executes before line 811 (`_run_forward_loop(...)`) and before lines 853/861 (`_assert_prefix_replay` on fills/signals). SUMMARY's "continuation note" claims this wiring was *missing* from a prior partial draft and had to be completed by this executor — code inspection confirms the wiring is now actually present and exercised (12/12 adversarial tests pass, including cases that would slip past the looser fills/signals replay check alone). |
| `run_paper_session` | fingerprint-first append sequencing | `_append_new_fingerprints` called before `append_jsonl` for fills/signals (runner.py:865-870) | ✓ WIRED | Fingerprints appended at line 865, fills/signals loop starts at line 867 — order confirmed correct per plan's crash-safety requirement. |
| `paper_run.py::handler` | `ProcessedPrefixMutatedError` | CLI `except (...)` tuple (paper_run.py:452-457) | ✗ NOT WIRED (documented, out-of-scope) | The except tuple lists `PaperStateDirError, ForwardDatasetDivergenceError, FillReplayDivergenceError, SidecarHashMismatchError` — `ProcessedPrefixMutatedError` is absent. Confirmed by direct read of paper_run.py. This is the executor-flagged gap; see Human Verification below. |

### Non-Modification Discipline

```
git diff --name-only 6700aae..HEAD -- src/bithumb_bot/strategy/ src/bithumb_bot/backtest/ src/bithumb_bot/execution/ src/bithumb_bot/broker/
(no output — zero files changed)

git diff --name-only 6700aae..HEAD -- src/bithumb_bot/cli/handlers/paper_run.py
(no output — zero files changed)
```

Both empty. Engine/execution/broker/strategy code and `paper_run.py` are untouched by this task, consistent with the plan's stated scope boundary.

### Grep Gates (plan-specified, all required zero)

```
grep -c "TestWarmupExcludedFromForwardPnl" tests/paper/test_runner.py        -> 0
grep -c "run_backtest" tests/paper/test_parity_with_backtest.py             -> 0
grep -rc "from bithumb_bot.broker" src/bithumb_bot/paper/*.py               -> 0 (all files)
grep -c "run_backtest" src/bithumb_bot/paper/runner.py                     -> 0
```

All four gates pass at zero, matching plan and SUMMARY claims.

### Push Safety

```
git log --oneline origin/main..HEAD
5aa45d3 fix(quick-260912-erv): D2 immutable-prefix verification on resume
aad358f fix(quick-260912-erv): D1 warmup isolation — fresh-portfolio forward loop
6700aae docs(260912-erv): pre-dispatch plan for paper runner defect fix
aae4313 docs(quick-260912-cao): bounded forward paper-trading runner
38d8a31 test(paper): add full test suite covering all 8 hard-requirement tests
74f81a1 feat(paper): add bt paper run CLI verb + canonical report writer
c26f7e5 feat(paper): add paper runner core (warmup/forward split, engine wrapper, persistence, resume)
59be2d9 docs(260912-cao): pre-dispatch plan for paper-trading runner
```

All commits local, ahead of `origin/main`. No `git push` was invoked. Confirmed independently by this verifier (not merely re-stating SUMMARY's claim).

### Behavioral Spot-Checks / Adversarial Test Run

```
uv run pytest tests/paper/test_warmup_isolation.py tests/paper/test_resume_prefix_integrity.py -v --no-header
============================= test session starts =============================
collecting ... collected 16 items

tests/paper/test_warmup_isolation.py::TestWarmupIsolationSwapEquivalent::test_swap_equivalent_warmups_yield_identical_forward_ledger PASSED
tests/paper/test_warmup_isolation.py::TestPaperStartInvariants::test_all_flat_dataset_yields_clean_paper_start_fields PASSED
tests/paper/test_warmup_isolation.py::TestFirstFillIsBuy::test_forward_long_then_cash_writes_buy_then_sell_to_fills_jsonl PASSED
tests/paper/test_warmup_isolation.py::TestFirstFillIsBuy::test_warmup_inherited_long_never_writes_a_leading_sell PASSED
tests/paper/test_resume_prefix_integrity.py::TestFingerprintKnownAnswerVector::test_fixed_candle_hashes_to_known_value PASSED
tests/paper/test_resume_prefix_integrity.py::TestFingerprintOrderIndependent::test_construction_kwarg_order_does_not_affect_fingerprint PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_ohlcv_field_fails_closed[open] PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_ohlcv_field_fails_closed[high] PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_ohlcv_field_fails_closed[low] PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_ohlcv_field_fails_closed[close] PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_ohlcv_field_fails_closed[volume] PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_ohlcv_field_fails_closed[quote_volume] PASSED
tests/paper/test_resume_prefix_integrity.py::TestMutatedFieldFailsClosed::test_mutated_open_time_utc_in_sidecar_fails_closed PASSED
tests/paper/test_resume_prefix_integrity.py::TestMissingProcessedCandleFailsClosed::test_dropped_processed_candle_fails_closed PASSED
tests/paper/test_resume_prefix_integrity.py::TestAppendOnlyExtensionSucceeds::test_append_extend_with_unchanged_prefix_succeeds PASSED
tests/paper/test_resume_prefix_integrity.py::TestCoincidentSignalMutationStillFailsClosed::test_volume_only_mutation_still_fails_closed PASSED

============================= 16 passed in 0.62s ==============================
```

### Regression Check

```
uv run pytest tests/paper/ -q
..........................                                               [100%]
26 passed in 1.85s

uv run pytest -q   (full suite, run once)
........................................................................ [  8%]
...
SKIPPED [1] tests\secrets\test_loader.py:83: Symlink creation on Windows requires developer mode / admin.
829 passed, 1 skipped in 18.92s
```

Matches SUMMARY's claimed "829 passed, 1 skipped" exactly; skip count unchanged (pre-existing Windows-symlink skip).

### Additional Gates Independently Re-Run

```
uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/errors.py src/bithumb_bot/cli/handlers/paper_run.py
Success: no issues found in 5 source files

uv run lint-imports
Core must not import broker KEPT
Paper must not import broker KEPT
Contracts: 2 kept, 0 broken.
```

### Anti-Patterns Found

None. Grep for `TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER` across all files modified/created by this task (`runner.py`, `state.py`, `errors.py`, `test_runner.py`, `test_parity_with_backtest.py`, `test_warmup_isolation.py`, `test_resume_prefix_integrity.py`, `test_persistence.py`) returned zero matches.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| SCOPE-5 | 260912-erv-PLAN.md | (paper-trading scope requirement carried forward from 260912-cao) | ✓ SATISFIED | Both correctness defects (D1, D2) fixed with adversarial test coverage; no regression in the 829-test full suite. |

### Documented Deviation (not a gap)

The plan's `TestMutatedFieldFailsClosed` asked for 7 parametrized cases including `open_time_utc` mutated directly on the dataset. The executor documented — and this verifier independently confirms via code reading — that a direct single-candle `open_time_utc` mutation is structurally unreachable at the D2 check: it trips either `_dataset_shape_refusal`'s `InternalCandleGapError` (contiguity) or `_resume_divergence_check`'s `ForwardDatasetDivergenceError` (warmup-hash/paper_start_ts drift) first — both earlier, correct defense-in-depth layers. The `open_time_utc` case is instead exercised via direct sidecar (`candle_fingerprints.jsonl`) corruption, which reaches the same `_verify_processed_prefix_fingerprints` "missing from the current dataset" code path. This is a sound test-construction substitution, not a missing test — accepted as-is, no override needed (it doesn't fail any must-have; the field is still covered, just via the realistic alternate threat vector).

### Human Verification Required

1. **Decide the fate of the paper_run.py CLI exception-tuple gap.**
   - **Test:** Review `src/bithumb_bot/cli/handlers/paper_run.py` lines 444-462; note `ProcessedPrefixMutatedError` is absent from the `except (...)` tuple.
   - **Expected:** A human decision — either (a) accept that a CLI invocation hitting a mutated-prefix resume today propagates an uncaught Python traceback instead of a clean `bt paper run: refusal (...)` message + exit 1, as acceptable for this milestone stage since `paper_run.py` is explicitly out of this task's scope per the plan's own success criteria; or (b) schedule a follow-up quick task to add the one missing exception type to the tuple.
   - **Why human:** This is a scope-and-priority judgment call (is an uncaught traceback on this one refusal path acceptable right now?), not a code-correctness defect within this task's declared boundary. The plan explicitly fenced `paper_run.py` out of Task 2's `<files>` list and success criteria (`git diff --stat` shows changes ONLY under `paper/`, `errors.py`, `tests/paper/**`), so leaving it unwired is plan-compliant — but it is a live, real gap the operator should consciously decide on.

### Gaps Summary

No blocking gaps. All 7 must-have truths verified with concrete evidence (not merely re-stated SUMMARY claims); all plan-specified grep gates return zero; non-modification discipline holds (zero lines changed outside `paper/`, `errors.py`, `tests/paper/**`); no push occurred; all 16 new adversarial tests pass; the full 829-test regression suite passes with the same skip count as before; mypy --strict and import-linter both clean. The single open item is an out-of-scope, plan-acknowledged CLI wiring gap (`ProcessedPrefixMutatedError` not caught by `paper_run.py`'s handler) that requires a human decision on follow-up scheduling rather than a code fix within this task.

---

*Verified: 2026-09-12*
*Verifier: Claude (gsd-verifier)*
