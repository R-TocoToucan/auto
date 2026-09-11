---
phase: quick-260912-cao
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/bithumb_bot/paper/__init__.py
  - src/bithumb_bot/paper/runner.py
  - src/bithumb_bot/paper/state.py
  - src/bithumb_bot/cli/handlers/paper_run.py
  - src/bithumb_bot/cli/dispatcher.py
  - tests/paper/__init__.py
  - tests/paper/test_runner.py
  - tests/paper/test_persistence.py
  - tests/paper/test_parity_with_backtest.py
  - tests/cli/test_handlers_paper_run.py
  - tests/import_boundary/test_paper_no_broker.py
autonomous: true
requirements: [SCOPE-5]
---

<objective>
Implement a bounded forward paper-trading runner for the frozen `price_over_sma` baseline (hysteresis_bps=75) that consumes a pre-fetched immutable candle dataset, splits it into a 1200-candle warm-up head and a forward-tradable tail, drives the existing `run_backtest` engine over the combined series, and persists a restart-safe audit trail plus a canonical JSON report — with **zero** new logic for fees, slippage, rounding, stops, lockout, notional cap, or readiness.

Purpose: Deliver §5 of `docs/IMPLEMENTATION_SCOPE.md` ("Paper operation") — a hypothetical-order runner that shares the simulator's accounting path, never loads trade credentials, never reaches broker code, and produces byte-deterministic replays.

Output: `bt paper run --dataset … --snapshot … --config … --state-dir … --out …` produces (a) a canonical JSON+sidecar report with `run_purpose="engineering_smoke"`, `selection_eligible=false`, `holdout_eligible=false`, `hysteresis_bps="75"`, and (b) an append-only audit trail under `--state-dir` that a second invocation of the same command can resume from without duplicating signals or fills.
</objective>

<context>
@docs/IMPLEMENTATION_SCOPE.md
@.claude/CLAUDE.md
@src/bithumb_bot/backtest/runner.py
@src/bithumb_bot/backtest/config.py
@src/bithumb_bot/execution/engine.py
@src/bithumb_bot/execution/stop.py
@src/bithumb_bot/execution/ledger.py
@src/bithumb_bot/execution/readiness.py
@src/bithumb_bot/strategy/config.py
@src/bithumb_bot/strategy/baseline.py
@src/bithumb_bot/market_data/dataset.py
@src/bithumb_bot/market_data/candles.py
@src/bithumb_bot/config/research_config.py
@src/bithumb_bot/config/validator.py
@src/bithumb_bot/cli/handlers/research_backtest.py
@src/bithumb_bot/cli/dispatcher.py
@src/bithumb_bot/artifact/canonical.py
</context>

<design_notes>

## Reuse strategy (contract that "reuse" is not aspirational)

The paper runner is a thin wrapper around the existing chronological backtest — it does NOT re-implement any accounting logic. Concretely:

- **Engine call:** `bithumb_bot.backtest.runner.run_backtest(dataset, snapshot, config)` is invoked once per run over a `CandleDataset` whose `candles` list is `warmup_head + forward_tail`. Because `run_backtest` is pure and deterministic, running it over the same combined slice on restart yields byte-identical `entries` — restart-safety is *inherited*, not re-invented.
- **Fee / slippage / rounding / min-order / notional cap:** already inside `execute_intent` (called by `run_backtest`). Not touched.
- **Protective stop + stopped-out lockout:** already inside `evaluate_protective_stop` and the main loop of `run_backtest`. Not touched.
- **Readiness / provisional-fee gating:** delegated to `execution.readiness.check_execution_readiness`, identical to `research_backtest.py`.
- **Config loader:** `bithumb_bot.config.research_config.load_research_config` is reused unchanged. Same TOML shape; the paper handler adds no new keys.
- **Strategy / signals / hysteresis:** `bithumb_bot.strategy.generate_signals` runs inside `run_backtest`; the paper runner supplies the same `BaselineStrategyConfig` (`hysteresis_bps=Decimal("75")` provided via config, no new code path).
- **Validation of gaps / duplicates / bad UTC:** already fail-closed inside `run_backtest`'s pre-flight (checks contiguity, tz-aware UTC, `missing_intervals_utc` inside range). The paper runner adds ONE additional check the backtest cannot make: refuse if the dataset's final candle's *close boundary* is in the future relative to `now_utc` (see T1 for the exact rule) — this is the "incomplete candle" rule.

