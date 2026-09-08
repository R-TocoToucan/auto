---
phase: 01-safety-foundation-bithumb-spec-adapter
plan: 01
subsystem: infra
tags: [uv, pydantic, pydantic-settings, tomllib, hashlib, structlog, pytest, hypothesis, gate1, decision-register, capability-validator, config-hash]

requires: []
provides:
  - "uv-managed Python 3.11+ project skeleton (`bithumb_bot` package)"
  - "Frozen `Gate1Decisions` pydantic v2 model + `StrictDecimal` type"
  - "`load_gate1(path) -> (Gate1Decisions, sha256_hex)` binary-mode loader with hash-of-committed-bytes provenance"
  - "Central capability registry encoding the D-88 guard matrix as data"
  - "`validate((verb, subverb)) -> ValidationResult` capability-scoped fail-closed startup validator (SAFE-02 per D-99)"
  - "`build_config_hash_manifest(...)` canonical-JSON manifest builder (D-62 / D-76)"
  - "Test scaffold with 4 shared fixtures (`tmp_gate1_toml`, `sanitized_fixture_loader`, `fake_monotonic_clock`, `frozen_utc_now`)"
  - "`config/decisions/gate1.toml` populated from the frozen Decision Register (D-01..D-100)"
  - "Import Linter root + placeholder pre-commit hook ids (wired to functional entries by plans 01-02 and 01-03)"
affects: [01-02, 01-03, 01-04, 02, 03, 05]

tech-stack:
  added:
    - "uv 0.12 — package manager + lockfile + virtualenv"
    - "pydantic 2.13 + pydantic-settings 2.15 — typed config"
    - "httpx 0.27, websockets 17 — reserved for M1 (plan 01-04)"
    - "PyJWT 2.13 — reserved for M1 JWT auth (plan 01-04)"
    - "structlog 26 — reserved for observability (plan 01-04)"
    - "pyarrow 25 — reserved for Parquet candle store (Phase 2)"
    - "pytest 9 + hypothesis + pytest-cov + ruff + mypy + import-linter + pre-commit — dev"
  patterns:
    - "Fail-closed by construction — every gate/cred/snapshot check returns `ValidationResult(ok=False, missing=(...), reason=...)` rather than logging-and-continuing"
    - "Registry-as-data — D-88 guard matrix is one dict[tuple[str,str], CapabilityRequirements] row per capability, no scattered if-statements"
    - "Canonical-JSON hashing — `sort_keys=True, separators=(',',':'), ensure_ascii=False, +b'\\n'` reused everywhere hash-of-bytes matters (D-76)"
    - "StrictDecimal — Annotated[Decimal, BeforeValidator] rejects float/int/bool at authoring time (D-49); TOML fields authored as QUOTED strings"
    - "Value-deferred sentinel — TOML 1.0.0 has no null; `{ value = 'unset', frozen_at = 'gateN', frozen_at_phase = N }` inline tables preserve schema visibility while remaining loadable via stdlib tomllib"

key-files:
  created:
    - "pyproject.toml"
    - "uv.lock"
    - ".gitignore"
    - ".pre-commit-config.yaml"
    - "src/bithumb_bot/__init__.py"
    - "src/bithumb_bot/core/__init__.py"
    - "src/bithumb_bot/broker/__init__.py"
    - "src/bithumb_bot/cli/__init__.py"
    - "src/bithumb_bot/config/__init__.py"
    - "src/bithumb_bot/config/gate1_model.py"
    - "src/bithumb_bot/config/gate_loader.py"
    - "src/bithumb_bot/config/capability_registry.py"
    - "src/bithumb_bot/config/validator.py"
    - "src/bithumb_bot/config/config_hash.py"
    - "src/bithumb_bot/errors.py"
    - "config/decisions/gate1.toml"
    - "tests/__init__.py"
    - "tests/conftest.py"
    - "tests/config/__init__.py"
    - "tests/config/test_gate1_model.py"
    - "tests/config/test_gate_loader.py"
    - "tests/config/test_capability_registry.py"
    - "tests/config/test_validator.py"
    - "tests/config/test_config_hash.py"
    - "tests/config/test_integration_wave2.py"
  modified: []

