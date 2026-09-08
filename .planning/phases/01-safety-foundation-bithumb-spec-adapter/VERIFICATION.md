---
phase: 01-safety-foundation-bithumb-spec-adapter
verified: 2026-09-08T11:50:26Z
status: passed
score: 12/12 checklist items verified
behavior_unverified: 0
overrides_applied: 0
---

# Phase 1: Safety Foundation + Bithumb Spec Adapter — Verification Report

**Phase Goal (ROADMAP.md §Phase 1):** The project has fail-closed safety rails, a documented immutable Decision Register, a three-class API key policy with withdrawal permanently disabled, and an authenticated read-only Bithumb spec/fee adapter producing a hashed snapshot the simulator will consume.

**Requirements in scope:** SAFE-01 … SAFE-07, SPEC-01 … SPEC-05 (12 IDs, all marked Complete in `.planning/REQUIREMENTS.md`).
**Verdict:** PASS
**Verifier:** Claude (gsd-verifier), goal-backward mode, read-only.

---

## Overall Result

| Bucket | Count |
| --- | --- |
| Must-have checklist items VERIFIED | **12** |
| Must-have checklist items FAILED | 0 |
| Must-have checklist items UNCERTAIN | 0 |
| Spot-checks (sentinel + OVIs + D-97) VERIFIED | 3 |
| Requirements CI-marked Complete AND covered by evidence | 12 / 12 |

Full pytest suite result (item 12): **466 passed, 1 skipped**, 18.09 s. The single skip is the documented Windows-symlink test (`tests/secrets/test_loader.py:83 — Symlink creation on Windows requires developer mode / admin`).

Every static-gate command exit code observed in this run matches the goal-backward contract in `01-VALIDATION.md` (`lint-imports` → 0 on real tree, non-zero on the committed negative fixture; `python -m tools.decimal_ast_check src tools` → 0).

---

## Checklist — Command / Actual Output / Status

### 1. `bt config validate --through gate1` succeeds on committed gate1.toml; corrupted fixture fails closed

```text
$ uv run --no-sync --extra dev bt config validate --through gate1
gate: gate1
status: frozen
schema_version: 1
source_commit: 000000000000
research_spec_sha256: 000000000000
execution_spec_sha256: 000000000000
gate1_file_sha256: 6f7b128a12d5
provisional_engineering_notional_krw: 100000
value-deferred fields awaiting Gate-2/Gate-3: 16
$ echo $? → 0

$ # Mutated fixture with status="unfrozen"
$ BITHUMB_BOT_REPO_ROOT=/tmp/badgate uv run --no-sync --extra dev bt config validate --through gate1
bt: refusal: gate1.toml at .../gate1.toml has status='unfrozen';
    only status="frozen" is accepted (D-60 fail-closed).
    (missing: gate1)
$ echo $? → 1
```

**Status:** VERIFIED. Both success + fail-closed paths behave correctly, and the refusal path reports the specific reason (`status='unfrozen'`) rather than a generic "gate1 missing" placeholder.

### 2. `bt m4 evaluate-selection` refuses because gate2.toml / gate3.toml don't exist (D-56)

```text
$ uv run --no-sync --extra dev bt m4 evaluate-selection
bt: refusal: gate2 not yet frozen — Phase 3. This file is created only
    after its approved freeze; see D-56 (Phase 1 MUST NOT create it).
    (missing: gate2)
$ echo $? → 1
```

**Status:** VERIFIED. Guard-matrix short-circuits on the first missing gate (Gate 2) rather than attempting the full chain. Reserved-verb handler is never invoked.

### 3. `bt m1 fetch-spec` works end-to-end offline via `httpx.MockTransport` and produces snapshot + fixture + verification bundle with sidecar hashes

```text
$ uv run --no-sync --extra dev pytest \
    tests/bithumb_spec/test_fetch_spec_end_to_end.py \
    tests/cli/test_handlers_m1_fetch_spec.py -q --no-cov
9 passed in 0.82s
```

