---
phase: 1
slug: safety-foundation-bithumb-spec-adapter
# status lifecycle: draft (seeded by plan-phase) → validated (set by validate-phase §6)
# audit-milestone §5.5 distinguishes NOT-VALIDATED (draft) from PARTIAL (validated + nyquist_compliant: false) (#2117)
status: draft
nyquist_compliant: false
wave_0_complete: false
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
| _to be populated by planner_ | — | — | — | — | — | — | — | — | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

> Wave 0 = infrastructure and test-scaffold tasks that MUST land before any behavioral task. Populated by the planner.

- [ ] `pyproject.toml` — uv-managed project skeleton with `[project]`, `[tool.pytest.ini_options]`, `[tool.importlinter]`, `[tool.ruff]`, `[tool.mypy]`
- [ ] `tests/conftest.py` — shared fixtures (tmp Gate-1 TOML, sanitized-fixture loader, fake monotonic clock for token-bucket, frozen UTC for atomic-write tests)
- [ ] `tools/decimal_ast_check.py` — repo-owned AST checker per D-72 + its own fixture suite under `tests/tools/decimal_ast/`
- [ ] `.pre-commit-config.yaml` — hooks for `lint-imports` (language: system), `decimal_ast_check` (language: python), `ruff`, `ruff format`, `mypy`

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
