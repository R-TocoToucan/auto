---
phase: 01-safety-foundation-bithumb-spec-adapter
plan: 03
subsystem: cli-and-boundary
tags: [import-linter, pre-commit, argparse, bt-cli, dispatcher, defense-in-depth, reserved-verbs, d-85, d-86, d-87, d-90, d-71, d-73]

requires:
  - "01-01: pyproject.toml [tool.importlinter] root, REGISTRY, validate(), placeholder import-linter pre-commit id, bithumb_bot package tree"
  - "01-02: reject_trade_credentials wired into validate(), bithumb_bot.core.money docstring for m0 selfcheck's risk-denominator print"
provides:
  - "Live Import Linter forbidden contract `core/ ↛ broker/` in pyproject.toml with `exclude_type_checking_imports = false` (D-71)"
  - "Committed negative-fixture package `tests/fixtures/import_linter_violation/badpkg/` proving the contract mechanically fails on a real violation (D-73)"
  - "Positive + negative regression runner test in `tests/import_boundary/test_import_linter_contract.py`"
  - "Functional `import-linter` pre-commit hook (D-71 / D-73); placeholder `entry: true` replaced with `entry: lint-imports`"
  - "`bt` console script bound to `bithumb_bot.cli.main:main` — one script, no per-verb entry points (D-85)"
  - "argparse two-level subparser dispatcher (`bithumb_bot.cli.dispatcher`) with validate-before-dispatch, defense-in-depth, and lazy handler resolution (D-85, D-89)"
  - "Functional `bt config validate --through gate1` handler (D-86, D-89): inspects gate1.toml, prints structured summary, never marks approved, never mutates file"
  - "Functional `bt m0 selfcheck` handler (SAFE-02 surface per D-99, D-86): prints Gate-1 decisions + resolved key class + 5 risk denominators; refuses on trade credential class (D-70 class-only)"
  - "Shared `reserved_handler` (D-87 / D-90) bound to all 9 D-87 reserved verbs — refuses uniformly without scaffolding functional behavior"
  - "`m1_stubs` module (`m1_fetch_spec_stub`, `m1_verify_facts_stub`, `m1_verify_snapshot_stub`) — defense-in-depth stubs replaced by plan 01-04"
  - "Documented single CI static-gate command: `uv run lint-imports && uv run python -m tools.decimal_ast_check <filtered>` (D-73)"
affects: [01-04, 02, 05]

tech-stack:
  added: []
  patterns:
    - "TDD RED->GREEN commit cadence across 5 code tasks (01-03-05 through 01-03-09); RED test commit precedes minimal GREEN implementation"
    - "Lazy-resolved handler map — `HANDLER_MAP` uses importlib-based lazy callables so the dispatcher lands before the individual handler modules exist; each callable resolves + invokes on first dispatch"
    - "Defense-in-depth (D-85) — every handler function's FIRST STATEMENT is `validate((verb,subverb))`. Direct Python callers who bypass the CLI (`from bithumb_bot.cli.handlers.m0_selfcheck import handler`) still trip the capability check"
    - "Argparse-native --help/--version — special-cased BEFORE any registry lookup, credential load, or `BithumbSecrets` construction (T-1-03-04)"
    - "Named-error translation in dispatcher — 5 SAFE-01/02/03 exceptions (`UnknownCapabilityError`, `Gate1LoadError`, `ProhibitedCredentialDetectedError`, `SecretsFileInsideRepoError`, `AmbiguousSecretsConfigurationError`) become clean stderr messages + exit 1, never Python tracebacks"
    - "Reserved-verb refusal via return code, not `NotImplementedError` — the D-90 discipline avoids unclean tracebacks for documented refusals"

key-files:
  created:
    - "tests/fixtures/import_linter_violation/badpkg/__init__.py"
    - "tests/fixtures/import_linter_violation/badpkg/core/__init__.py"
    - "tests/fixtures/import_linter_violation/badpkg/core/leak.py"
    - "tests/fixtures/import_linter_violation/badpkg/broker/__init__.py"
    - "tests/fixtures/import_linter_violation/pyproject.toml"
    - "tests/import_boundary/__init__.py"
    - "tests/import_boundary/test_import_linter_contract.py"
    - "src/bithumb_bot/cli/main.py"
    - "src/bithumb_bot/cli/dispatcher.py"
    - "src/bithumb_bot/cli/handlers/__init__.py"
    - "src/bithumb_bot/cli/handlers/config_validate.py"
    - "src/bithumb_bot/cli/handlers/m0_selfcheck.py"
    - "src/bithumb_bot/cli/handlers/reserved.py"
    - "src/bithumb_bot/cli/handlers/m1_stubs.py"
    - "tests/cli/__init__.py"
    - "tests/cli/test_dispatcher.py"
    - "tests/cli/test_handlers_config_validate.py"
    - "tests/cli/test_handlers_m0_selfcheck.py"
    - "tests/cli/test_reserved_handlers.py"
    - "tests/cli/test_defense_in_depth.py"
  modified:
    - "pyproject.toml"
    - ".pre-commit-config.yaml"
    - ".gitignore"