The end-to-end test file wires `httpx.MockTransport` to intercept `/v1/orders/chance`, injects sentinel account/read creds via `monkeypatch`, exercises `fetch_spec(...)`, then asserts that the three artifact paths (snapshot JSON, sanitized fixture, VERIFICATION.md bundle) all exist and that `load_snapshot(...)` succeeds after sidecar-hash verification. **Status:** VERIFIED.

### 4. `lint-imports` exits 0 on real tree AND non-zero on the committed intentional-violation fixture

```text
$ PYTHONIOENCODING=utf-8 uv run --no-sync --extra dev lint-imports --no-cache
Analyzed 43 files, 62 dependencies.
Core must not import broker KEPT
Contracts: 1 kept, 0 broken.
$ echo $? → 0

$ uv run --no-sync --extra dev pytest tests/import_boundary/ -q --no-cov
3 passed in 0.84s
```

`tests/import_boundary/test_import_linter_contract.py` covers both `test_real_tree_passes` (exit 0 on repo tree) and `test_negative_fixture_fails` (non-zero exit against `tests/fixtures/import_linter_violation/pyproject.toml`; asserts the contract name appears in stdout AND that fixture files are not mutated). **Status:** VERIFIED.

**Windows note:** direct `lint-imports` without `PYTHONIOENCODING=utf-8` emits a `UnicodeEncodeError` in the Rich renderer on the default cp949 console — this is a display-layer issue only; the contract itself is KEPT (verified by re-running with UTF-8 forced). The test module documents this and sets `PYTHONIOENCODING=utf-8` on the child subprocess environment (`test_import_linter_contract.py:63-64`).

### 5. `tools/decimal_ast_check.py` exits 0 on real src + non-zero on committed positive fixtures

```text
$ uv run --no-sync --extra dev python -m tools.decimal_ast_check src/bithumb_bot
$ echo $? → 0

$ uv run --no-sync --extra dev python -m tools.decimal_ast_check src tests tools
tests\tools\decimal_ast\positive\basic_float.py:6:4: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\complex_literal.py:4:4: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\decimal_module.py:4:4: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\module_alias.py:4:4: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\name_alias_D.py:4:4: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\nested\deeper\violation.py:5:19: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\signed_float.py:5:6: DECIMAL_FROM_FLOAT
tests\tools\decimal_ast\positive\signed_float.py:6:6: DECIMAL_FROM_FLOAT
$ echo $? → 1

$ uv run --no-sync --extra dev pytest tests/tools/ -q --no-cov
19 passed in 3.02s
```

Real code paths are clean (exit 0); every committed positive fixture is flagged; the fixture-suite tests all green. **Status:** VERIFIED.

### 6. `Gate1Decisions` is frozen (mutation raises; unknown key raises; unquoted decimal raises)

```text
$ uv run --no-sync --extra dev pytest \
    tests/config/test_gate1_model.py tests/config/test_gate_loader.py -q --no-cov
30 passed in 0.61s

$ uv run --no-sync --extra dev python -c "…"
type= Gate1Decisions
frozen config= {'frozen': True, 'extra': 'forbid', 'strict': True}
mutation raised: ValidationError
unknown key raised: ValidationError
float raised: ValidationError
```

`gate1_model.py:186-190` declares `frozen=True, extra="forbid", strict=True`. Live-invocation trace confirms every one of the three failure modes raises `pydantic.ValidationError`, matching D-60's fail-closed contract. **Status:** VERIFIED.

### 7. `BithumbSecrets` rejects `BITHUMB_TRADE_*` env with `ProhibitedCredentialDetectedError`; withdrawal-permission field absent

```text
$ uv run --no-sync --extra dev pytest tests/secrets/ -q --no-cov
39 passed, 1 skipped in 0.59s   (the skip is the documented Windows-symlink case)

$ BITHUMB_TRADE_ACCESS_KEY=deadbeef BITHUMB_TRADE_SECRET_KEY=cafebabe \
    uv run --no-sync --extra dev bt m0 selfcheck
bt: refusal: Trade credential class detected in environment ('trade') —
    refused (D-68 / D-97). Value not inspected.
    (missing: trade_credential_prohibited)
$ echo $? → 1
```