## Warm-up vs forward split

- The dataset MUST contain **at least 1200 + 1 candles**. The first 1200 candles are the SMA seed (indicator-only, MUST NOT be counted as forward performance). `paper_start_ts` = `dataset.candles[1200].open_time_utc` (open of the 1201st candle — the first candle where a signal generated at the close of a fully-seeded lookback window can be acted on). Written to `state.json` on the very first invocation and never changed thereafter.
- The strategy's own `warmup_candles=1200` already ensures signals for `candles[i]` require `i >= 1199`; the paper runner therefore filters `run_backtest`'s ledger entries and equity points to those whose driving candle's `open_time_utc >= paper_start_ts` when reporting "forward" performance. Warm-up-window entries (should be none by construction, given the strategy contract) are also excluded defensively.

## Persistence file layout (frozen contract — test #4 verifies this exact shape)

Under `--state-dir` (created if absent; refuse if it exists as a non-directory):

```
<state-dir>/
  state.json          # single-file ledger + cursor snapshot; atomic replace
  state.json.sha256   # sidecar written via artifact.canonical.write_with_sidecar
  signals.jsonl       # append-only; one JSON object per line, one line per
                      #   transition signal ≥ paper_start_ts
  fills.jsonl         # append-only; one JSON object per line, one line per
                      #   LedgerEntry whose driving candle open_time ≥ paper_start_ts
```

`state.json` canonical shape (sorted keys, string-typed Decimals, ISO-8601 UTC):

```json
{
  "schema_version": 1,
  "run_purpose": "engineering_smoke",
  "selection_eligible": false,
  "holdout_eligible": false,
  "hysteresis_bps": "75",
  "paper_start_ts_utc": "<ISO-8601 UTC>",
  "warmup_first_open_utc": "<ISO-8601 UTC>",
  "warmup_last_open_utc": "<ISO-8601 UTC>",
  "warmup_candles": 1200,
  "warmup_sha256": "<sha256 of canonical JSON of warmup candles>",
  "config_sha256": "<sha256 of config file bytes>",
  "snapshot_sha256": "<sha256 of snapshot file bytes>",
  "dataset_market": "KRW-BTC",
  "unit_minutes": 240,
  "last_processed_open_utc": "<ISO-8601 UTC or null>",
  "forward_candle_count": <int>,
  "forward_signal_count": <int>,
  "forward_fill_count": <int>,
  "final_cash_krw": "<Decimal string>",
  "final_position_qty": "<Decimal string>"
}
```