key-decisions:
  - "Handler resolution uses `_make_lazy_handler(module, function)` importlib closures instead of direct imports. This lets 01-03-05's dispatcher land BEFORE 01-03-06/07/08/09 create the individual handler modules — each closure resolves on first dispatch. Tests use `mock.patch.dict(HANDLER_MAP, {...})` to inject controllable handlers without needing importlib patching."
  - "Reserved handler prints the refusal to stderr and returns 1 rather than raising `NotImplementedError`. Raising would emit an unclean Python traceback for a documented refusal; the return-code path preserves signal-to-noise for the operator (D-90 discipline)."
  - "structlog contextvars binding in the dispatcher is guarded by `try/except Exception` — plan 01-04 makes structlog live; until then, a missing/misconfigured logging layer must not block a documented refusal or a valid dispatch (T-1-03-06)."
  - "Import Linter checker invoked in tests via `sys.executable -c 'from importlinter.cli import lint_imports; sys.exit(lint_imports(...))'` — avoids `python -m importlinter` (no `__main__.py` exists), Windows `.exe` vs POSIX script differences, and any `uv run` rebuild path (offline-safe)."
  - "`PYTHONIOENCODING=utf-8` is set on every subprocess call that invokes `importlinter.cli.lint_imports` — Rich (import-linter's pretty-output library) writes non-ASCII glyphs that trip Windows `cp949` codec. This matches the 01-01 fix pattern for the same class of issue."
  - "Negative-fixture test passes `no_cache=True` to `lint_imports()` so `.import_linter_cache/` never appears inside the fixture tree. Belt-and-suspenders: the test also `shutil.rmtree`s any stray cache dir + the same directory is added to `.gitignore`."
  - "The negative-fixture mtime check compares BEFORE-set to same-set-AFTER (rather than reglobbing the directory) — this ignores transient artifacts by construction while still asserting no committed fixture file was mutated."
  - "The `bt config validate` handler is intentionally the ONLY inspection path for gate1.toml. It never mutates the file (mtime unchanged assertion in tests), never marks it approved, never records a decision — D-89 inspect-only discipline."
  - "The `m0 selfcheck` handler runs `reject_trade_credentials` internally as defense in depth EVEN THOUGH `validate()` already ran it. A trade cred set post-validate would still be refused inside the handler (D-89 ephemeral construction runs every check every time)."

requirements-completed: [SAFE-06]

coverage:
  - id: D1
    description: "Import Linter forbidden contract `core/ -> broker/` in pyproject.toml; `exclude_type_checking_imports = false`; lint-imports exits 0 on the real tree"
    requirement: SAFE-06
    verification:
      - kind: integration
        ref: "PYTHONIOENCODING=utf-8 .venv/Scripts/lint-imports.exe  (exit=0; Contracts: 1 kept, 0 broken)"
        status: pass
    human_judgment: false
  - id: D2
    description: "Negative-fixture proof that the contract mechanically fails on a real violation (D-73); fixture never mutated"
    requirement: SAFE-06
    verification:
      - kind: unit
        ref: "tests/import_boundary/test_import_linter_contract.py (3/3 pass)"
        status: pass
    human_judgment: false
  - id: D3
    description: "Functional `import-linter` pre-commit hook (D-71 / D-73) — placeholder swap-in"
    requirement: SAFE-06
    verification:
      - kind: config
        ref: ".pre-commit-config.yaml lines 61-77; PATH=.venv/Scripts pre-commit run import-linter --all-files -> Passed"
        status: pass
    human_judgment: false
  - id: D4
    description: "`bt` console script registered (`[project.scripts] bt = 'bithumb_bot.cli.main:main'`); `bt --help` runs after uv sync"
    requirement: SAFE-02
    verification:
      - kind: integration
        ref: "PYTHONIOENCODING=utf-8 .venv/Scripts/bt.exe --help (exit=0; usage text printed)"
        status: pass
    human_judgment: false
  - id: D5
    description: "argparse two-level dispatcher; --help/--version side-effect-free; unknown verb fails hard; validate-before-dispatch; named errors translated"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/cli/test_dispatcher.py (15/15 pass; --help does NOT construct BithumbSecrets or call validate; unknown-verb refusal to stderr)"
        status: pass
    human_judgment: false
  - id: D6
    description: "`bt config validate --through gate1` handler — D-85 defense in depth; inspects gate1.toml without mutation; never prints credential-adjacent tokens"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/cli/test_handlers_config_validate.py (9/9 pass; mtime unchanged; regex sweep for access|secret|token|withdraw returns none)"
        status: pass
    human_judgment: false
  - id: D7
    description: "`bt m0 selfcheck` handler — 3-section output (Gate-1 short summary + resolved key class + 5 risk denominators); D-70 class-only for trade refusal"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/cli/test_handlers_m0_selfcheck.py (10/10 pass; sentinel credential values absent from stdout/stderr on every path)"
        status: pass
    human_judgment: false
  - id: D8
    description: "D-87 reserved-verb handler + argparse wiring — every reserved subverb refuses uniformly; no side effects"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/cli/test_reserved_handlers.py (47/47 pass; parametrized over all 9 D-87 pairs; no httpx.AsyncClient instantiated; tmp_path stays empty)"
        status: pass
    human_judgment: false
  - id: D9
    description: "Defense-in-depth (D-85) tests — handlers reachable via direct Python import ALSO call validate() first; m1_stubs follow the same pattern"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/cli/test_defense_in_depth.py (11/11 pass; failing validate never reaches load_gate1 / load_secrets / stub body)"
        status: pass
    human_judgment: false