Refusal message reports only the credential *class* (`'trade'`) — the value is never spliced into the exception text (D-70). **Withdrawal grep** shows all four `withdraw` matches under `src/bithumb_bot/` are D-69 *negation* citations (settings.py, loader.py, config_validate.py), zero `withdrawal_*` fields. **Status:** VERIFIED.

### 8. `redact_secrets` structlog processor replaces `SecretStr` values with a mask in log output

```text
$ uv run --no-sync --extra dev pytest \
    tests/secrets/test_redaction.py tests/observability/ -q --no-cov
21 passed in 0.46s

$ uv run --no-sync --extra dev python -c "…"
redacted= {'k': '***'}
OK
```

**Note on mask string:** the checklist item wording uses `[REDACTED]`; the actual implementation uses `***` (`redaction.py:42 _MASK = "***"`). Intent (raw SecretStr value never leaks to logs) is fully satisfied; the mask literal is a naming choice, not a semantic gap. **Status:** VERIFIED.

### 9. No `Decimal(<float-literal>)` occurrences anywhere in `src/bithumb_bot/`

```text
$ uv run --no-sync --extra dev python -m tools.decimal_ast_check src/bithumb_bot
$ echo $? → 0
```

**Status:** VERIFIED.

### 10. No `config/decisions/gate2.toml` or `config/decisions/gate3.toml` exists on disk (D-56)

```text
$ ls config/decisions/
gate1.toml
```

Only `gate1.toml` exists. D-56 discipline held. **Status:** VERIFIED.

### 11. No trade-permission credential class is created, referenced by real code, or requested

```text
$ grep -R BITHUMB_TRADE_ src/
src\bithumb_bot\secrets\loader.py:54:    "BITHUMB_TRADE_ACCESS_KEY",
src\bithumb_bot\secrets\loader.py:55:    "BITHUMB_TRADE_SECRET_KEY",
src\bithumb_bot\config\validator.py:71:    "BITHUMB_TRADE_ACCESS_KEY",
src\bithumb_bot\config\validator.py:72:    "BITHUMB_TRADE_SECRET_KEY",
src\bithumb_bot\secrets\settings.py:45:        trade_access_key  ← BITHUMB_TRADE_ACCESS_KEY (prohibited pre-M6B)
src\bithumb_bot\secrets\settings.py:46:        trade_secret_key  ← BITHUMB_TRADE_SECRET_KEY (prohibited pre-M6B)
src\bithumb_bot\secrets\__init__.py:6: fields (`BITHUMB_ACCOUNT_READ_*`, `BITHUMB_TRADE_*`) per D-67
```

Every `BITHUMB_TRADE_*` reference is inside code that *rejects* the credential class (loader's `reject_trade_credentials`, validator's `_check_trade_credential_prohibition`, or documentation stating "prohibited pre-M6B"). No real code loads, uses, or requests a trade credential. Runtime proof: item 7's `bt m0 selfcheck` invocation with the env vars set refuses at the dispatcher — the value is never inspected. **Status:** VERIFIED.

### 12. Full repo test suite green — 466+ pass, ≤ 1 documented skip

```text
$ PYTHONIOENCODING=utf-8 uv run --no-sync --extra dev pytest -q --no-cov
466 passed, 1 skipped in 18.09s
SKIPPED [1] tests\secrets\test_loader.py:83:
    Symlink creation on Windows requires developer mode / admin.
```

**Status:** VERIFIED. Exactly the promised numbers.

---

## Spot-Checks

### A. TOML sentinel deviation `{value="unset", frozen_at="gateN", frozen_at_phase=N}` present + normalized to `None`

```text
$ grep -c 'value = "unset"' config/decisions/gate1.toml
17

$ python -c "from bithumb_bot.config.gate_loader import load_gate1; …"
max_validated_notional_krw= None
watchdog_lease_ttl_ms= None
sentinel-normalization OK
```