Restart-safety contract (test #4):

1. On startup, if `state.json` exists, load it and verify:
   - `warmup_sha256` matches the current dataset's warmup slice → else refuse (`ForwardDatasetDivergenceError`).
   - `config_sha256` and `snapshot_sha256` match current inputs → else refuse.
   - `paper_start_ts_utc` matches `dataset.candles[1200].open_time_utc` → else refuse.
2. Replay: run `run_backtest` over `warmup + forward_slice_up_to_last_processed` and confirm the produced `entries` matches `fills.jsonl` byte-for-byte after canonical serialization. If drift → refuse (`FillReplayDivergenceError`) — never rewrite the audit trail.
3. Advance: run `run_backtest` over `warmup + forward_slice_through_new_last_candle`, take the *new* entries beyond the previously recorded set, and *append* them to `fills.jsonl`. Update `state.json` (atomic replace via `write_with_sidecar`).

Because the source-of-truth cursor is `last_processed_open_utc` and `run_backtest` is deterministic, a second invocation over the same dataset writes zero new lines to either JSONL.

## CLI verb (mirrors `bt research backtest`)

```
bt paper run \
    --dataset  path/to/dataset.json \
    --snapshot path/to/snapshot.json \
    --config   path/to/config.toml \
    --state-dir path/to/state/ \
    --out      path/to/report.json
```

`--config` is the *same* TOML shape `bt research backtest` consumes (no new keys). `--out` and `--state-dir` are distinct: `--out` is a one-shot canonical report (like research backtest); `--state-dir` is the durable audit-trail root.

## Import boundary (test #7)

Package `bithumb_bot.paper` MUST NOT import `bithumb_bot.broker` (top-level or transitively through its own modules). Test #7 is an `import_linter`-style AST scan analogous to `tests/import_boundary/test_import_linter_contract.py`, plus a runtime `sys.modules` inspection after importing `bithumb_bot.paper.runner` in a fresh subprocess.

</design_notes>

<tasks>

<task type="auto">
  <name>Task 1: paper runner core (warmup/forward split, engine wrapper, persistence, resume)</name>
  <files>src/bithumb_bot/paper/__init__.py, src/bithumb_bot/paper/state.py, src/bithumb_bot/paper/runner.py, src/bithumb_bot/errors.py</files>
  <action>
    Create the `bithumb_bot.paper` package. MUST NOT modify existing backtest engine, strategy, or hysteresis logic — this task adds a thin wrapper only.

    `paper/__init__.py`: re-export `run_paper_session`, `PaperSessionResult`, `PaperState`.

    `paper/state.py`: define
    - `PaperState` (frozen `pydantic.BaseModel`, `extra="forbid"`, `strict=True`) with exactly the JSON shape sketched in the plan's design_notes. `hysteresis_bps` is a string field (round-trips exactly, matches D-49 discipline — no Decimal→float trap). Decimal-string fields are validated as string form of a `Decimal`.
    - `load_state(state_dir: Path) -> PaperState | None` — reads `state.json` via `write_with_sidecar`'s companion loader (re-hashes bytes vs sidecar via `sha256_hex`; on any mismatch raise `SidecarHashMismatchError`).
    - `save_state(state_dir: Path, state: PaperState) -> None` — canonical-JSON serialize via `bithumb_bot.artifact.canonical.canonical_bytes`, atomic replace via `write_with_sidecar` (which already does tmp+rename).
    - `append_jsonl(path: Path, obj: dict) -> None` — open `"a"` in binary, write `canonical_bytes(obj) + b"\n"`, `fsync`. Refuse to write if the file has a non-directory parent problem; create parent dir with `mkdir(parents=True, exist_ok=True)`.
    - `read_jsonl(path: Path) -> list[dict]` — line-by-line, ignore trailing empty line; raise on any malformed line.

    `errors.py`: add three new exceptions (subclasses of the existing `BithumbBotError` hierarchy — check `errors.py` for the base) — `PaperStateDirError`, `ForwardDatasetDivergenceError`, `FillReplayDivergenceError`. Each carries the human-readable reason as its sole message; no credential-carrying fields.

    `paper/runner.py`: pure library entry point `run_paper_session(dataset: CandleDataset, snapshot: SnapshotV1, config: BacktestConfig, state_dir: Path, *, now_utc: datetime) -> PaperSessionResult`.

    `PaperSessionResult` is a frozen dataclass mirroring `BacktestResult`'s shape plus paper-specific fields: `paper_start_ts_utc`, `warmup_candle_count` (always 1200), `forward_candle_count`, `forward_entries: tuple[LedgerEntry, ...]` (subset of the underlying backtest's `entries` whose driving candle open_time ≥ `paper_start_ts_utc`), `forward_signal_count: int`, `new_fills_this_invocation: int`, `resumed: bool`, `invalid_reason: str | None`, `refusal_code: str | None`.

    Algorithm (fail-closed at every step):

    1. Validate `state_dir`: exists-or-mkdir; refuse if it's a file (`PaperStateDirError`). NEVER read any environment variable that might carry a credential — the whole runner runs with zero credentials.
    2. Validate the dataset preconditions the backtest engine will not catch upfront:
       - `len(dataset.candles) >= 1201` (else refuse: not enough for 1200 warmup + ≥1 forward).
       - Every candle's `open_time_utc` is tz-aware UTC (defence-in-depth — `run_backtest` also checks).
       - Each candle's *close boundary* `open_time_utc + unit_minutes` MUST be `<= now_utc`. Any candle whose close boundary is in the future is an "incomplete" candle and MUST cause refusal — this is the incomplete-candle rule required by the spec. Emit `refusal_code="IncompleteCandleError"`.
       - Contiguity, duplicates, and tz-aware UTC are ALSO enforced by `run_backtest`'s own pre-flight (`InternalCandleGapError`, ascending-strict validator in `CandleDataset`); this task must NOT re-implement those checks — surface `run_backtest`'s refusal codes verbatim.
    3. Compute `paper_start_ts_utc = dataset.candles[1200].open_time_utc`.
    4. Compute `warmup_sha256 = sha256_hex(canonical_bytes([c.model_dump(mode="json") for c in dataset.candles[:1200]]))`.
    5. If `state.json` exists in `state_dir`: load it, then refuse (with the appropriate exception) if any of: `warmup_sha256`, `paper_start_ts_utc`, `dataset_market`, `unit_minutes`, `hysteresis_bps` differs from the current invocation. `resumed = True`.
       - Also perform the *fill-replay* check: read `fills.jsonl`, replay `run_backtest` over `dataset.candles[:1200 + prior_forward_candle_count]`, canonically serialize the resulting `entries` filtered to `open_time_utc >= paper_start_ts_utc`, and assert byte-equality with the on-disk `fills.jsonl`. Any drift → raise `FillReplayDivergenceError` — MUST NOT overwrite the audit trail.
    6. Run `run_backtest(dataset, snapshot, config)` **exactly once** for this invocation (over the full warmup+forward slice — `run_backtest` is deterministic, this is the reuse contract). If it returns an `invalid_reason`, propagate it into `PaperSessionResult` and short-circuit before writing any JSONL lines.
    7. Filter `entries` to those with driving candle `open_time_utc >= paper_start_ts_utc` → `forward_entries`. (Ledger entries carry their `signal_ts_utc`/`source_open_time_utc` — see `LedgerEntry` in `execution/ledger.py`; use the source open time boundary that best matches "the candle whose close produced the intent." Comment which field you chose and why.)
    8. Compute `new_entries = forward_entries[prior_forward_fill_count:]` — because the engine is deterministic and the input is append-only, the prior N entries are guaranteed prefix-equal (verified in step 5).
    9. For each new entry: append one line to `fills.jsonl` using the same canonical serializer `research_backtest.py`'s `_serialize_ledger_entry` uses (import it or duplicate the small helper — do NOT invent a third format).
    10. Precompute the current invocation's signals via `bithumb_bot.strategy.generate_signals(dataset.candles, config.strategy)`; append any signal whose `source_open_time_utc >= paper_start_ts_utc` AND is beyond the prior `forward_signal_count` to `signals.jsonl` (each line: `{"source_open_time_utc": ..., "signal_ts_utc": ..., "target_state": "LONG"|"CASH"}`).
    11. Build the new `PaperState`, save via `save_state` (atomic replace + sidecar).
    12. Return `PaperSessionResult` with counts and the underlying `BacktestResult`'s pending intent / active stop / lockout state.

    Explicit prohibitions (each will be tested):
    - MUST NOT import `bithumb_bot.broker` anywhere in the `paper` package (test #7).
    - MUST NOT read `os.environ` for any key matching credential prefixes (`BITHUMB_TRADE_*`, `BITHUMB_JWT_*`, etc.). The runner runs with zero credentials.
    - MUST NOT modify existing backtest engine, strategy, or hysteresis logic.
    - MUST NOT fabricate candles for reported gaps or duplicates — surface `run_backtest`'s existing refusal.
  </action>
  <verify>
    <automated>uv run pytest tests/paper/test_runner.py tests/paper/test_persistence.py -x -q</automated>
  </verify>
  <done>
    `paper/runner.py`, `paper/state.py`, `paper/__init__.py` exist. `run_paper_session` returns a `PaperSessionResult` with correct warmup/forward split; second invocation over the same dataset is a no-op (writes zero new lines to both JSONL files); mid-run dataset extension appends only new fills. `import bithumb_bot.paper` succeeds with no credential env vars set and never imports `bithumb_bot.broker`.
  </done>
</task>

<task type="auto">
  <name>Task 2: CLI verb `bt paper run` + canonical report writer</name>
  <files>src/bithumb_bot/cli/handlers/paper_run.py, src/bithumb_bot/cli/dispatcher.py, src/bithumb_bot/config/capability_registry.py</files>
  <action>
    MUST NOT modify existing backtest engine, strategy, or hysteresis logic.

    `src/bithumb_bot/cli/handlers/paper_run.py`:
    - Follow the exact structure of `research_backtest.py` — same lazy imports, same validate-before-do sequence, same overwrite guard, same canonical report + sidecar writer.
    - Handler signature: `def handler(args: argparse.Namespace) -> int`. Reads `args.dataset`, `args.snapshot`, `args.config`, `args.state_dir`, `args.out`.
    - Reuse `load_research_config`, `load_dataset`, `load_snapshot`, `load_gate1` UNCHANGED. Reuse `check_execution_readiness` with `readiness.research_simulation_readiness == "ready"` gate — same refusal path as research backtest.
    - Reuse the engineering-smoke Gate-1 provisional-cap enforcement block from `research_backtest.py` verbatim (copy the ~40 lines; do NOT abstract into a shared helper in this task — that's premature abstraction under ponytail mode, and diverging safety code paths is safer than a shared helper with a subtle bug affecting both handlers). Add a code comment explaining the deliberate duplication.
    - After all pre-flight passes, call `run_paper_session(dataset, snapshot, backtest_config, state_dir=Path(args.state_dir), now_utc=utc_now())`.
    - Build a canonical JSON report analogous to `research_backtest.py`'s `_build_report`, with these paper-specific additions/overrides in the top-level object:
        - `"run_purpose": "engineering_smoke"` (identical to research)
        - `"selection_eligible": false`, `"holdout_eligible": false` (identical)
        - `"mode": "paper"`
        - `"paper": { "paper_start_ts_utc", "warmup_candle_count": 1200, "forward_candle_count", "forward_signal_count", "forward_fill_count", "new_fills_this_invocation", "resumed", "state_dir_sha256_of_state_json" }`
        - `"strategy.hysteresis_bps": "75"` (as a canonical Decimal string via `_decimal_str`, and re-asserted equal to `"75"` before write — if not, refuse: the paper handler is bound to hysteresis 75).
        - Ledger/equity: only the *forward* subset (drop warmup entries — should be empty already, but filter defensively).
    - Write via `write_with_sidecar(out_path, canonical_bytes(report))` — same guard-against-overwrite as research backtest applied to `--out` only (state-dir lives its own append-only life).
    - Human-readable stdout summary mirrors research backtest's key/value stream.
    - Refusal-code translation: `IncompleteCandleError`, `ForwardDatasetDivergenceError`, `FillReplayDivergenceError`, `PaperStateDirError`, `SidecarHashMismatchError` — each caught and rendered as `bt paper run: refusal ({code}): {message}` on stderr, exit 1. Any unlisted exception propagates (matches dispatcher convention).

    `src/bithumb_bot/cli/dispatcher.py`:
    - Add `("paper", "run")` to `HANDLER_MAP`, bound to `_make_lazy_handler("bithumb_bot.cli.handlers.paper_run", "handler")`.
    - Add a `paper` verb subparser modeled on the `research` verb block. Sub-subparser `run` accepts `--dataset`, `--snapshot`, `--config`, `--state-dir`, `--out` — all `required=True`. Help text: "Bounded forward paper-trading runner — consumes public dataset, writes restart-safe audit trail + canonical report. Never loads trade credentials or calls real-order endpoints."

    `src/bithumb_bot/config/capability_registry.py`:
    - Register `("paper", "run")` as a first-class Phase-1 capability so `validate(("paper", "run"))` in the dispatcher pre-flight returns `ok=True` for a properly configured project. Reuse the exact prerequisite list of `("research", "backtest")` (same gate-1 + spec-snapshot + config paths) MINUS any trade-credential requirement — the paper runner MUST run with zero credentials. If the existing registry has helper predicates for "no trade credential required," use them; otherwise inspect how `("research", "backtest")` is wired and mirror it.

    Refuse to modify any other dispatcher wiring (`_RESERVED_TREE`, existing subparsers).
  </action>
  <verify>
    <automated>uv run pytest tests/cli/test_handlers_paper_run.py tests/cli/test_dispatcher.py -x -q</automated>
  </verify>
  <done>
    `bt paper run --help` prints usage naming all five flags. `bt paper run` with valid dataset/snapshot/config/state-dir/out writes a canonical JSON report + `.sha256` sidecar to `--out`, and `state.json` + `state.json.sha256` + `signals.jsonl` + `fills.jsonl` under `--state-dir`. Report top-level contains `"run_purpose": "engineering_smoke"`, `"selection_eligible": false`, `"holdout_eligible": false`, and `strategy.hysteresis_bps == "75"`. Refusal paths (missing file, hash mismatch, incomplete candle, gap, duplicate, drifted state) each exit 1 with a stderr message naming the refusal code. Zero credentials required to run.
  </done>
</task>

<task type="auto">
  <name>Task 3: full test suite covering all 8 hard-requirement tests</name>
  <files>tests/paper/__init__.py, tests/paper/test_runner.py, tests/paper/test_persistence.py, tests/paper/test_parity_with_backtest.py, tests/cli/test_handlers_paper_run.py, tests/import_boundary/test_paper_no_broker.py</files>
  <action>
    MUST NOT modify existing backtest engine, strategy, or hysteresis logic — tests only.

    Follow existing test conventions: pytest, hypothesis where property-testing pays (see `tests/backtest/test_runner.py` and `tests/execution/*` for style). Prefer small hand-built `CandleDataset` fixtures with a synthetic price series just long enough to trigger the 1200-candle warmup (e.g. build 1210 candles: 1200 flat + 10 with a rising leg that triggers a LONG transition). Share a `conftest.py` fixture that returns `(dataset, snapshot, config)` where `hysteresis_bps=Decimal("75")` and `starting_cash_krw` is small enough to stay within `provisional_engineering_notional_krw`. Freeze `now_utc` in every test to a value AFTER all fixture candles have closed, unless a test specifically probes the incomplete-candle rule.

    Tests (one per hard-requirement item; group by file as noted):

    **`tests/paper/test_runner.py`:**

    1. `test_warmup_excluded_from_forward_pnl` — Build a dataset whose warmup slice deliberately contains a would-be BUY-then-SELL round trip (e.g. by seeding a price pattern that, if the strategy were live during warmup, would emit signals). Assert `PaperSessionResult.forward_entries` contains zero entries whose `source_open_time_utc < paper_start_ts_utc`, and `forward_signal_count` matches the count of signals whose `source_open_time_utc >= paper_start_ts_utc`.

    2. `test_signal_uses_only_completed_candles` — Set `now_utc` so that the dataset's final candle's close boundary is *in the future*. Assert `run_paper_session` returns `refusal_code="IncompleteCandleError"` and writes nothing to `state-dir`.

    3. `test_fill_occurs_on_next_candle` — Construct a two-transition dataset (one LONG at candle `t`, one CASH at candle `t+k`). Assert each ledger entry's `source_open_time_utc == t`-open but the entry's `fill_candle_open_time_utc` equals `t+1`-open (or the first available later candle if a gap was reported). Assert no entry has `fill_candle_open_time_utc == source_open_time_utc` (structural no-same-candle invariant).

    **`tests/paper/test_persistence.py`:**

    4. `test_restart_produces_identical_state_and_no_duplicate_fills` — Run once, snapshot `state.json.sha256` + `fills.jsonl` byte hash + `signals.jsonl` byte hash. Run again over the same dataset. Assert:
       - Second run returns `resumed=True`, `new_fills_this_invocation == 0`.
       - `state.json` file bytes are identical (canonical serialization + atomic replace guarantees this — if not, the test surfaces a nondeterminism bug in the writer).
       - `fills.jsonl` byte hash unchanged.
       - `signals.jsonl` byte hash unchanged.
       Then extend the dataset by 5 real candles (a new object with the same warmup slice + a 5-candle tail extension), run a third time. Assert `resumed=True`, `state.json` `forward_candle_count` grew by 5, and any new fills were *appended* (existing lines byte-equal to the previous `fills.jsonl` prefix).

    5. `test_dataset_faults_fail_closed` — Three sub-cases, one per fault:
       - **Gap**: build a dataset with a missing 240-minute slot in the middle → assert `refusal_code="InternalCandleGapError"` (surfaced from `run_backtest`), zero JSONL writes.
       - **Duplicate**: attempt to build a dataset with two identical `open_time_utc` candles → assert `CandleDataset` construction raises via its ascending-strict validator (this is caught upstream at `load_dataset` and re-raised by the paper runner).
       - **Incomplete**: covered by test #2 above; add a cross-reference assertion here that state-dir remains empty of `state.json` after each fault.

    **`tests/paper/test_parity_with_backtest.py`:**

    6. `test_parity_with_backtest_engine_deterministic` — Given the *same* fixture `(dataset, snapshot, config)`, run:
       - `expected = run_backtest(dataset, snapshot, config)`
       - `actual = run_paper_session(dataset, snapshot, config, state_dir=tmp_path, now_utc=<after-all-candles>)`
       Assert every LedgerEntry in `actual.forward_entries` is byte-identical to the corresponding entry in `expected.entries` (filter `expected.entries` to `source_open_time_utc >= paper_start_ts_utc`). Assert `actual` final cash / position / pending-intent / stop / lockout match `expected`'s. Add a `hypothesis` property test that draws random small candle series (length 1201–1210, prices in a bounded range) and asserts the same parity property — this is the property-based demonstration that reuse (not divergent reimplementation) is real.

    **`tests/cli/test_handlers_paper_run.py`:**

    7. Test the CLI end-to-end via `dispatch(["paper", "run", "--dataset", ..., ...])`:
       - Golden-run test: report JSON parses, contains `"run_purpose": "engineering_smoke"`, `"selection_eligible": false`, `"holdout_eligible": false`, and — **hard-requirement test #8** — `strategy.hysteresis_bps == "75"` (string form, byte-equal). Assert the `state.json` on disk also carries `"hysteresis_bps": "75"`.
       - Overwrite-guard test: pre-create `--out`, assert exit 1.
       - Missing-argument tests for each of the 5 flags.
       - No-credential test: unset every env var starting with `BITHUMB_` in a monkeypatched environment, assert the golden run still exits 0.

    **`tests/import_boundary/test_paper_no_broker.py`** — hard-requirement test #7:
       - AST scan of every `.py` under `src/bithumb_bot/paper/` asserting no `import bithumb_bot.broker` / `from bithumb_bot.broker` line exists (also check for aliased imports of any submodule of `broker`).
       - Subprocess test (`sys.executable -c "import bithumb_bot.paper.runner; import sys; assert not any(m.startswith('bithumb_bot.broker') for m in sys.modules)"`) asserting no broker module lands in `sys.modules` after importing the paper runner.
       - Extend `pyproject.toml`'s `[tool.importlinter]` contracts (if the repo's existing contract permits paper-layer forbidden-imports) — but do NOT modify the existing "Core must not import broker" contract; add a new sibling contract "Paper must not import broker." Cross-reference this contract by name in the test's assertion message.
  </action>
  <verify>
    <automated>uv run pytest tests/paper tests/cli/test_handlers_paper_run.py tests/import_boundary/test_paper_no_broker.py -x -q</automated>
  </verify>
  <done>
    All 8 hard-requirement tests exist and pass. Test #6's parity assertion runs both `run_backtest` and `run_paper_session` on the same fixture and asserts byte-identical forward entries (this is the operational contract that "reuse" is real, not aspirational). Test #7's import-boundary contract fails the build if any future change introduces a `broker` import into the paper layer. Test #8 pins the exact string `"75"` in both the on-disk report and `state.json`.
  </done>
</task>

</tasks>

<verification>

Overall phase checks (run after all three tasks):

1. `uv run ruff check src/bithumb_bot/paper src/bithumb_bot/cli/handlers/paper_run.py tests/paper` — clean.
2. `uv run mypy --strict src/bithumb_bot/paper src/bithumb_bot/cli/handlers/paper_run.py` — clean.
3. `uv run pytest -x -q` — full suite green (including all existing tests, unchanged).
4. `uv run bt paper run --help` — prints usage naming all five flags.
5. Full smoke run: `uv run bt paper run --dataset tests/fixtures/paper/synthetic_dataset.json --snapshot tests/fixtures/paper/snapshot.json --config tests/fixtures/paper/config.toml --state-dir /tmp/paper_state --out /tmp/paper_report.json && uv run bt paper run --dataset ... (same args) ...` — first run exits 0 and writes state; second run exits 0 with `new_fills_this_invocation=0` and `state.json` byte-identical.
6. `grep -rn "from bithumb_bot.broker\|import bithumb_bot.broker" src/bithumb_bot/paper/` — zero matches.

</verification>

<success_criteria>

- Paper runner exists at `src/bithumb_bot/paper/` and is invocable via `bt paper run …`.
- Every one of the 8 hard-requirement tests exists and passes (warmup-exclusion, completed-candle-only, next-candle fill, restart-safe/no-duplicate, fail-closed on gap/dup/incomplete, parity with backtest engine, no-broker import boundary, persisted `hysteresis_bps="75"`).
- Zero new dependencies added to `pyproject.toml` (stdlib `json`/`pathlib` + existing `pydantic`/`pyarrow`/`structlog` surface only).
- Zero modifications to `execute_intent`, `evaluate_protective_stop`, `run_backtest`, `generate_signals`, `BaselineStrategyConfig`, or any protective-stop / lockout / notional-cap code — verified by `git diff --stat` touching only new files under `paper/` + the dispatcher/registry wiring + tests.
- Runner runs with **zero** credentials in the environment (unit-tested).
- Report `run_purpose="engineering_smoke"`, `selection_eligible=false`, `holdout_eligible=false` — same three-way engineering-smoke identity as `bt research backtest`.

</success_criteria>

<output>
No SUMMARY required for quick-mode plans; the plan file itself is the deliverable.
</output>