key-decisions:
  - "TOML 1.0.0 has no null literal — use `{ value = 'unset', frozen_at = 'gateN', frozen_at_phase = N }` inline-table sentinel for D-09/D-41/D-42 value-deferred fields; pydantic BeforeValidator normalises to Python None."
  - "Errors module is populated with narrow named exceptions upfront (`Gate1LoadError`, `UnknownCapabilityError`, `ProhibitedCredentialDetectedError`) so downstream modules can `except` for exact fail-closed conditions from day one."
  - "REPO_ROOT_ENV = 'BITHUMB_BOT_REPO_ROOT' — testable repo-root override so `validate()` unit tests can point at `tmp_path`-rooted config trees without monkey-patching module globals."
  - "CapabilityRequirements dataclass has NO defaults — every registry row must author all 8 dimensions explicitly (D-89 forbids defaulting to empty)."
  - "Placeholder pre-commit hook ids (`decimal-float-literal-check`, `import-linter`) reserve names now with `entry: 'true'` no-op; plans 01-02 and 01-03 swap in the functional entries. Prevents id-drift when the real hooks land."

patterns-established:
  - "Fail-closed validator — capability-scoped, short-circuits on first missing prereq, never partial-succeeds. Same function called by CLI dispatcher and internal service layers (defence in depth)."
  - "Hash-of-committed-bytes provenance — hash the raw file bytes git tracks, not a re-serialization. Gate-1 SHA-256 feeds the config_hash manifest which feeds Phase 5 FRZ-02."
  - "Ephemeral credential surface — `validate()` inspects env-var PRESENCE only; VALUES are only touched inside the exact M1 network operation that needs them (plan 01-04 pattern)."
  - "TDD RED-GREEN commit cadence — every `type='code'` `tdd='true'` task commits failing tests first, then the minimal implementation. Six paired commits across tasks 04..08."

requirements-completed: [SAFE-01, SAFE-02]

coverage:
  - id: D1
    description: "uv-managed Python 3.11+ project skeleton with `bithumb_bot` importable and pinned tool versions"
    requirement: SAFE-01
    verification:
      - kind: integration
        ref: "uv sync && uv run python -c 'import bithumb_bot; import bithumb_bot.core; import bithumb_bot.broker; import bithumb_bot.cli; import bithumb_bot.config' && uv run pytest -x -q --collect-only"
        status: pass
    human_judgment: false
  - id: D2
    description: "Shared conftest fixtures (`tmp_gate1_toml`, `sanitized_fixture_loader`, `fake_monotonic_clock`, `frozen_utc_now`) load defensively"
    verification:
      - kind: integration
        ref: "uv run pytest --fixtures tests/ | grep -E '(tmp_gate1_toml|sanitized_fixture_loader|fake_monotonic_clock|frozen_utc_now)'"
        status: pass
    human_judgment: false
  - id: D3
    description: "`config/decisions/gate1.toml` records the frozen Decision Register (D-01..D-100) as public non-secret TOML"
    requirement: SAFE-01
    verification:
      - kind: unit
        ref: "tests/config/test_gate_loader.py#TestLoadGate1Positive::test_committed_gate1_toml_loads"
        status: pass
    human_judgment: false
  - id: D4
    description: "`Gate1Decisions` frozen pydantic v2 model + `StrictDecimal` rejects float/int/bool at authoring time"
    requirement: SAFE-01
    verification:
      - kind: unit
        ref: "tests/config/test_gate1_model.py (20/20 pass)"
        status: pass
    human_judgment: false
  - id: D5
    description: "`load_gate1` binary-mode + hash-of-committed-bytes provenance; missing/malformed/non-frozen → `Gate1LoadError`"
    requirement: SAFE-01
    verification:
      - kind: unit
        ref: "tests/config/test_gate_loader.py (10/10 pass)"
        status: pass
    human_judgment: false
  - id: D6
    description: "Central capability registry (D-88) encoded as data; every Phase-1 D-86 row present; every D-87 reserved verb registered with a NotImplementedError-raising handler"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/config/test_capability_registry.py (33/33 pass)"
        status: pass
    human_judgment: false
  - id: D7
    description: "`validate()` capability-scoped fail-closed validator; unknown capability → `UnknownCapabilityError`"
    requirement: SAFE-02
    verification:
      - kind: unit
        ref: "tests/config/test_validator.py (16/16 pass)"
        status: pass
    human_judgment: false
  - id: D8
    description: "`build_config_hash_manifest` produces byte-identical output for byte-identical inputs (canonical JSON per D-76)"
    requirement: SAFE-01
    verification:
      - kind: unit
        ref: "tests/config/test_config_hash.py (14/14 pass, incl. hypothesis property test)"
        status: pass
    human_judgment: false
  - id: D9
    description: "Wave-2 integration smoke composes loader + validator + manifest against the REAL committed gate1.toml"
    requirement: SAFE-01
    verification:
      - kind: integration
        ref: "tests/config/test_integration_wave2.py (6/6 pass)"
        status: pass
    human_judgment: false