17 sentinel inline-tables (16 value-deferred fields + `max_validated_notional_krw` per D-09). BeforeValidator normalizes each to `None` per `_optional_strict_decimal_from_input` / `_optional_int_from_input` / `_optional_str_from_input` in `gate1_model.py`. **Status:** VERIFIED.

### B. Open Verification Items #1 (JWT timestamp shape) and #3 (per-channel rate-limit numeric values) flagged with `TODO(M1-verify)` in code

```text
$ grep -n TODO\(M1-verify\) src/
src\bithumb_bot\bithumb_spec\jwt_auth.py:33:  TODO(M1-verify): Open Verification Item #1 — confirm whether
src\bithumb_bot\bithumb_spec\jwt_auth.py:116: TODO(M1-verify): Open Verification Item #1 — confirm the
src\bithumb_bot\bithumb_spec\rate_limits.py:23: TODO(M1-verify): swap `capacity` / `refill_rate` for the observed
src\bithumb_bot\bithumb_spec\client.py:96:  TODO(M1-verify)
src\bithumb_bot\bithumb_spec\snapshot.py:194: TODO(M1-verify): per-price-band structure is Open Verification
src\bithumb_bot\bithumb_spec\snapshot.py:209: TODO(M1-verify): The `/v1/orders/chance` response does not (per
```

OVI #1 anchored in `jwt_auth.py` (two anchors — the class-level docstring and the `include_timestamp` parameter default). OVI #3 anchored in `rate_limits.py:23`. Both open items are also referenced in `01-04-PLAN.md`, `01-04-SUMMARY.md`, `01-VALIDATION.md`, and `01-RESEARCH.md`. **Status:** VERIFIED.

**Bundle location note.** The `verification/` directory at the repo root does not yet exist. This is correct: per `01-VALIDATION.md`'s Manual-Only Verifications table (SPEC-01 row), the human-approved bundle is produced by the operator running `bt m1 fetch-spec` against real `apidocs.bithumb.com` — an explicitly manual, out-of-CI step (D-79: "CI never calls Bithumb"). The code path is fully in place and exercised offline via MockTransport (item 3 above); operator execution is scheduled for M1 handoff and is not a Phase-1 automation gap.

### C. Debt-marker gate — every `TODO`/`FIXME`/`XXX` in modified files references formal follow-up

```text
$ grep -n 'TODO\|FIXME\|XXX' src/bithumb_bot/**
```

All matches reference either `TODO(M1-verify)` (open-verification items with formal M1 anchor) or `TODO(plan-01-04)` (plan-linked follow-up in `dispatcher.py:346`). No unreferenced debt markers. **Status:** VERIFIED (no BLOCKER under the debt-marker gate).

---

## Requirements Coverage