duration: 24 min
completed: 2026-09-08
status: complete
---

# Phase 1 Plan 03: Import boundary + `bt` CLI dispatcher Summary

**Live Import Linter contract enforces `core/ -> broker/` at pre-commit AND CI, provable by a committed negative-fixture package that mechanically fails on a real violation; the `bt` console script routes every capability through an argparse two-level dispatcher that validates before invoking any handler, with defense-in-depth `validate()`-first guards on every reachable handler; `bt config validate --through gate1` and `bt m0 selfcheck` are functional; every D-87 reserved verb refuses uniformly without scaffolding; every M1 verb is a defense-in-depth stub 01-04 will replace — 94/94 plan tests + 314/315 full-repo tests green (+1 Windows-symlink skip).**

## Performance

- **Duration:** 24 min
- **Started:** 2026-09-08T10:30:48Z
- **Tasks:** 9
- **Files created:** 20
- **Files modified:** 3 (`pyproject.toml`, `.pre-commit-config.yaml`, `.gitignore`)
- **Plan test count:** 94 passed (`tests/import_boundary/` 3 + `tests/cli/` 91) in 7.5s
- **Full repo test count:** 314 passed, 1 skipped (Windows symlink dev-mode), 1 deselected (`slow`) in ~15s
- **Static-gate command:** `uv run lint-imports && uv run python -m tools.decimal_ast_check <filtered>` — both green

## Accomplishments