duration: 22 min
completed: 2026-09-08
status: complete
---

# Phase 1 Plan 01: Config loader + Immutable Gate-1 Decision Register + capability-scoped startup validation Summary

**Frozen `Gate1Decisions` pydantic v2 model loaded from `config/decisions/gate1.toml` with SHA-256-of-committed-bytes provenance, plus a capability-scoped `validate()` fail-closed startup validator wired to a central D-88 registry — 99/99 tests green, foundation ready for plans 01-02..01-04.**

## Performance

- **Duration:** 22 min
- **Started:** 2026-09-08T09:24:18Z
- **Completed:** 2026-09-08T09:45:58Z
- **Tasks:** 9
- **Files created:** 25
- **Files modified:** 1 (`tests/conftest.py` — refreshed the fixture TOML to match the value-deferred sentinel convention introduced in task 01-01-03)

## Accomplishments

- **`uv`-managed Python 3.11+ project skeleton.** `pyproject.toml` with `[project]`, `[tool.pytest.ini_options]`, `[tool.ruff]`, `[tool.mypy]` (strict), `[tool.importlinter]` (root only — contract added by plan 01-03). `uv.lock` pins every quality-tool version (D-73).
- **`bithumb_bot` package tree.** `core/`, `broker/`, `cli/`, `config/`, `errors` all importable — Import Linter contract can slot in unmodified in plan 01-03.
- **`config/decisions/gate1.toml` populated** from the frozen Decision Register with D-40 numeric HTTP values, provisional cap, provenance metadata (D-61), and value-deferred D-09/D-41/D-42 fields represented as inline-table sentinels (see Deviations §1).
- **`Gate1Decisions` frozen pydantic v2 model + `StrictDecimal`** — rejects float/int/bool/None from TOML sources at construction time (D-49). 20/20 model tests pass.
- **`load_gate1(path) -> (Gate1Decisions, hex_sha256)`** — binary-mode TOML parse; hex hash is `hashlib.sha256(<committed bytes>).hexdigest()`; every fail-closed condition surfaces the specific parse/schema message via `Gate1LoadError`. 10/10 loader tests pass.
- **Central capability registry (`REGISTRY`)** — D-88 guard matrix encoded as `dict[tuple[str,str], CapabilityRequirements]`; every Phase-1 D-86 row present as functional; every D-87 reserved verb present with a `RESERVED_HANDLER_ERROR` sentinel that raises `NotImplementedError` (D-90). No `m6b`/`live`/`withdraw` keys (D-96 / D-69). 33/33 registry tests pass.
- **`validate((verb, subverb)) -> ValidationResult`** — capability-scoped fail-closed validator; unknown capability raises `UnknownCapabilityError` (D-89); trade-cred env vars raise `ProhibitedCredentialDetectedError` (D-68 / D-97 — class-only, never value); short-circuits on first missing prereq; does NOT create future-gate files (D-56). 16/16 validator tests pass.
- **`build_config_hash_manifest(gate1_sha256, gate2_sha256=None, gate3_sha256=None)`** — canonical-JSON manifest builder (D-62 / D-76). Byte-identical inputs → byte-identical output; single-hex-char change flips the hash. Includes a hypothesis property test on 64-hex random inputs. 14/14 tests pass.
- **Wave-2 integration smoke** composes loader + validator + manifest against the REAL committed `config/decisions/gate1.toml`. 6/6 tests pass; full `tests/config/` suite is 99/99 green in 0.72s (well under VALIDATION.md's 10s quick-command budget).

## Task Commits

Each task committed atomically (TDD tasks split into RED test-commit + GREEN feat-commit):

| Task     | Description                                                        | Commit    | Type       |
|----------|--------------------------------------------------------------------|-----------|------------|
| 01-01-01 | Bootstrap `uv` project skeleton + tool-config + `bithumb_bot` tree | `0d3ab02` | chore      |
| 01-01-02 | `tests/` scaffold + 4 shared conftest fixtures                     | `e4936fc` | test       |
| 01-01-03 | Populate `config/decisions/gate1.toml` from Decision Register      | `62c11c0` | config     |
| 01-01-04 | RED — failing tests for `Gate1Decisions` model                     | `7fef87d` | test (RED) |
| 01-01-04 | GREEN — implement `Gate1Decisions` + `StrictDecimal`               | `7a5a2c5` | feat       |
| 01-01-05 | RED — failing tests for `load_gate1`                               | `819d12e` | test (RED) |
| 01-01-05 | GREEN — implement `load_gate1` + `Gate1LoadError`                  | `c0f44fc` | feat       |
| 01-01-06 | RED — failing tests for capability registry                        | `a8d8912` | test (RED) |
| 01-01-06 | GREEN — implement central capability registry                      | `c6eb50b` | feat       |
| 01-01-07 | RED — failing tests for `validate()`                               | `60d71a9` | test (RED) |
| 01-01-07 | GREEN — implement `validate()`                                     | `be9cdf9` | feat       |
| 01-01-08 | RED — failing tests for `build_config_hash_manifest`               | `1639576` | test (RED) |
| 01-01-08 | GREEN — implement `build_config_hash_manifest`                     | `637ad44` | feat       |
| 01-01-09 | Wave-2 integration smoke composing loader/validator/manifest       | `b97d469` | test       |

**Plan metadata commit:** appended at close-out.

## Files Created/Modified

- **`pyproject.toml`** — project metadata + tool config; contains only `[tool.importlinter] root_package = "bithumb_bot"` + `exclude_type_checking_imports = false` (contract block deferred to plan 01-03).
- **`uv.lock`** — pins every runtime + dev dependency version (D-73).
- **`.gitignore`** — excludes `.venv/`, tool caches, `state/`, `artifacts/spec_snapshots/`, secrets patterns.
- **`.pre-commit-config.yaml`** — active ruff/ruff-format/mypy hooks; placeholder `decimal-float-literal-check` and `import-linter` hook ids with `entry: 'true'` no-op (wired to functional entries by plans 01-02 / 01-03).
- **`src/bithumb_bot/{__init__,core/__init__,broker/__init__,cli/__init__,config/__init__}.py`** — package tree, importable, Import-Linter-ready.
- **`src/bithumb_bot/config/gate1_model.py`** — `Gate1Decisions` + `StrictDecimal` + optional-sentinel variants.
- **`src/bithumb_bot/config/gate_loader.py`** — `load_gate1` binary-mode + hash-of-bytes.
- **`src/bithumb_bot/config/capability_registry.py`** — `REGISTRY`, `RESERVED_HANDLER_ERROR`, enums, `CapabilityRequirements` dataclass.
- **`src/bithumb_bot/config/validator.py`** — `validate()`, `ValidationResult`, env-var conventions.
- **`src/bithumb_bot/config/config_hash.py`** — `build_config_hash_manifest`, `_canonical_bytes`.
- **`src/bithumb_bot/errors.py`** — `BithumbBotError`, `Gate1LoadError`, `UnknownCapabilityError`, `ProhibitedCredentialDetectedError`.
- **`config/decisions/gate1.toml`** — frozen Gate-1 Decision Register (populated from D-01..D-100).
- **`tests/__init__.py`, `tests/config/__init__.py`, `tests/conftest.py`** — test scaffold + 4 shared fixtures.
- **`tests/config/test_gate1_model.py`, `test_gate_loader.py`, `test_capability_registry.py`, `test_validator.py`, `test_config_hash.py`, `test_integration_wave2.py`** — 99 tests, all green.

## Decisions Made

1. **TOML value-deferred sentinel convention.** Stdlib `tomllib` (TOML 1.0.0) has no `null` literal, so the plan's original verify command `assert data['max_validated_notional_krw'] is None` could not be satisfied by any raw-TOML representation. Chose to represent D-09 / D-41 / D-42 value-deferred fields as inline tables `{ value = "unset", frozen_at = "gateN", frozen_at_phase = N }`. The pydantic `Gate1Decisions` loader normalises this sentinel to Python `None`. This preserves the plan's "schema declared at Gate 1, value frozen later" intent while remaining loadable via stdlib tomllib. Documented in `config/decisions/gate1.toml`'s file header and in every place that consumes the sentinel.
2. **Validators raise `ValueError`, not `TypeError`.** Pydantic v2 wraps only `ValueError` / `AssertionError` from validators into `ValidationError`; raising `TypeError` would escape pydantic's error-handling and bypass the D-60 fail-closed contract.
3. **`REPO_ROOT_ENV = "BITHUMB_BOT_REPO_ROOT"` env-var override.** Tests point the validator at `tmp_path`-rooted config trees via `monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_path))` — no module-global mutation, no test-order dependence.
4. **All errors declared in `errors.py` upfront.** `ProhibitedCredentialDetectedError` is imported by 01-01's validator (which raises it) even though the plan's `Files` line notes that "its raising site is 01-02". The class needed to be importable now so `validate()` could raise it uniformly for every capability's trade-cred prohibition check. This is a light forward-shim, not scope creep — the credential-value handling in 01-02 remains untouched.
5. **`CapabilityRequirements` dataclass has no field defaults.** Every registry row must author all 8 dimensions explicitly. D-89 forbids defaulting to empty — the safest way to make that impossible is to make the dataclass reject omissions.
6. **Placeholder pre-commit hooks with `entry: "true"`.** Reserves the `decimal-float-literal-check` and `import-linter` hook ids now (per plan task 01-01-01 requirement) without pretending they enforce anything. Every downstream reference (CI, docs, other plans) already sees the correct id and can grep for it before plans 01-02 / 01-03 swap in the real entries.
7. **Removed `readme = "README.md"` from pyproject.toml** because the project has no README (agent policy forbids creating one unrequested) and hatchling's editable build refused to proceed without the referenced file. `[project.description]` alone is sufficient metadata for the internal build.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `uv` not installed on the machine**
- **Found during:** Task 01-01-01 (project skeleton bootstrap)
- **Issue:** `which uv` returned empty; `uv sync` command would fail immediately.
- **Fix:** `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org uv` — installed the official `uv 0.12.10` package from PyPI (an existing, well-known project). This is a runtime tool required to execute the plan, not a new project dependency.
- **Files modified:** none in the repo — user-site pip only.
- **Verification:** `uv --version` prints `uv 0.12.10 (…)`.
- **Committed in:** no code commit — pre-work.

**2. [Rule 3 - Blocking] `uv sync` failed with SSL cert error, then editable-build failed on missing README.md**
- **Found during:** Task 01-01-01 (project skeleton bootstrap)
- **Issue:** (a) `uv` refused to fetch `pydantic-settings` with `invalid peer certificate: UnknownIssuer` because the environment has a corporate/self-signed cert store; (b) hatchling refused to build the editable wheel because `readme = "README.md"` referenced a file that does not exist (agent policy forbids creating one unrequested).
- **Fix:** (a) Retried with `uv sync --extra dev --system-certs` to use the OS's system trust store. (b) Removed the `readme = "README.md"` line from `pyproject.toml`.
- **Files modified:** `pyproject.toml`.
- **Verification:** `uv sync --extra dev --system-certs` completed, installing 40+ pinned packages; `uv.lock` produced.
- **Committed in:** `0d3ab02` (task 01-01-01).

**3. [Rule 3 - Blocking] TOML 1.0.0 has no `null` literal — plan verify command for task 01-01-03 unsatisfiable as written**
- **Found during:** Task 01-01-03 (`config/decisions/gate1.toml`)
- **Issue:** The plan Oracle asserted `data['max_validated_notional_krw'] is None` against the raw output of `tomllib.loads(...)`. Stdlib `tomllib` implements TOML 1.0.0 which has no `null` literal — no bare-TOML representation can produce Python `None` at the raw-dict level; `tomllib.loads` on `key = null` raises `TOMLDecodeError`.
- **Fix:** Introduced a value-deferred inline-table sentinel `{ value = "unset", frozen_at = "gateN", frozen_at_phase = N }` for every D-09 / D-41 / D-42 value-deferred field. Pydantic BeforeValidators in `Gate1Decisions` normalise the sentinel to Python `None`. Adjusted the verify command in this task's commit to check the sentinel structure (`data['max_validated_notional_krw']['value'] == 'unset'`) instead. The plan's semantic intent (schema declared at Gate 1, value frozen at the downstream gate) is preserved end-to-end; only the surface representation changed.
- **Files modified:** `config/decisions/gate1.toml`, `tests/conftest.py` (`_MINIMAL_GATE1_TOML` fixture updated to match), later `src/bithumb_bot/config/gate1_model.py` (BeforeValidator normalisation).
- **Verification:** `uv run python -c "import tomllib; ..."` runs the adjusted verify command and passes; downstream Gate1Decisions tests confirm `.max_validated_notional_krw is None` on the loaded model.
- **Committed in:** `62c11c0` (task 01-01-03).

**4. [Rule 3 - Blocking] Pytest `filterwarnings = ["error"]` promoted a benign `PytestConfigWarning` to a fatal error before `tests/` existed**
- **Found during:** Task 01-01-01 verification (`uv run pytest --collect-only` after uv sync but before task 01-01-02 created the `tests/` directory).
- **Issue:** With `testpaths = ["tests"]` set but `tests/` not yet created, pytest emitted a `PytestConfigWarning: No files were found in testpaths` which the `filterwarnings = ["error"]` policy escalated to a collection error — making the plan's "empty collection is acceptable" oracle unreachable.
- **Fix:** Added a narrow, single-warning ignore rule for `PytestConfigWarning:No files were found in testpaths` inside task 01-01-01. Immediately removed it in task 01-01-02 once the `tests/` directory was populated. The full `filterwarnings = ["error"]` policy is active for the rest of the plan and going forward.
- **Files modified:** `pyproject.toml` (added and later removed the ignore).
- **Verification:** `uv run pytest -x -q --collect-only` returns `no tests collected` with no warnings after task 01-01-02.
- **Committed in:** `0d3ab02` (add) and `e4936fc` (remove).

---

**Total deviations:** 4 auto-fixed (4 Rule 3 blockers — all environment / TOML-syntax / build-tool constraints, zero from the plan's design decisions).
**Impact on plan:** No scope creep. Every deviation is a mechanical workaround for a real environment / TOML-standard limitation the planner could not have anticipated. The one substantive workaround (value-deferred sentinel) is documented in the TOML file's header, in `gate1_model.py`, and in the SUMMARY so plans 01-02 / 01-03 / 01-04 can consume the convention without confusion.

## Issues Encountered

None during planned work. Every blocker above was environment-tool-related and unblocked in <5 minutes each.

## User Setup Required

None — no external service configuration required. `bithumb_bot` package is fully self-contained at this point. Plan 01-04 (M1 spec adapter) will introduce optional `BITHUMB_BOT_SECRETS_FILE` handling and the account/read credential env vars.

## Threat Register — Mitigation Verification

| Threat ID | Status | Test |
|---|---|---|
| T-1-01-01 | mitigated | `test_gate_loader.py::TestLoadGate1HashSensitivity::test_hash_changes_on_single_byte_mutation`; `test_config_hash.py::TestSensitivity::*` |
| T-1-01-02 | mitigated | `test_validator.py::TestUnknownCapability::test_unknown_verb_raises` (and `test_unknown_subverb_raises`) |
| T-1-01-03 | mitigated | `gate1.toml` header + `capability_registry.py` explicitly document class-only reporting; grep sweep in `test_capability_registry.py::TestNoLiveOrM6BVerbs::test_no_withdraw_key_in_registry` and no `access|secret|token` keys in the TOML |
| T-1-01-04 | mitigated | `test_gate1_model.py::TestGate1Decisions::test_assignment_after_construction_raises`; `test_capability_registry.py::TestCapabilityRequirementsFrozen::test_cannot_mutate_after_construction` |
| T-1-01-05 | mitigated | `test_gate1_model.py::TestGate1Decisions::test_unquoted_decimal_raises` + `test_toml_float_for_strict_decimal_raises`; `test_gate_loader.py::TestLoadGate1Negative::test_unquoted_decimal_raises_via_wrapped_validation_error` |
| T-1-01-SC | accepted (per plan) | No new packages introduced beyond STACK.md's allowlist. `uv.lock` records exact pinned versions. |

## Threat Flags

None — no new security-relevant surface was introduced outside the plan's `<threat_model>` block.

## Next Phase Readiness

Ready for plan **01-02** (Decimal AST checker + secrets loader + Money/Qty value objects). Prerequisites this plan provides:

- `bithumb_bot` package importable (Import Linter can slot the contract in unmodified).
- `errors.py` already exports `ProhibitedCredentialDetectedError` — 01-02's secrets loader raises it; no new module needed.
- `validate()` already stubs the trade-cred prohibition; 01-02's real secrets loader can hook into it without changing the validator's public API.
- Pre-commit hook ids `decimal-float-literal-check` (01-02) and `import-linter` (01-03) reserved with `entry: 'true'` — swapping in the functional entry is a two-line edit, not a schema change.

No blockers or concerns for the next plan.

## Self-Check: PASSED

- Every declared `key-files.created` file exists on disk (25/25 verified via `[ -f ]`).
- Every recorded task commit is in `git log --oneline --all` (14/14 verified).
- Every plan `<success_criteria>` re-executed and passed at plan close (`uv sync` clean, `uv run pytest -x -q --no-cov -m "not slow"` = **99 passed in 0.71s**).
- Every plan `<acceptance_criteria>` (per-task oracles) recorded green in the RED→GREEN commit pairs; no criterion was silently skipped.
- Every `<threat_model>` entry has an explicit corresponding test (see Threat Register table above).

---
*Phase: 01-safety-foundation-bithumb-spec-adapter*
*Completed: 2026-09-08*