| Req | Description (abridged) | Status | Evidence |
| --- | --- | --- | --- |
| SAFE-01 | Config loader holds Decision Register as immutable typed values | SATISFIED | Item 6 + `tests/config/test_gate1_model.py` (frozen + extra=forbid + StrictDecimal) |
| SAFE-02 | Startup self-check (per D-99: capability-scoped validator) refuses on missing prereqs | SATISFIED | Items 1, 2, 7 + `tests/config/test_validator.py`, `tests/cli/test_handlers_m0_selfcheck.py` |
| SAFE-03 | Three-class API key policy; no key has withdrawal permission | SATISFIED | Item 7 + D-69 grep sweep; `BithumbSecrets` has exactly four fields (all account_read + trade), zero withdrawal_* fields |
| SAFE-04 | Secrets from env/secret store only; symlink-into-repo rejected; ambiguous config rejected | SATISFIED | `tests/secrets/test_loader.py` (39 passed + 1 doc-skip); `SECRETS_FILE_ENV` outside-repo enforcement in `loader.py:_resolve_secrets_file` |
| SAFE-05 | Exact-decimal money; Decimal(<float-literal>) blocked | SATISFIED | Items 5, 9 + `tools/decimal_ast_check.py` positive+negative fixtures |
| SAFE-06 | `core/` never imports from `broker/`; CI-checked | SATISFIED | Item 4 (real tree KEPT, negative fixture BROKEN) |
| SAFE-07 | Risk-denominator vocabulary documented | SATISFIED | `src/bithumb_bot/core/money.py` NewTypes + `bt m0 selfcheck` prints five risk denominators (`tests/cli/test_handlers_m0_selfcheck.py`) |
| SPEC-01 | Account/read JWT spec adapter fetches fees, min-order, tick/step, order types | SATISFIED | Item 3 + `tests/bithumb_spec/test_fetch_spec_end_to_end.py`, `test_jwt_auth.py`, `test_http_client_retry.py` |
| SPEC-02 | Fees recorded per experiment | SATISFIED | `SnapshotV1` fee_rates field + sidecar hash recorded per snapshot (`tests/bithumb_spec/test_snapshot_build.py`) |
| SPEC-03 | Tick/step + minimum-order boundary tests | SATISFIED | `tests/core/test_rounding.py` + `tests/core/test_money.py` (hypothesis-driven boundary + idempotence) |
| SPEC-04 | Fee/tick/min-order snapshot persisted as hashed artifact | SATISFIED | `bithumb_spec/snapshot.py` atomic-write + sidecar; `tests/bithumb_spec/test_snapshot_load.py` tamper-detection |
| SPEC-05 | Per-channel token bucket for public REST / private REST / WebSocket | SATISFIED | `bithumb_spec/rate_limits.py` three-bucket sentinel; `tests/rate_limit/`, `tests/bithumb_spec/test_rate_limits.py` |

All 12 requirements have verified codebase evidence beyond the checkbox in `REQUIREMENTS.md`.

---

## Anti-Pattern Scan

| Category | Count | Notes |
| --- | --- | --- |
| Unreferenced `TODO` / `FIXME` / `XXX` markers | 0 | Every marker in Phase-1 files cites either `M1-verify` (documented OVI) or `plan-01-04` (documented plan follow-up). Debt-marker gate CLEAN. |
| Empty implementations in Phase-1 files that render / return live data | 0 | Reserved handlers (D-87) intentionally raise — that IS the D-90 discipline (not a stub). Real handlers exercised by tests in items 1-3 return substantive output. |
| `Decimal(<float>)` occurrences | 0 in `src/` | Confirmed by item 9. |
| `withdrawal_*` fields / paths | 0 | Confirmed by D-69 static sweep in item 7. All `withdraw*` matches are negation-only citations. |
| `console.log` / bare `print` in place of structured logging | Non-issue for Phase 1 | CLI handlers legitimately print to stdout for operator-facing output; structlog is wired for the audit trail (`main.py:configure_logging`, `secrets/redaction.py`). |

No blockers, no warnings.

---

## Human Verification

None required for Phase-1 automation surface.

**One recurring manual step is scheduled elsewhere** (already documented in `01-VALIDATION.md § Manual-Only Verifications`): the operator runs `bt m1 fetch-spec --market KRW-BTC` against real `apidocs.bithumb.com` once, reviews the sanitized fixtures + generated `verification/bithumb/<ts>/VERIFICATION.md`, marks each of the five build-time facts, and commits the bundle. That step is intrinsically manual per D-79 and does not gate Phase-1 verification.

---

## Notes for Downstream Phases

- **Provenance hashes are still zero-placeholders** in `config/decisions/gate1.toml` (`source_commit = "000…"`, `research_spec_sha256 = "000…"`, `execution_spec_sha256 = "000…"`). This is called out in the TOML header itself: "updated when the Decision Register is countersigned at Phase-1 close." Not a Phase-1 gap by the SUMMARY contract, but Phase 5's FRZ-02 will need real values here before the final `config_hash` manifest is computed.
- **Zero `.env`, no `config.json` credential fields, no committed secrets.** Manual-verification row in `01-VALIDATION.md` for SAFE-04 remains an operator sign-off item at phase close.

---

_Verified: 2026-09-08T11:50:26Z_
_Verifier: Claude (gsd-verifier), goal-backward mode_
_Read-only: no source code, test, or SUMMARY file was modified during verification._