- **Import Linter contract wired live (D-71).** `pyproject.toml` gained the `[[tool.importlinter.contracts]]` `forbidden` block with `source_modules = ["bithumb_bot.core"]` / `forbidden_modules = ["bithumb_bot.broker"]` — arrays per Finding 1's TOML-vs-INI gotcha. `exclude_type_checking_imports = false` (stricter setting) preserved from 01-01. `lint-imports` exits 0 on the real tree; the "Core must not import broker" contract is kept.
- **Committed negative-fixture proof (D-73).** `tests/fixtures/import_linter_violation/badpkg/` ships an intentional `from badpkg.broker import x` inside `badpkg.core.leak`, its own `pyproject.toml` with a matching-shape contract, and 3 pytest tests in `tests/import_boundary/test_import_linter_contract.py`. Positive: real-tree lint exits 0. Negative: fixture-tree lint exits non-zero AND stdout names the contract. Fixture mtimes unchanged after test (belt-and-suspenders).
- **Functional pre-commit hook (D-71 / D-73).** Placeholder `entry: "true"` on the `import-linter` hook id is replaced with `entry: lint-imports; language: system; pass_filenames: false; always_run: true`. Top-of-file CI comment documents the single-command form the operator's CI provider should execute: `uv run lint-imports && uv run python -m tools.decimal_ast_check <filtered file set>` — matches VALIDATION.md's static-gate command exactly.
- **`bt` console script registered (D-85, D-99).** `[project.scripts] bt = "bithumb_bot.cli.main:main"` — one script, no per-verb entry points. After `uv sync`, `.venv/Scripts/bt.exe` (Windows) exists and runs `bt --help` cleanly.
- **`bt` CLI dispatcher (D-85, D-89, T-1-03-02/04/05/06).** `src/bithumb_bot/cli/dispatcher.py` builds a two-level argparse tree for Phase-1 verbs (`config validate --through gate1`, `m0 selfcheck`, `m1 fetch-spec --market <...>`, `m1 verify-facts --bundle <...>`, `m1 verify-snapshot --snapshot <...>`) and — via 01-03-08 — for every D-87 reserved verb, each subparser labelled `RESERVED — future phase, not implemented (D-87)`. `HANDLER_MAP` uses lazy `_make_lazy_handler(module, function)` closures so the dispatcher landed BEFORE the individual handler modules existed (they arrived in 01-03-06/07/08/09). Dispatch sequence: argparse -> registry lookup -> `validate()` -> `_bind_contextvars` -> handler. Argparse handles `--help` / `--version` / empty argv BEFORE any registry lookup or credential load. Named errors from the SAFE-01/02/03 stack translate to clean stderr + exit 1.
- **`bt config validate --through gate1` handler (D-86, D-89).** `src/bithumb_bot/cli/handlers/config_validate.py`: first statement is `validate(("config","validate"))` (D-85). Loads `config/decisions/gate1.toml` via `load_gate1` — inspects only, never marks approved, never mutates the file (mtime unchanged assertion). Pure `format_gate1_summary(gate1, sha256)` printer returns the multi-line summary tests assert against without capsys: gate name, status, schema_version, source_commit prefix, spec-sha prefixes, file-sha prefix, provisional cap, value-deferred-field count. Sweeps `/access|secret|token|withdraw/i` on the output text and asserts no match.
- **`bt m0 selfcheck` handler (SAFE-02 per D-99, D-86).** `src/bithumb_bot/cli/handlers/m0_selfcheck.py`: three-section output — Gate-1 short summary, resolved key class (`public` / `account_read` / `account_read (incomplete)` / trade-refused), and the five SAFE-07 risk denominators with descriptions. `BithumbSecrets()` constructed ephemerally per D-89; `reject_trade_credentials` invoked as defense-in-depth trade-cred check even after `validate()`'s earlier check (a race-free single-check-per-invocation isn't possible with env-var lookup). Sentinel credential values never appear in stdout or stderr on any code path (positive-and-negative sentinel absence tests).
- **D-87 reserved-verb refusal (D-90 discipline).** `src/bithumb_bot/cli/handlers/reserved.py`: single `reserved_handler(args) -> int` prints `refused — this verb is reserved for a future phase — do not scaffold` to stderr and returns 1. DOES NOT raise `NotImplementedError` at runtime (unclean traceback vs. documented refusal). Every one of the 9 D-87 (verb, subverb) pairs — `m2 collect-observations`, `m2 calibrate-costs`, `m2 replay-known-answer`, `m4 evaluate-selection`, `m5 evaluate-module`, `freeze strategy`, `m6a verify-mock-broker`, `freeze final`, `holdout evaluate` — is bound to this handler in `HANDLER_MAP`. Parametrized end-to-end assertion: `main([verb, subverb])` returns non-zero, stderr contains reserved-message, no `httpx.AsyncClient` instantiated, `tmp_path` remains empty.
- **`m1_stubs` for plan 01-04 (D-85, D-90).** `src/bithumb_bot/cli/handlers/m1_stubs.py`: `m1_fetch_spec_stub`, `m1_verify_facts_stub`, `m1_verify_snapshot_stub`. Each stub: FIRST STATEMENT calls `validate((verb, subverb))`; if refused -> return 1 with clean refusal; else raise `RuntimeError("plan 01-04 required — not yet implemented")`. Direct Python callers who bypass the CLI still trip the guard first. Plan 01-04 replaces each stub body with the real handler, preserving the `validate()`-first pattern.

## Task Commits

Each task committed atomically (TDD tasks split into RED test-commit + GREEN feat-commit; final `fix` commit consolidates the Rule 3 auto-fix discovered by the full-suite re-run):

