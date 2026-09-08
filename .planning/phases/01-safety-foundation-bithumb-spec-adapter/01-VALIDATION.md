---
phase: 1
slug: safety-foundation-bithumb-spec-adapter
# status lifecycle: draft (seeded by plan-phase) → validated (set by validate-phase §6)
# audit-milestone §5.5 distinguishes NOT-VALIDATED (draft) from PARTIAL (validated + nyquist_compliant: false) (#2117)
status: draft
nyquist_compliant: false
wave_0_complete: true  # Wave-0 specified in plan 01-01 tasks 01-01-01 and 01-01-02; scaffold lands when those tasks execute
created: 2026-09-08
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Sampling matrix authored in `01-RESEARCH.md` §12 (Validation Architecture); this file is the executable contract the planner and executor consume.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + hypothesis (per `.planning/research/STACK.md`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` — Wave 0 installs |
| **Quick run command** | `uv run pytest -x -q --no-cov -m "not slow"` |
| **Full suite command** | `uv run pytest --cov=src --cov-report=term-missing` |
| **Static-gate command** | `uv run lint-imports && uv run python -m tools.decimal_ast_check src tests tools` |
| **Estimated runtime** | ~30 seconds (quick) / ~90 seconds (full) — Phase 1 has no I/O-bound suites |

---

## Sampling Rate

- **After every task commit:** Run quick command + static-gate command
- **After every plan wave:** Run full suite command
- **Before `/gsd-verify-work`:** Full suite must be green; pre-commit + CI both pass import-boundary and Decimal-AST checks (per D-73)
- **Max feedback latency:** 30 seconds (quick) / 90 seconds (full)

---

## Per-Task Verification Map

> Populated by the planner as it emits PLAN.md files. One row per atomic task. The `Requirement` column maps to REQUIREMENTS.md IDs; the `Threat Ref` column maps to the PLAN.md `<threat_model>` block. `File Exists` starts ❌ W0 for any file created by Wave 0.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01-01 | 0 | infrastructure | — | Package `bithumb_bot` importable; `pyproject.toml` skeleton with `[tool.importlinter] root_package="bithumb_bot"` | integration (uv sync + import) | `uv sync && uv run python -c "import bithumb_bot; import bithumb_bot.core; import bithumb_bot.broker; import bithumb_bot.cli; import bithumb_bot.config" && uv run pytest -x -q --collect-only` | ❌ W0 | ⬜ pending |
| 01-01-02 | 01-01 | 0 | infrastructure | — | Shared fixtures load without ImportError; pytest collects | integration (collect-only) | `uv run pytest -x -q --collect-only` | ❌ W0 | ⬜ pending |
| 01-01-03 | 01-01 | 1 | SAFE-01 | T-1-01-01, T-1-01-03, T-1-01-05 | `gate1.toml` parses; D-40 numeric HTTP values recorded; D-41/D-42 fields null-with-comment; no credential-looking key | schema | `uv run python -c "import tomllib; import pathlib; data = tomllib.loads(pathlib.Path('config/decisions/gate1.toml').read_text()); assert data['status'] == 'frozen'; assert data['schema_version'] == 1; assert 'provisional_engineering_notional_krw' in data; assert data['m1_spec_http_connect_timeout_ms'] == 5000; assert data['max_validated_notional_krw'] is None"` | ❌ W0 | ⬜ pending |
| 01-01-04 | 01-01 | 1 | SAFE-01 | T-1-01-04, T-1-01-05 | `Gate1Decisions` frozen + extra=forbid + StrictDecimal; float-in-decimal-field rejected | unit + negative | `uv run pytest tests/config/test_gate1_model.py -x -q` | ❌ W0 | ⬜ pending |
| 01-01-05 | 01-01 | 1 | SAFE-01 (D-58, D-61, D-62) | T-1-01-01 | `load_gate1()` binary-mode + hash-of-bytes provenance; hash byte-stable | unit + hash sensitivity | `uv run pytest tests/config/test_gate_loader.py -x -q` | ❌ W0 | ⬜ pending |
| 01-01-06 | 01-01 | 2 | SAFE-02 (per D-99) | T-1-01-02 | Registry table matches D-88; every D-87 verb reserved with NotImplementedError handler | unit (parametrized per row) | `uv run pytest tests/config/test_capability_registry.py -x -q` | ❌ W0 | ⬜ pending |
| 01-01-07 | 01-01 | 2 | SAFE-02 (per D-99) | T-1-01-01, T-1-01-02 | `validate()` fails closed on missing prereqs; unknown capability raises | unit (parametrized per Phase-1 row + negative) | `uv run pytest tests/config/test_validator.py -x -q` | ❌ W0 | ⬜ pending |
| 01-01-08 | 01-01 | 2 | SAFE-01, D-62 | T-1-01-01 | `config_hash` manifest byte-stable; single-hex-char change → different hash | unit + hypothesis | `uv run pytest tests/config/test_config_hash.py -x -q` | ❌ W0 | ⬜ pending |
| 01-01-09 | 01-01 | 2 | SAFE-01, SAFE-02 (per D-99) | T-1-01-01, T-1-01-02 | Composed wave-1+2 smoke: load real gate1.toml, validate() Phase-1 verbs, config_hash stability | integration | `uv run pytest tests/config -x -q` | ❌ W0 | ⬜ pending |
| 01-02-01 | 01-02 | 1 | SAFE-05 | T-1-02-04 | Repo-owned AST checker flags Decimal(<float>) with alias tracking; runs from `python -m tools.decimal_ast_check` | code (runnable) | `uv run python -m tools.decimal_ast_check tools src` | ❌ | ⬜ pending |
| 01-02-02 | 01-02 | 1 | SAFE-05 | T-1-02-04 | Positive fixtures flagged; negative fixtures not flagged; syntax error → exit 2; nested-dir walk works | fixture suite | `uv run pytest tests/tools/test_decimal_ast_check.py -x -q` | ❌ | ⬜ pending |
| 01-02-03 | 01-02 | 1 | SAFE-05, D-73 | T-1-02-04 | Pre-commit hook `decimal-float-literal-check` functional; runs across tree with test-fixture excludes | pre-commit | `uv run pre-commit run decimal-float-literal-check --all-files` | ❌ | ⬜ pending |
| 01-02-04 | 01-02 | 2 | SAFE-03, SAFE-04 | T-1-02-01, T-1-02-03, T-1-02-06 | Exactly four credential fields; SecretStr masks repr; no withdrawal field | unit | `uv run pytest tests/secrets/test_settings.py -x -q` | ❌ | ⬜ pending |
| 01-02-05 | 01-02 | 2 | SAFE-04, SAFE-03 | T-1-02-01, T-1-02-02, T-1-02-03, T-1-02-05 | Symlink-into-repo rejected; ambiguous mixed rejected; trade-cred rejected with class only | unit + integration hook | `uv run pytest tests/secrets/test_loader.py tests/config/test_validator.py -x -q` | ❌ | ⬜ pending |
| 01-02-06 | 01-02 | 2 | SAFE-04, D-70 | T-1-02-01 | `redact_secrets` processor replaces SecretStr with `***`; belt-and-suspenders assertion | unit + capture_logs | `uv run pytest tests/secrets/test_redaction.py -x -q` | ❌ | ⬜ pending |
| 01-02-07 | 01-02 | 2 | SAFE-05, SAFE-07 | T-1-02-04 | Money/Qty float-construction raises; risk-denominator NewType vocabulary documented | unit + hypothesis + mypy | `uv run pytest tests/core/test_money.py -x -q && uv run python -m tools.decimal_ast_check src/bithumb_bot/core/money.py` | ❌ | ⬜ pending |
| 01-02-08 | 01-02 | 2 | SAFE-05, SPEC-03 | T-1-02-04 | Rounding helpers direction-correct per D-49/D-50/D-51; boundary + idempotence property tests | unit + hypothesis | `uv run pytest tests/core/test_rounding.py -x -q` | ❌ | ⬜ pending |
| 01-02-09 | 01-02 | 2 | SAFE-03, SAFE-04, D-69 | T-1-02-06 | Static sweep: no `withdraw` string outside D-69 citations; integration smoke of loader+validator | static + integration | `uv run pytest tests/secrets -x -q` | ❌ | ⬜ pending |
| 01-03-01 | 01-03 | 1 | SAFE-06 | T-1-03-01 | `[[tool.importlinter.contracts]]` block active in pyproject.toml; real project passes | contract check | `uv run lint-imports` | ❌ | ⬜ pending |
| 01-03-02 | 01-03 | 1 | SAFE-06, D-73 | T-1-03-01 | Fixture `badpkg` proves contract mechanically fails; real project passes | subprocess (positive + negative) | `uv run pytest tests/import_boundary -x -q` | ❌ | ⬜ pending |
| 01-03-03 | 01-03 | 1 | SAFE-06, D-73 | T-1-03-01 | Pre-commit hook `import-linter` functional (language: system, project-wide) | pre-commit | `uv run pre-commit run import-linter --all-files` | ❌ | ⬜ pending |
| 01-03-04 | 01-03 | 2 | SAFE-02 surface (D-99), D-85 | T-1-03-04 | `bt` console script installed | integration | `uv run which bt` | ❌ | ⬜ pending |
| 01-03-05 | 01-03 | 2 | SAFE-02 surface, D-85, D-89 | T-1-03-02, T-1-03-04, T-1-03-05, T-1-03-06 | Argparse dispatcher validate-before-dispatch; help/version load no cred; unknown verb fails hard | unit + subprocess | `uv run pytest tests/cli/test_dispatcher.py -x -q` | ❌ | ⬜ pending |
| 01-03-06 | 01-03 | 2 | SAFE-01, SAFE-02 surface, D-86, D-89 | T-1-03-02 | `bt config validate --through gate1` prints summary + hash prefix; never marks approved | unit | `uv run pytest tests/cli/test_handlers_config_validate.py -x -q` | ❌ | ⬜ pending |
| 01-03-07 | 01-03 | 2 | SAFE-02 (per D-99), SAFE-07, D-86 | T-1-03-02, T-1-03-04, T-1-03-06 | `bt m0 selfcheck` prints resolved decisions + key class + five risk-denominators; no credential value leaked | unit + capture stdout | `uv run pytest tests/cli/test_handlers_m0_selfcheck.py -x -q` | ❌ | ⬜ pending |
| 01-03-08 | 01-03 | 2 | D-87, D-90 | T-1-03-03, T-1-03-05 | Every D-87 verb registered but refuses uniformly; no side effect | unit (parametrized) | `uv run pytest tests/cli/test_reserved_handlers.py -x -q` | ❌ | ⬜ pending |
| 01-03-09 | 01-03 | 2 | D-85 | T-1-03-02 | Handler functions call `validate()` FIRST when invoked directly from Python (defense in depth) | unit (mock.patch) | `uv run pytest tests/cli/test_defense_in_depth.py -x -q` | ❌ | ⬜ pending |
| 01-04-01 | 01-04 | 1 | SPEC-04 | T-1-04-03, T-1-04-07, T-1-04-09 | Canonical bytes deterministic; atomic_write same-dir temp; guard_against_overwrite refuses matching / renames mismatching; colon-free timestamps | unit + hypothesis | `uv run pytest tests/artifact -x -q` | ❌ | ⬜ pending |
| 01-04-02 | 01-04 | 1 | SPEC-05 | T-1-04-05 | TokenBucket refills correctly under fake clock; asyncio.sleep never called for non-zero real duration | unit + fake clock | `uv run pytest tests/rate_limit -x -q` | ❌ | ⬜ pending |
| 01-04-03 | 01-04 | 1 | SAFE-04, D-70, D-85 audit trail | T-1-04-01 | structlog wired with `redact_secrets`; capability contextvars bound; SecretStr never appears in captured logs | unit + capture_logs | `uv run pytest tests/observability -x -q` | ❌ | ⬜ pending |
| 01-04-04 | 01-04 | 2 | SPEC-01 | T-1-04-01, T-1-04-08 | JWT known-answer round-trip; `include_timestamp=False` default per Open Verification Item #1; secret sentinel absent from any exception text | unit + known-answer | `uv run pytest tests/bithumb_spec/test_jwt_auth.py -x -q` | ❌ | ⬜ pending |
| 01-04-05 | 01-04 | 2 | SPEC-01, SPEC-05 | T-1-04-05 | Retry-loop D-40 semantics: retryable statuses / exceptions retried up to max; non-retryable 4xx returned immediately; `Retry-After` honored | unit + mock transport | `uv run pytest tests/bithumb_spec/test_http_client_retry.py -x -q` | ❌ | ⬜ pending |
| 01-04-06 | 01-04 | 2 | SPEC-01, D-77 | T-1-04-02 | Response schema StrictDecimal + extra=forbid; sanitizer is allowlist-only; forbidden keys never appear in sanitized output | unit + fixture | `uv run pytest tests/bithumb_spec/test_schemas.py tests/bithumb_spec/test_sanitize.py -x -q` | ❌ | ⬜ pending |
| 01-04-07 | 01-04 | 2 | SPEC-02, SPEC-04, D-75, D-79, D-80..D-83, D-100 | T-1-04-03, T-1-04-09 | SnapshotV1 D-75 shape; verification_status keys per D-83; load_snapshot fails on sidecar mismatch / missing required status; write refuses to overwrite consumed snapshot | unit + round-trip + tamper | `uv run pytest tests/bithumb_spec/test_snapshot_build.py tests/bithumb_spec/test_snapshot_load.py -x -q` | ❌ | ⬜ pending |
| 01-04-08 | 01-04 | 2 | SPEC-01, D-78, D-84 | T-1-04-06 | VERIFICATION.md scaffold has all 5 facts unchecked; uses "human-approved" verbatim; "human-signed" absent codebase-wide; unresolved fact refuses | unit + parser + static grep | `uv run pytest tests/bithumb_spec/test_verification_bundle.py -x -q` | ❌ | ⬜ pending |
| 01-04-09 | 01-04 | 2 | SPEC-05 | T-1-04-05 | Three per-channel bucket instances with sentinel values; docstring cites Open Verification Item #3 | unit | `uv run pytest tests/bithumb_spec/test_rate_limits.py -x -q` | ❌ | ⬜ pending |
| 01-04-10 | 01-04 | 2 | SPEC-01, SPEC-02, D-85, D-89 | T-1-04-01, T-1-04-02, T-1-04-04 | fetch_spec ephemeral cred load; validate-first; three-artifact bundle produced via MockTransport; trade cred → refusal with no client construction | end-to-end offline + mock | `uv run pytest tests/bithumb_spec/test_fetch_spec_end_to_end.py -x -q` | ❌ | ⬜ pending |
| 01-04-11 | 01-04 | 3 | SPEC-01, D-86 | T-1-04-04 | `bt m1 fetch-spec` wired via real handler; trade cred → refusal; three artifact paths printed on success | integration + mock transport | `uv run pytest tests/cli/test_handlers_m1_fetch_spec.py -x -q` | ❌ | ⬜ pending |
| 01-04-12 | 01-04 | 3 | SPEC-04, D-79, D-86 | T-1-04-03, T-1-04-08 | `bt m1 verify-facts` refuses unresolved-fact bundle; `bt m1 verify-snapshot` refuses tampered snapshot; neither loads account-read creds | unit + fixture bundles | `uv run pytest tests/cli/test_handlers_m1_verify_facts.py tests/cli/test_handlers_m1_verify_snapshot.py -x -q` | ❌ | ⬜ pending |
| 01-04-13 | 01-04 | 3 | SAFE-04, D-70, D-85 audit trail | T-1-04-01 | structlog live in `main.py`; end-to-end integration smoke: no secret value appears in captured logs; VALIDATION.md quick + static commands green | integration | `uv run pytest tests/cli/test_integration_wave3.py -x -q` | ❌ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

> Wave 0 = infrastructure and test-scaffold tasks that MUST land before any behavioral task. Populated by the planner.

- [x] `pyproject.toml` — uv-managed project skeleton with `[project]`, `[tool.pytest.ini_options]`, `[tool.importlinter]` (root only; contract block added by plan 01-03), `[tool.ruff]`, `[tool.mypy]` — specified in **plan 01-01, task 01-01-01**
- [x] `tests/conftest.py` — shared fixtures (`tmp_gate1_toml`, `sanitized_fixture_loader`, `fake_monotonic_clock`, `frozen_utc_now`) — specified in **plan 01-01, task 01-01-02**
- [x] `tools/decimal_ast_check.py` — repo-owned AST checker per D-72 + its own fixture suite under `tests/tools/decimal_ast/` — specified in **plan 01-02, tasks 01-02-01 + 01-02-02** (Wave 1 of 01-02, no dependency on plans 01-03 / 01-04 — runs in parallel with plan 01-01 Wave 2)
- [x] `.pre-commit-config.yaml` — ruff / ruff-format / mypy hooks live at plan 01-01, task 01-01-01; `decimal-float-literal-check` hook wired by plan 01-02 task 01-02-03; `import-linter` hook wired by plan 01-03 task 01-03-03

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| M1 `VERIFICATION.md` build-time facts bundle (five facts per D-78) | SPEC-01, SPEC-03 | Requires a real, one-time authenticated read call against `apidocs.bithumb.com` with the account/read JWT key; CI **never** calls Bithumb (D-79). The output is a **human-approved** evidence bundle (D-84), not a cryptographic signature. | Operator runs `bt m1 fetch-spec --market KRW-BTC`, reviews the resulting sanitized fixtures + `VERIFICATION.md`, marks each fact `confirmed` / `contradicted` / `unresolved`, commits the bundle. |
| Repository state: no committed secrets, no `.env`, no `config.json` credential fields | SAFE-04 | Static grep sweep in CI catches known credential patterns, but a human reviewer confirms no novel patterns before Phase-1 sign-off. | Reviewer runs `git ls-files | xargs grep -In -E "(BITHUMB_.*(ACCESS|SECRET))|withdrawal"` and confirms only test fixtures / documentation match. |
| Windows-safety of atomic-write / snapshot paths | SPEC-04 | Requires exercising the code on a real Windows filesystem (colons in filenames rejected, `os.replace()` atomicity across drives). CI on Windows GitHub-Actions runner covers most but not all cases. | Operator runs `bt m1 fetch-spec` locally on Windows, confirms artifact paths use `YYYYMMDDTHHMMSSZ` (no colons) and the sidecar hash validates. |

---

## Validation Sign-Off

- [ ] All Phase-1 tasks have an `<automated>` verify or Wave 0 dependency
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (Import Linter contract, Decimal-AST checker + fixtures, pre-commit config, pytest scaffold, sanitized-fixtures directory)
- [ ] No `--watch` / persistent-mode flags in any automated command
- [ ] Feedback latency < 90s (full suite)
- [ ] Both `lint-imports` and `decimal_ast_check` run in pre-commit AND CI (per D-73)
- [ ] `nyquist_compliant: true` set in frontmatter (after Phase-1 execution completes and validate-phase confirms)

**Approval:** pending