| Task     | Description                                                            | Commit    | Type       |
|----------|------------------------------------------------------------------------|-----------|------------|
| 01-03-01 | Add `[[tool.importlinter.contracts]]` forbidden block (D-71)           | `8b65948` | config     |
| 01-03-02 | Negative-fixture `badpkg/` + `test_import_linter_contract.py`          | `fd489b3` | test       |
| 01-03-03 | Wire `lint-imports` pre-commit hook (D-71, D-73)                       | `9bc06f6` | config     |
| 01-03-04 | Register `bt` console script (D-85, D-99)                              | `19e979e` | config     |
| 01-03-05 | RED — failing tests for `bt` CLI dispatcher                            | `88e5053` | test (RED) |
| 01-03-05 | GREEN — dispatcher.py + main.py + handlers/__init__.py (D-85, D-89)    | `5de0407` | feat       |
| 01-03-06 | RED — failing tests for `bt config validate --through gate1`           | `381cd41` | test (RED) |
| 01-03-06 | GREEN — `config_validate.py` handler (D-86, D-89)                      | `ce5681b` | feat       |
| 01-03-07 | RED — failing tests for `bt m0 selfcheck`                              | `4df2154` | test (RED) |
| 01-03-07 | GREEN — `m0_selfcheck.py` handler (D-86, SAFE-02 per D-99)             | `27a2b5e` | feat       |
| 01-03-08 | RED — failing tests for D-87 reserved-verb handler                     | `f5bce75` | test (RED) |
| 01-03-08 | GREEN — `reserved.py` + argparse reserved subparsers (D-87, D-90)      | `f299146` | feat       |
| 01-03-09 | RED — defense-in-depth tests for handlers + m1_stubs                   | `e228508` | test (RED) |
| 01-03-09 | GREEN — `m1_stubs.py` (D-85, D-90)                                     | `b8d08f8` | feat       |
| —        | fix — Rule 3 auto-fix: D-69 sweep + import-linter cache cleanup        | `1b7d230` | fix        |

**Plan metadata commit:** appended at close-out.

## Decisions Made

1. **Handler resolution uses `_make_lazy_handler(module, function)` importlib closures.** This lets 01-03-05's dispatcher land BEFORE 01-03-06/07/08/09 create the individual handler modules. Each closure resolves + invokes the target function on first dispatch via `importlib.import_module(module_name)` + `getattr`. Tests bypass the closure entirely by monkey-patching `HANDLER_MAP` with `mock.patch.dict` to inject controllable callables — no need to patch importlib itself. This keeps the RED->GREEN TDD cadence clean per task and avoids scaffolding stub handler modules just so imports work.
2. **Reserved handler prints refusal + returns 1 instead of raising `NotImplementedError`.** Raising would emit an unclean Python traceback for a documented refusal. Return-code refusal preserves signal-to-noise for the operator (D-90 discipline). The registry sentinel `RESERVED_HANDLER_ERROR` from 01-01 still raises when accidentally invoked — this handler is what the DISPATCHER binds to for the actual CLI path.
3. **structlog contextvars binding guarded by `try/except Exception`.** Plan 01-04 makes structlog live; until then, a missing or misconfigured logging layer must not block a documented refusal or a valid dispatch (T-1-03-06). Comment `TODO(plan-01-04): promote to structured logging` marks the ticket.
4. **Named-error translation table (`_TRANSLATED_EXCEPTIONS`).** Five specific exceptions from the SAFE-01/02/03 stack (`UnknownCapabilityError`, `Gate1LoadError`, `ProhibitedCredentialDetectedError`, `SecretsFileInsideRepoError`, `AmbiguousSecretsConfigurationError`) translate to clean stderr + exit 1. Every other exception propagates for a real Python traceback — the operator should never see a documented refusal as a traceback, but a genuine bug MUST NOT be silently swallowed.
5. **Import Linter subprocess invocation via `sys.executable -c 'from importlinter.cli import lint_imports; sys.exit(lint_imports(...))'`.** Avoids three failure modes: (a) `python -m importlinter` — no `__main__.py` exists in the package; (b) `.exe` vs POSIX-script differences between platforms for the `lint-imports` script; (c) `uv run` in the subprocess would trigger a rebuild which requires network — offline-safe invocation was mandatory for local dev.
6. **`PYTHONIOENCODING=utf-8` on every subprocess call that invokes `importlinter.cli.lint_imports`.** Rich (import-linter's pretty-output library) writes non-ASCII glyphs that trip Windows' default `cp949` codec. This matches the 01-01 pattern for the same issue class (`UnicodeEncodeError: 'cp949' codec can't encode character '∙'`).
7. **Negative-fixture test passes `no_cache=True` to `lint_imports()` + belt-and-suspenders cache-dir cleanup.** By default the checker drops a `.import_linter_cache/` directory into the working directory — inside the fixture tree, that would mutate the "never-mutate the fixture" invariant. Disabling caching for the test + `shutil.rmtree` any stray cache directory + adding `.import_linter_cache/` to `.gitignore` covers every failure mode.
8. **The negative-fixture mtime check compares BEFORE-set to same-set-AFTER (rather than reglobbing).** Reglobbing would pick up transient artifacts (cache dir contents) even with `no_cache=True` if a race left something behind. Comparing only the initially-captured file set proves no COMMITTED file was mutated, which is the invariant we care about.
9. **`bt config validate` is inspect-only per D-89.** The handler never mutates gate1.toml, never marks it approved, never records a decision. mtime-unchanged assertion covers this explicitly in a `tmp_path` copy test.
10. **`m0_selfcheck` runs `reject_trade_credentials` internally even though `validate()` already did.** `validate()` and the handler both construct `BithumbSecrets()` ephemerally per D-89 — the handler's own re-check catches any trade cred that appeared in env between the two constructions (a real threat model if a subshell writes to env; a paranoid safety net otherwise).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `uv run` rebuild path fails without `--system-certs` (inherited from 01-01)**
- **Found during:** Task 01-03-01 verification (`uv run lint-imports`).
- **Issue:** `uv run` triggers an editable-install build; hatchling reaches out to PyPI for `hatchling` itself; the environment's TLS trust store returns `invalid peer certificate: UnknownIssuer` (same corporate cert issue documented in 01-01-SUMMARY §Deviations 2).
- **Fix:** Invoke checkers directly through the already-installed venv scripts (`.venv/Scripts/lint-imports.exe`, `.venv/Scripts/pre-commit.exe`) or through `sys.executable` + `-c` snippets; used `uv sync --extra dev --system-certs` for the one sync required by task 01-03-04 to install the `bt` console script; used `uv run --no-sync --extra dev pytest` for all test runs to skip the rebuild path entirely.
- **Files modified:** none.
- **Committed in:** every task commit reflects this by using `--no-sync` in verification commands.

**2. [Rule 3 - Blocking] Windows `cp949` codec cannot encode Rich's non-ASCII output**
- **Found during:** Task 01-03-01 first attempt (`.venv/Scripts/lint-imports.exe`).
- **Issue:** `UnicodeEncodeError: 'cp949' codec can't encode character '∙' in position 0` — import-linter uses Rich for pretty output, which writes decorative glyphs Windows' default codec can't handle.
- **Fix:** `PYTHONIOENCODING=utf-8` on every subprocess call that invokes the checker (test runner + doc CI comments). Documented in the test module docstring for future maintenance.
- **Files modified:** `tests/import_boundary/test_import_linter_contract.py` documents the pattern.
- **Committed in:** `fd489b3` (task 01-03-02).

**3. [Rule 3 - Blocking] `python -m importlinter` fails — no `__main__.py`**
- **Found during:** Task 01-03-02 first attempt at the subprocess invocation.
- **Issue:** `No module named importlinter.__main__; 'importlinter' is a package and cannot be directly executed`.
- **Fix:** Invoke `sys.executable -c "from importlinter.cli import lint_imports; sys.exit(lint_imports(config_filename=<...>))"`. Cross-platform + avoids the `lint-imports.exe` vs POSIX-script split.
- **Files modified:** `tests/import_boundary/test_import_linter_contract.py`.
- **Committed in:** `fd489b3` (task 01-03-02).

**4. [Rule 1 - Bug] `.import_linter_cache/` mutation invalidates the fixture mtime check**
- **Found during:** Full-repo test re-run after task 01-03-09.
- **Issue:** The negative-fixture test's mtime-comparison caught the checker dropping a `.import_linter_cache/` directory into the fixture tree — `mtimes_before == mtimes_after` failed because the "after" glob picked up newly-created cache files.
- **Fix:** (a) Pass `no_cache=True` to `lint_imports()` in the negative-fixture call. (b) Refactor mtime check to iterate `mtimes_before` keys only (not reglob). (c) Belt-and-suspenders `shutil.rmtree` cleanup for any stray cache dir. (d) Add `.import_linter_cache/` to `.gitignore`.
- **Files modified:** `tests/import_boundary/test_import_linter_contract.py`, `.gitignore`.
- **Committed in:** `1b7d230`.

**5. [Rule 1 - Bug] D-69 static sweep failed on new files mentioning `withdraw` as a documentation regex**
- **Found during:** Full-repo test re-run after task 01-03-09.
- **Issue:** `tests/secrets/test_no_withdrawal_credential.py::test_every_withdraw_match_cites_D69` flagged three new offenders: `src/bithumb_bot/cli/handlers/config_validate.py:24` (docstring regex reference), `tests/cli/test_dispatcher.py:301` (assertion), `tests/cli/test_handlers_config_validate.py:68` (assertion). Each mentioned `withdraw` without a D-69 marker in the 3-line context window.
- **Fix:** Added inline `D-69: no withdrawal path exists in this project` comments directly next to each `withdraw` occurrence.
- **Files modified:** `src/bithumb_bot/cli/handlers/config_validate.py`, `tests/cli/test_dispatcher.py`, `tests/cli/test_handlers_config_validate.py`.
- **Committed in:** `1b7d230`.

---

**Total deviations:** 5 auto-fixed (2 Rule 3 environment blockers inherited from 01-01/carried, 1 Rule 3 blocker for module invocation form, 2 Rule 1 bugs surfaced by the full-suite re-run).
**Impact on plan:** No scope creep. Every deviation is a mechanical workaround for a real environment / packaging / static-sweep constraint. The plan's design decisions (dispatcher structure, handler map, defense-in-depth) landed as written. The two Rule 1 bugs are surface artifacts of the plan's own D-69 discipline reaching the new files this plan added — the discipline works.

## Issues Encountered

None during planned work beyond the deviations above. Every task's RED->GREEN cycle completed on the first GREEN attempt; the two full-suite regressions were caught by the plan's own static-sweep discipline and fixed inline.

## Threat Register — Mitigation Verification

| Threat ID | Category | Component | Severity | Status | Test(s) |
|-----------|----------|-----------|----------|--------|---------|
| T-1-03-01 | Elevation of Privilege | `core/ -> broker/` silently added | critical | mitigated | Import Linter contract in `pyproject.toml` (D-71); `.pre-commit-config.yaml` hook (D-73); `tests/import_boundary/test_import_linter_contract.py::TestImportLinterContract::test_negative_fixture_fails` proves the contract mechanically fails on a real violation via the committed `badpkg/` fixture |
| T-1-03-02 | Elevation of Privilege | CLI handler invoked without passing through `validate()` | high | mitigated | (a) `tests/cli/test_dispatcher.py::TestValidateBeforeDispatch::test_valid_capability_calls_validate_and_then_handler` proves call order; (b) `tests/cli/test_defense_in_depth.py` proves every direct-Python handler call trips `validate()` first, even bypassing `main()` |
| T-1-03-03 | Elevation of Privilege | Reserved future verb scaffolded as functional | high | mitigated | `tests/cli/test_reserved_handlers.py::TestEveryReservedVerbRefuses` parametrized over all 9 D-87 pairs; every one returns non-zero with reserved-message; `TestReservedHandlerReturnsRefusal::test_reserved_handler_never_raises` proves clean refusal (not traceback) |
| T-1-03-04 | Information Disclosure | `bt --help` / `--version` inadvertently loads credentials | medium | mitigated | `tests/cli/test_dispatcher.py::TestHelpAndVersionAreSideEffectFree`: `test_help_does_not_load_secrets` (BithumbSecrets constructor `assert_not_called`), `test_help_does_not_call_validate`, `test_help_via_subprocess_env_with_sentinel_creds_does_not_leak` (subprocess with sentinel creds in env, assert absent from output) |
| T-1-03-05 | Denial of Service | Dispatcher accepts M6B / live verb | medium | mitigated | `tests/cli/test_dispatcher.py::TestHandlerMapSurface::test_no_m6b_or_live_verbs_in_handler_map` (registry lookup); D-96 already enforced in 01-01 registry tests |
| T-1-03-06 | Repudiation | Handler invocation not logged with invocation ID | low | mitigated | Dispatcher calls `_bind_contextvars(capability, command)` with a `uuid4()` invocation ID; guarded by `try/except` per plan 01-04 dependency. Once 01-04 lands structlog live, every handler invocation is bound to an invocation ID for post-hoc audit |
| T-1-03-SC | Tampering | npm/pip/cargo installs | low | accepted (per plan) | No new dependencies — `import-linter` was already installed by 01-01; every new module uses stdlib `argparse` + `subprocess` + `importlib` |

## Threat Flags

None — no new security-relevant surface was introduced outside the plan's `<threat_model>` block. The `bt` console script is the exact CLI surface D-85 mandates; every capability check flows through the existing `validate()` from 01-01; every credential surface flows through the existing `BithumbSecrets` / `load_secrets` / `reject_trade_credentials` from 01-02. Plan 01-03 adds the wiring, not the credential-touching code.

## Known Stubs

**Documented and correct architectural boundaries — NOT stubs blocking Phase-1 goals:**

- **`m1_fetch_spec_stub`, `m1_verify_facts_stub`, `m1_verify_snapshot_stub`** in `src/bithumb_bot/cli/handlers/m1_stubs.py`. Each raises `RuntimeError("plan 01-04 required — not yet implemented")` when invoked (after validate passes). This is intentional D-90 discipline — the plan explicitly wires argparse for these verbs (so `bt --help` shows them) while making the runtime path an honest refusal. Plan 01-04 replaces each stub body with the real handler; the `validate()`-first pattern MUST be preserved by the replacement.

- **`_bind_contextvars` guarded `try/except`** in `src/bithumb_bot/cli/dispatcher.py`. Structlog isn't yet configured at process start — plan 01-04 owns that wiring. The guard is documented with `TODO(plan-01-04): promote to structured logging`.

No stubs prevent the plan's own goal from being achieved. Every Phase-1 D-86 verb functional in this plan (`config validate`, `m0 selfcheck`) is real code end-to-end.

## Next Phase Readiness

Ready for plan **01-04** (`BithumbSpec` authenticated read adapter, hashed fee/tick/min-order snapshot, per-channel rate limiting). Prerequisites this plan provides:

- **`bt` CLI dispatcher is functional.** Plan 01-04 replaces the three `m1_stubs` bodies with real handlers. The `HANDLER_MAP` wiring is stable — no dispatcher edits needed unless a new verb is added.
- **Defense-in-depth pattern is documented + tested.** Plan 01-04's real M1 handlers MUST preserve the `validate()`-first FIRST STATEMENT discipline; test_defense_in_depth.py is the regression barrier.
- **Import Linter contract is live.** Plan 01-04's `bithumb_bot.bithumb_spec.*` modules land under `core/`-eligible or `broker/`-eligible paths; the contract rejects any accidental core -> broker import at pre-commit AND CI.
- **`bt --help` / `bt --version` invariant is protected.** Plan 01-04 MUST NOT introduce a top-level import that pulls httpx/structlog/PyJWT into the argparse-only path — lazy imports inside the M1 fetch-spec handler are the pattern.
- **Sentinel-value absence tests set the discipline.** Plan 01-04's real M1 handler must never leak a credential value; the tests in this plan (`test_help_via_subprocess_env_with_sentinel_creds_does_not_leak`, `test_trade_cred_env_refused_class_only`) are templates for the M1 handler's own tests.
- **`.pre-commit-config.yaml` is complete.** Every hook is functional — no placeholder ids remain. Plan 01-04 does not need to touch pre-commit.

No blockers or concerns for the next plan.

## Self-Check: PASSED

- Every declared `key-files.created` file exists on disk (20/20 verified via `[ -f ]`).
- Every recorded task commit is in `git log --oneline --all` (15/15 verified: `8b65948`, `fd489b3`, `9bc06f6`, `19e979e`, `88e5053`, `5de0407`, `381cd41`, `ce5681b`, `4df2154`, `27a2b5e`, `f5bce75`, `f299146`, `e228508`, `b8d08f8`, `1b7d230`).
- Every plan `<success_criteria>` bullet re-executed and passed at close-out:
  - `uv run lint-imports` (invoked via venv scripts) exits 0; contract "Core must not import broker" KEPT (1 kept, 0 broken).
  - Negative-fixture runner test: positive exits 0, negative exits non-zero AND stdout names the contract (`tests/import_boundary/test_import_linter_contract.py` 3/3).
  - `import-linter` pre-commit hook runs with `pass_filenames: false, always_run: true, language: system`.
  - `bt --help` / `bt --version` exit 0, do NOT load credentials, do NOT call `validate()`, do NOT construct `BithumbSecrets` (verified in `test_help_does_not_load_secrets` + `test_help_via_subprocess_env_with_sentinel_creds_does_not_leak`).
  - `bt bogus verb` fails non-zero with "unknown" / "invalid" in stderr (`test_bogus_verb_returns_nonzero_and_stderr_mentions_unknown`).
  - `bt config validate --through gate1` exits 0 on committed state, prints structured summary, exits 0 (`test_main_config_validate_through_gate1_exits_zero`).
  - `bt m0 selfcheck` prints resolved Gate-1 decisions + resolved key class + all 5 risk denominators, exits 0 on clean env (`test_main_m0_selfcheck_exits_zero_on_clean_env`).
  - Every D-87 reserved verb (9 pairs) exits non-zero with reserved-message; no side effects (`test_reserved_verb_returns_nonzero_with_reserved_message` parametrized).
  - Every internal service function reachable via direct Python import calls `validate()` first (`test_defense_in_depth.py` 11/11).
- Every plan `<acceptance_criteria>` (per-task Oracle) recorded green in the RED->GREEN commit pairs; no criterion silently skipped.
- Every `<threat_model>` entry has a corresponding test (Threat Register table above).
- Full repo test suite: **314 passed, 1 skipped, 1 deselected slow** in 14.6s.
- Static-gate composed command: `lint-imports` exit 0 + `decimal_ast_check <filtered>` exit 0; mypy `--strict` clean on 24 source files.

---
*Phase: 01-safety-foundation-bithumb-spec-adapter*
*Completed: 2026-09-08*
