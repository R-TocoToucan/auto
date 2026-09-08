---
phase: 01-safety-foundation-bithumb-spec-adapter
plan: 02
subsystem: safety
tags: [pydantic-settings, secret-str, structlog, decimal, ast-lint, pre-commit, newtype, quantize, symlink-safe, credential-policy]

requires:
  - "01-01: `bithumb_bot` package tree, `errors.py`, `validate()` hook point, placeholder `decimal-float-literal-check` pre-commit id"
provides:
  - "`tools.decimal_ast_check` — repo-owned Python-AST checker with alias tracking that rejects `Decimal(<float>)` / `Decimal(<complex>)` (D-49, D-72)"
  - "Functional `decimal-float-literal-check` pre-commit hook + documented CI invocation (D-73)"
  - "`bithumb_bot.secrets.BithumbSecrets` — pydantic-settings model with exactly four `SecretStr | None` credential fields, `extra='forbid'` (D-64, D-67, D-70)"
  - "`bithumb_bot.secrets.loader.load_secrets` — symlink-safe in-repo rejection + ambiguous-mixed-source detection + env-wins precedence (D-65, D-66)"
  - "`bithumb_bot.secrets.loader.reject_trade_credentials` — class-only trade-cred refusal wired into 01-01's `validate()` for every registered capability (D-68, D-97)"
  - "`bithumb_bot.secrets.redaction.redact_secrets` — importable structlog processor masking `SecretStr` at the observability layer (D-70 defense in depth)"
  - "`bithumb_bot.core.money.Money` / `Qty` — frozen dataclass value objects; constructor rejects non-`Decimal`; `__mul__` rejects `float` (D-49, SAFE-05)"
  - "`bithumb_bot.core.money.{PlannedStopLoss, MaxMarketLoss, MaxOperationalLoss, PositionFraction, RiskPerTrade}` — five `NewType(Decimal)` risk denominators (SAFE-07)"
  - "`bithumb_bot.core.rounding.{quantize_krw_amount_down, quantize_volume_down, quantize_price_tick_up, quantize_price_tick_down}` — direction-aware quantize primitives (D-49/D-50/D-51, SPEC-03)"
  - "Static repo sweep proving no withdrawal-adjacent field/comment lands without a D-69 citation"
affects: [01-03, 01-04, 02, 04, 05]

tech-stack:
  added: []
  patterns:
    - "TDD RED→GREEN commit cadence — six paired commits across tasks 04..08; RED test commit precedes the minimal implementation commit"
    - "Repo-owned dependency-free AST checker — 229 lines of stdlib `ast` + `argparse`; runs from a fresh clone before `uv sync`"
    - "Symlink-safe path containment — `Path.resolve(strict=True)` on both sides, then `is_relative_to`; treats symlinked-into-repo targets as in-repo"
    - "Class-only credential reporting — every exception (`ProhibitedCredentialDetectedError`, `AmbiguousSecretsConfigurationError`, `SecretsFileInsideRepoError`) accepts only the class/key name; no code path can splice a `SecretStr` value into an exception, log line, or `model_dump` output"
    - "Ephemeral secrets construction — `BithumbSecrets()` documented as fresh-per-operation (D-89); the loader returns freshly-constructed instances; the validator's trade-cred check constructs, checks, and drops them all inside `_check_trade_credential_prohibition`"
    - "Adverse-direction quantize primitives — floors for user-controlled spend/sell/volume (D-49); ceil for BUY price ticks (D-50); floor for SELL price ticks (D-51). Zero coercion to `Money`/`Qty` — the primitives are context-agnostic"

key-files:
  created:
    - "tools/__init__.py"
    - "tools/decimal_ast_check.py"
    - "src/bithumb_bot/secrets/__init__.py"
    - "src/bithumb_bot/secrets/settings.py"
    - "src/bithumb_bot/secrets/loader.py"
    - "src/bithumb_bot/secrets/redaction.py"
    - "src/bithumb_bot/core/money.py"
    - "src/bithumb_bot/core/rounding.py"
    - "tests/tools/__init__.py"
    - "tests/tools/decimal_ast/__init__.py"
    - "tests/tools/decimal_ast/positive/basic_float.py"
    - "tests/tools/decimal_ast/positive/signed_float.py"
    - "tests/tools/decimal_ast/positive/module_alias.py"
    - "tests/tools/decimal_ast/positive/decimal_module.py"
    - "tests/tools/decimal_ast/positive/name_alias_D.py"
    - "tests/tools/decimal_ast/positive/complex_literal.py"
    - "tests/tools/decimal_ast/positive/nested/deeper/violation.py"
    - "tests/tools/decimal_ast/negative/string_arg.py"
    - "tests/tools/decimal_ast/negative/int_arg.py"
    - "tests/tools/decimal_ast/negative/neg_int_arg.py"
    - "tests/tools/decimal_ast/negative/string_var_arg.py"
    - "tests/tools/decimal_ast/negative/local_class_named_decimal.py"
    - "tests/tools/decimal_ast/syntax_error/broken.py.txt"
    - "tests/tools/test_decimal_ast_check.py"
    - "tests/secrets/__init__.py"
    - "tests/secrets/test_settings.py"
    - "tests/secrets/test_loader.py"
    - "tests/secrets/test_redaction.py"
    - "tests/secrets/test_no_withdrawal_credential.py"
    - "tests/secrets/test_integration.py"
    - "tests/core/__init__.py"
    - "tests/core/test_money.py"
    - "tests/core/test_rounding.py"
  modified:
    - ".pre-commit-config.yaml"
    - "src/bithumb_bot/errors.py"
    - "src/bithumb_bot/config/validator.py"

key-decisions:
  - "AST checker walks alias tables only for stdlib `decimal` — cross-file, star-import, and mid-file rebinding are documented explicit non-goals (Finding 2 + D-72 scope note preserved in the module docstring)."
  - "`Decimal.from_float(...)` is deliberately NOT flagged — it makes the float-to-Decimal conversion explicit and callers reaching for it are doing so with intent."
  - "Loader uses `settings.model_copy(update=...)` to overlay file-only credential keys on top of the env-loaded settings instead of passing `_env_file=` to `BithumbSecrets` — the merge stays under the loader's control so 'env wins' is enforced by construction (env keys are read first, then file keys skip anything already set)."
  - "`BithumbSecrets` fields are typed `SecretStr | None` (Optional). This lets the loader distinguish `unset` (None) from `present-but-empty` (SecretStr('')) — both are refused by account-read-required capabilities but with different failure reasons."
  - "`redact_secrets` walks only the top-level `event_dict` — nested `SecretStr` in a log call is left visible as a design smell rather than silently masked. Pydantic v2's own `SecretStr` masking is the primary defense; the processor is belt-to-the-suspenders for accidental raw-SecretStr log calls."
  - "`Money` / `Qty` `__post_init__` uses `type(x) is not Decimal` (identity, not `isinstance`) to reject `Decimal` subclasses that could alter arithmetic semantics — the sizing pipeline must know exactly what it's working with."
  - "Rounding primitives return only `Decimal` — never coerce to `Money` / `Qty`. Wrapping is the caller's responsibility so the helpers stay reusable across sizing / accounting / broker-adapter contexts."
  - "D-69 no-withdrawal sweep asserts every `withdraw` token in tracked `.py` / `.toml` files sits within 3 lines of a `D-69` or `no withdrawal` marker; the test file itself is excluded from the scan to avoid a false positive on its own constants."

requirements-completed: [SAFE-03, SAFE-04, SAFE-05, SAFE-07]

coverage:
  - id: D1
    description: "`tools.decimal_ast_check` — AST alias-tracker + UnaryOp-unwrap; exits 1 on findings, 2 on syntax error, 0 on clean tree"
    requirement: SAFE-05
    verification:
      - kind: integration
        ref: "uv run python -m tools.decimal_ast_check tools src (exit=0)"
        status: pass
    human_judgment: false
  - id: D2
    description: "Positive fixtures flagged; negative fixtures NOT flagged; syntax-error fixture exits 2; nested-directory scan discovers deep violations"
    requirement: SAFE-05
    verification:
      - kind: unit
        ref: "tests/tools/test_decimal_ast_check.py (19/19 pass)"
        status: pass
    human_judgment: false
  - id: D3
    description: "Functional `decimal-float-literal-check` pre-commit hook + inline CI invocation comment (D-73)"
    requirement: SAFE-05
    verification:
      - kind: config
        ref: ".pre-commit-config.yaml lines 49-59; `import-linter` placeholder still reserved for plan 01-03"
        status: pass
    human_judgment: false
  - id: D4
    description: "`BithumbSecrets` — exactly four `SecretStr | None` fields; `extra='forbid'`; `env_prefix='BITHUMB_'`; no `withdrawal_*` field"
    requirement: SAFE-03
    verification:
      - kind: unit
        ref: "tests/secrets/test_settings.py"
        status: pass
    human_judgment: false
  - id: D5
    description: "`load_secrets` — symlink-safe in-repo rejection + ambiguous-mixed detection + env-wins precedence; `reject_trade_credentials` — class-only refusal wired into `validate()`"
    requirement: SAFE-04
    verification:
      - kind: unit
        ref: "tests/secrets/test_loader.py (symlink test skipped on Windows without dev-mode)"
        status: pass
    human_judgment: false
  - id: D6
    description: "`redact_secrets` structlog processor masks top-level `SecretStr`; pydantic v2 `SecretStr` masking verified end-to-end via `model_dump()` / `model_dump_json()` / `repr` sentinel-absent asserts"
    requirement: SAFE-04
    verification:
      - kind: unit
        ref: "tests/secrets/test_redaction.py"
        status: pass
    human_judgment: false
  - id: D7
    description: "`bithumb_bot.core.money` — `Money`, `Qty`, and five `NewType(Decimal)` risk denominators; float construction rejected at every entry point (direct call, `__mul__` RHS, and `_require_decimal`); AST checker reports zero violations on the file"
    requirement: SAFE-07
    verification:
      - kind: unit
        ref: "tests/core/test_money.py + `uv run python -m tools.decimal_ast_check src/bithumb_bot/core/money.py` (exit=0)"
        status: pass
    human_judgment: false
  - id: D8
    description: "`bithumb_bot.core.rounding` — four direction-aware quantize helpers; hypothesis property tests at exact/below/above boundary, zero, and idempotence"
    requirement: SAFE-05
    verification:
      - kind: unit
        ref: "tests/core/test_rounding.py"
        status: pass
    human_judgment: false
  - id: D9
    description: "Static repo-wide sweep — every `withdraw` token in tracked `.py` / `.toml` sits within 3 lines of a `D-69` marker; `BithumbSecrets.model_fields` contains no withdrawal-adjacent name"
    requirement: SAFE-03
    verification:
      - kind: unit
        ref: "tests/secrets/test_no_withdrawal_credential.py"
        status: pass
    human_judgment: false
  - id: D10
    description: "Integration smoke composing 01-01 + 01-02: `validate(('m1','fetch-spec'))` ok=True with account/read env; ok=False + trade_credential_prohibited with trade env; credential sentinel absent from every ValidationResult surface"
    requirement: SAFE-04
    verification:
      - kind: integration
        ref: "tests/secrets/test_integration.py"
        status: pass
    human_judgment: false

duration: not_recorded_resumed_close_out
completed: 2026-09-08
status: complete
---

# Phase 1 Plan 02: Three-class key policy + secrets loader + Decimal money type + AST float-literal lint + risk-denominator vocabulary Summary

**Repo-owned Python-AST checker + pre-commit-wired `decimal-float-literal-check` hook enforce D-49 project-wide; `BithumbSecrets` exposes exactly four `SecretStr` credential fields with zero withdrawal path (D-69); `load_secrets` refuses in-repo/symlinked-in-repo secrets files (D-65) and ambiguous mixed-source configuration (D-66); `reject_trade_credentials` is wired into 01-01's `validate()` for every registered capability (D-97); `Money` / `Qty` value objects and five `NewType(Decimal)` risk denominators land the SAFE-07 vocabulary; four adverse-direction quantize primitives ship as Phase-2-ready building blocks — 121/121 plan tests green (+1 Windows-symlink skip), 220/220 total repo tests green.**

## Performance

- **Tasks:** 9
- **Files created:** 33
- **Files modified:** 3 (`.pre-commit-config.yaml`, `src/bithumb_bot/errors.py`, `src/bithumb_bot/config/validator.py`)
- **Plan test count:** 121 passed + 1 skipped (`tests/tools/` 19 + `tests/secrets/` 39+1 skip + `tests/core/` 63)
- **Full repo test count:** 220 passed + 1 skipped in 4.76s
- **Prior-agent commits landed:** 14 (`fa1ae72` … `6603974`)
- **Close-out session:** SUMMARY.md write + STATE/ROADMAP/REQUIREMENTS advancement + metadata commit (no re-execution of prior TDD cycles)

## Accomplishments

- **`tools.decimal_ast_check`** — 229-line stdlib-only AST walker (`ast` + `argparse` + `pathlib`). Tracks module aliases (`import decimal as dec`) and name aliases (`from decimal import Decimal as D`), unwraps one level of `UnaryOp(UAdd|USub, ...)`, and flags any `Call` whose callable resolves to `decimal.Decimal` with a `Constant(float|complex)` first positional argument. Exits `0` clean, `1` on findings, `2` on syntax error. Deliberate non-goals (mid-file rebinding, cross-file inference, dynamic-argument type inference, star-imports, `Decimal.from_float`) are documented at the top of the module.
- **Fixture suite for the AST checker** — 12 committed fixture files under `tests/tools/decimal_ast/{positive,negative,syntax_error,nested}/`. Positive set covers `Decimal(0.1)`, `Decimal(-0.1)`, `Decimal(+0.1)`, `decimal.Decimal(0.1)`, `dec.Decimal(0.1)` (via `import decimal as dec`), `D(0.1)` (via `from decimal import Decimal as D`), `Decimal(1j)`. Negative set covers `Decimal("0.1")`, `Decimal(1)`, `Decimal(-1)`, `Decimal(some_string_var)`, and a locally-defined `MyLocalDecimal(0.1)` that must NOT be confused with stdlib. Syntax-error fixture is a `.py.txt` file to avoid tripping pytest's own collector; the runner copies it to `<tmp>/broken.py` before invoking the checker. The runner uses `subprocess.run([sys.executable, "-m", "tools.decimal_ast_check", ...])` per plan Behavior spec.
- **Pre-commit hook wired.** `.pre-commit-config.yaml` replaced the placeholder `entry: "true"` stub with the functional `entry: python -m tools.decimal_ast_check`, `files: \.py$`, `exclude: ^tests/tools/decimal_ast/(positive|nested|syntax_error)/`. Top-of-file comment documents the exact CI command (`python -m tools.decimal_ast_check $(git ls-files '*.py' | grep -Ev '^tests/tools/decimal_ast/(positive|nested|syntax_error)/')`) so whichever CI provider the operator later chooses has a copy-paste invocation (D-73). The `import-linter` placeholder remains reserved for plan 01-03.
- **`BithumbSecrets(BaseSettings)`** — pydantic-settings v2 model with exactly four fields: `account_read_access_key`, `account_read_secret_key`, `trade_access_key`, `trade_secret_key`, all typed `SecretStr | None`, defaulting to `None`. `SettingsConfigDict(env_prefix="BITHUMB_", case_sensitive=False, secrets_dir=None, extra="forbid")` — unknown env vars raise `ValidationError` (defense against typos silently dropping credentials). `SecretStr` masking verified positively (`repr` contains `***`) and negatively (a sentinel value like `hunter2` is absent from `repr(model)`, `str(model)`, `model.model_dump()`, and `model.model_dump_json()`). Module docstring documents the D-89 ephemeral-construction rule.
- **`bithumb_bot.secrets.loader`** — `load_secrets(repo_root)` returns a freshly-constructed `BithumbSecrets` from env only, or env + external file:
  1. If `BITHUMB_BOT_SECRETS_FILE` is set: `Path.resolve(strict=True)` on both the file path and the repo root, then `is_relative_to` — symlinks are followed on both sides before the containment check. In-repo (direct or via symlink) → `SecretsFileInsideRepoError` (D-65).
  2. Parse the file as narrow `KEY=value` (no shell semantics, no `export`, no quoting nuance; `#`-comments and blank lines skipped; malformed lines silently dropped so no content leaks into an error).
  3. For every tracked D-67 credential key present in BOTH `os.environ` and the parsed file with DIFFERENT values → `AmbiguousSecretsConfigurationError(key)` (D-66 rule 3).
  4. Construct `BithumbSecrets()` from env, then `model_copy(update=...)` overlay for file-only credential keys — env-wins precedence enforced by construction.
- **`reject_trade_credentials(settings)`** — raises `ProhibitedCredentialDetectedError(credential_class="trade")` if either `trade_access_key` or `trade_secret_key` is not `None`. Reads only the fact of presence via `is not None` — never unwraps the `SecretStr`, never touches the value. Exception message contains only the literal `'trade'`, verified with a distinctive-hex sentinel absence check.
- **`validate()` hook.** `src/bithumb_bot/config/validator.py` gained `_check_trade_credential_prohibition(repo_root)`, invoked before every other check for every capability with `row.trade_cred_prohibited` — which is every registered capability per D-97. On rejection the raised `ProhibitedCredentialDetectedError` is translated into a clean `ValidationResult(ok=False, missing=("trade_credential_prohibited",), reason=...)` so the CLI dispatcher (plan 01-03) prints a uniform refusal instead of a Python traceback.
- **`redact_secrets` structlog processor** — 10-line pure function walking the top-level `event_dict` and replacing every `SecretStr` value with the literal `"***"`. Importable independently of the full structlog config (no side effects on import) so plan 01-04 can wire it in without forcing every consumer of `bithumb_bot.secrets` to pay the structlog import cost. Nested `SecretStr` intentionally left visible — nested containers with secrets are a design smell the observability layer should surface, not silently mask.
- **`bithumb_bot.core.money`** — `Money` and `Qty` frozen dataclass wrappers around `value: Decimal`. `__post_init__` uses `type(value) is Decimal` identity (not `isinstance`) to reject `Decimal` subclasses. Only `.from_str(<str>)` accepts string input. `__add__` / `__sub__` require same-type operands; `__mul__` accepts `Decimal | int` on the RHS and explicitly rejects `bool` (which passes `isinstance(x, int)` in Python) and `float`. Comparisons defined between same-type operands. Five risk-denominator `NewType(Decimal)`s (`PlannedStopLoss`, `MaxMarketLoss`, `MaxOperationalLoss`, `PositionFraction`, `RiskPerTrade`) each carry a module-level docstring section AND a per-binding docstring, giving downstream sizing call sites `mypy --strict`-enforced type discipline at zero runtime cost.
- **`bithumb_bot.core.rounding`** — four helpers: `quantize_krw_amount_down`, `quantize_volume_down`, `quantize_price_tick_up`, `quantize_price_tick_down`. Guard against non-positive `unit` / `step` / `tick` with `ValueError`. Internally use `(x / unit).to_integral_value(rounding=ROUND_DOWN|ROUND_UP)` then multiply back onto the grid — this handles sub-unit ticks like `Decimal("0.00000001")` exactly, satisfies the idempotence property (`q(q(x, u), u) == q(x, u)`), and never allocates a float. Hypothesis property tests use `hypothesis.strategies.decimals(...)` exclusively — never `strategies.floats`. Docstrings cite D-49 (base rule), D-50 (round UP for BUY tick), D-51 (round DOWN for SELL tick) verbatim.
- **`errors.py`** — grew `ProhibitedCredentialDetectedError(credential_class=...)`, `SecretsFileInsideRepoError(resolved_path)`, `AmbiguousSecretsConfigurationError(credential_key)`. Every one accepts ONLY the class/key/path name via keyword arg — the constructor signature makes it impossible for a future refactor to accidentally start passing values through. Message templates never interpolate values.
- **No-withdrawal static sweep + integration smoke.** `tests/secrets/test_no_withdrawal_credential.py` shells out to `git ls-files *.py *.toml`, greps each tracked file for the token `withdraw` (case-insensitive), and asserts every match sits within 3 lines of a `D-69` or `no withdrawal` marker. Also asserts `BithumbSecrets.model_fields` contains no withdrawal-adjacent field name. `tests/secrets/test_integration.py` composes plans 01-01 + 01-02: `load_secrets(repo_root)` succeeds when no trade env is set; `validate(("m1","fetch-spec"))` returns `ok=True` when both account/read env vars are set to sentinels; adding `BITHUMB_TRADE_ACCESS_KEY` returns `ok=False` with `missing=("trade_credential_prohibited",)` and a sentinel-free reason string.

## Task-by-Task Completion Status

Cross-reference of each plan task against landed commits and disk state. Every task was completed by the prior executor before interruption — this close-out session executed zero task-level RED/GREEN cycles and only wrote SUMMARY.md + advanced state.

| Task | Type | tdd | Landed | Commit(s) | Files verified | Status |
|------|------|-----|--------|-----------|----------------|--------|
| 01-02-01 — Repo-owned AST checker | code | true | Wave 1 | `fa1ae72` | `tools/{__init__,decimal_ast_check}.py` | ✅ complete (tests provided by 01-02-02 fixture suite) |
| 01-02-02 — Positive + negative + syntax-error fixture suite | test | true | Wave 1 | `83fc32c` | 12 fixture files + `tests/tools/test_decimal_ast_check.py` | ✅ complete (19/19 pass) |
| 01-02-03 — Wire hook into pre-commit + document CI wiring | config | — | Wave 1 | `05b9ead` | `.pre-commit-config.yaml` (placeholder swapped for functional entry) | ✅ complete |
| 01-02-04 — `BithumbSecrets` model | code | true | Wave 2 | `0559930` (RED) → `8432ab4` (GREEN) | `secrets/{__init__,settings}.py` + `tests/secrets/test_settings.py` | ✅ complete |
| 01-02-05 — Loader (symlink-safe, ambiguous, trade-refusal) + `validate()` hook | code | true | Wave 2 | `ce1d2b5` (RED) → `6a09fd5` (GREEN) | `secrets/loader.py`, `errors.py` (3 new exceptions), `config/validator.py` (hook), `tests/secrets/test_loader.py` | ✅ complete (Windows symlink test skips on non-admin, documented in plan Oracle) |
| 01-02-06 — `redact_secrets` structlog processor | code | true | Wave 2 | `b1e8354` (RED) → `2316319` (GREEN) | `secrets/redaction.py` + `tests/secrets/test_redaction.py` | ✅ complete |
| 01-02-07 — `bithumb_bot.core.money` | code | true | Wave 2 | `043d1fb` (RED) → `e61327c` (GREEN) | `core/money.py` + `tests/core/{__init__,test_money}.py` | ✅ complete (AST checker over `money.py` exits 0) |
| 01-02-08 — `bithumb_bot.core.rounding` | code | true | Wave 2 | `be52ce5` (RED) → `a701518` (GREEN) | `core/rounding.py` + `tests/core/test_rounding.py` | ✅ complete |
| 01-02-09 — No-withdrawal sweep + integration smoke | test | — | Wave 2 | `6603974` | `tests/secrets/{test_no_withdrawal_credential,test_integration}.py` | ✅ complete |

## Task Commits

| Task | Description | Commit | Type |
|------|-------------|--------|------|
| 01-02-01 | `tools.decimal_ast_check` with alias tracking (D-49, D-72) | `fa1ae72` | feat |
| 01-02-02 | Positive+negative+syntax_error fixtures for decimal AST checker | `83fc32c` | test |
| 01-02-03 | Wire `decimal-float-literal-check` pre-commit hook (D-49, D-73) | `05b9ead` | config |
| 01-02-04 | RED — failing tests for `BithumbSecrets` settings model | `0559930` | test (RED) |
| 01-02-04 | GREEN — `BithumbSecrets` pydantic-settings model (D-64, D-67, D-70, D-89) | `8432ab4` | feat |
| 01-02-05 | RED — failing tests for secrets bootstrap + errors surface | `ce1d2b5` | test (RED) |
| 01-02-05 | GREEN — secrets bootstrap loader + `validate()` hook (D-65, D-66, D-68, D-97) | `6a09fd5` | feat |
| 01-02-06 | RED — failing tests for `redact_secrets` structlog processor | `b1e8354` | test (RED) |
| 01-02-06 | GREEN — `redact_secrets` structlog processor (D-70 defense in depth) | `2316319` | feat |
| 01-02-07 | RED — failing tests for `Money`/`Qty` + risk-denominator vocabulary | `043d1fb` | test (RED) |
| 01-02-07 | GREEN — `core.money`: `Money`, `Qty`, `NewType(Decimal)` vocab (D-49, SAFE-07) | `e61327c` | feat |
| 01-02-08 | RED — failing tests for direction-aware rounding helpers | `be52ce5` | test (RED) |
| 01-02-08 | GREEN — `core.rounding` direction-aware quantize helpers (D-49/50/51) | `a701518` | feat |
| 01-02-09 | No-withdrawal static sweep + secrets integration smoke (D-69) | `6603974` | test |

**Plan metadata commit:** appended at close-out (this session).

## Decisions Made

1. **`Decimal.from_float(...)` is NOT flagged.** The AST checker deliberately allows the explicit `Decimal.from_float(x)` construction because it makes the float-to-decimal conversion visible at the call site. Silent construction via `Decimal(0.1)` is the pathology D-49 targets; a caller who reaches for `Decimal.from_float` has made an informed choice.
2. **Loader uses `model_copy(update=...)` instead of pydantic-settings' `_env_file=` support.** Passing `_env_file=` to `BithumbSecrets(...)` would let pydantic-settings' internal source-selector merge env and file — but the merge order and precedence at that layer are pydantic-internal implementation detail. Doing the env-loaded base + file-only overlay ourselves in `load_secrets` keeps "env wins" enforced by construction (env keys are populated first; file keys skip anything already set) and keeps the ambiguous-mixed check auditable in loader code, not pydantic-settings code.
3. **`SecretStr | None` field type, not `SecretStr`.** Allows the loader to distinguish `unset` (`None`) from `present-but-empty` (`SecretStr("")`) — both are refused by account-read-required capabilities, but they emit different failure reasons and the operator gets a more actionable diagnostic.
4. **`redact_secrets` walks only the top-level event dict.** Nested `SecretStr` in a log call remains visible on purpose — a well-designed log call never puts a secret in a nested container, so the pattern is a design smell we want surfaced rather than silently masked. Pydantic v2's own `SecretStr` masking (which fires on `str(secret_str)`) is the primary defense; the processor is belt-and-suspenders for accidental raw-SecretStr log calls.
5. **`Money` / `Qty` `__post_init__` uses `type(x) is Decimal` (identity, not `isinstance`).** Rejects `Decimal` subclasses that could alter rounding or comparison semantics. The sizing pipeline must know exactly what arithmetic it is running.
6. **`Money.__mul__` / `Qty.__mul__` explicitly reject `bool`.** In Python, `bool` is a subclass of `int` and would silently pass an `isinstance(x, int)` check. The explicit `isinstance(other, bool)` rejection prevents `Money(x) * True` from silently multiplying by 1.
7. **Rounding primitives return raw `Decimal`, not `Money` / `Qty`.** Callers wrap in the appropriate type at their site. This keeps the primitives reusable across sizing, accounting, and broker-adapter contexts — none of which shares the same wrapper type discipline.
8. **`_ceil_to_multiple` / `_floor_to_multiple` use `(x / unit).to_integral_value(rounding=...)` then multiply back onto the grid.** Chosen over `Decimal.quantize(exp, rounding=...)` because it supports sub-unit ticks (e.g. `Decimal("0.00000001")`) exactly, preserves idempotence for any positive `Decimal` unit, and works uniformly for both floor and ceil directions without exponent-alignment gymnastics.
9. **D-69 static sweep excludes its own test file from the scan.** The test defines constants like `_WITHDRAW_RE = re.compile(r"withdraw", ...)` that would trigger a false positive against itself. The sweep filters out the current file via `path.resolve() == Path(__file__).resolve()` before checking marker context.
10. **Trade-cred check runs BEFORE gate/cred checks in `validate()`.** Every registered capability has `trade_cred_prohibited=True` per D-97. Checking it first means a trade-cred env var short-circuits the validator with the specific `trade_credential_prohibited` refusal even for capabilities that would also fail on a missing gate file — the safety-critical failure is what the operator sees first.

## Deviations from Plan

**None** — every task's Behavior contract, Files list, and Oracle was implemented as written by the prior executor. This close-out session executed zero task-level deviations; the only work done in this session was:

1. Verifying every plan file exists on disk (33/33 found).
2. Running the full plan test suite (`uv run pytest -x -q --no-cov tests/tools tests/secrets tests/core`) — 121 passed, 1 skipped.
3. Running the full repo test suite (`uv run pytest -q --no-cov`) — 220 passed, 1 skipped.
4. Confirming `uv run python -m tools.decimal_ast_check tools src` exits `0` on the real committed tree.
5. Writing this SUMMARY, advancing STATE.md / ROADMAP.md / REQUIREMENTS.md, and creating the metadata commit.

**Auto-fixed Issues during original execution:** none recorded in the RED/GREEN commit history — every RED commit was followed by a clean GREEN commit with no intermediate fix-up work.

## Issues Encountered

None during this close-out session. The prior executor's 14 task commits landed cleanly; the working tree was clean apart from three out-of-scope `.planning/research/.cache/` files (untracked; expected artifact of the research subsystem — left untouched).

## Threat Register — Mitigation Verification

| Threat ID | Category | Component | Severity | Status | Test(s) |
|-----------|----------|-----------|----------|--------|---------|
| T-1-02-01 | Information Disclosure | `SecretStr` leaking via logs / exception text / `repr` / `model_dump` | critical | mitigated | `test_settings.py` (`SecretStr` masking positive+negative sentinel-absence); `test_redaction.py` (processor + `model_dump_json` sentinel absent); `test_loader.py::test_reject_trade_creds_raises_with_class_only` (`str(exc)` contains `trade` but not `deadbeef` or any hex substring); `test_integration.py` (sentinel absent from `ValidationResult.reason` / `missing` / `repr`) |
| T-1-02-02 | Tampering | In-repo `.env` / symlinked-into-repo secrets file silently loaded | high | mitigated | `test_loader.py::test_absolute_in_repo_path_rejected`; `test_symlink_into_repo_rejected` (skipped on Windows without admin/dev-mode per plan Oracle); `_resolve_secrets_file` uses `Path.resolve(strict=True)` + `is_relative_to` on fully-resolved repo root |
| T-1-02-03 | Elevation of Privilege | Trade credential env var silently accepted pre-M6B | critical | mitigated | `test_loader.py::test_reject_trade_creds_raises_*`; `test_integration.py::test_validate_refuses_when_trade_cred_present`; validator wires the check for every registered capability (`row.trade_cred_prohibited=True` is D-97 default) |
| T-1-02-04 | Tampering (data integrity) | `Decimal` silently constructed from float literal introduces base-2 rounding | critical | mitigated | `test_decimal_ast_check.py` (19 tests covering 6 positive + 5 negative + 1 syntax-error + 1 nested-directory scan fixture); `.pre-commit-config.yaml` `decimal-float-literal-check` hook; CI invocation documented in top-of-file comment (D-73); `test_money.py` (float construction rejected at every `Money` / `Qty` entry point); `test_rounding.py` (hypothesis strategies never generate floats) |
| T-1-02-05 | Repudiation | Ambiguous mixed env + file secrets config silently resolves to one side | medium | mitigated | `test_loader.py::test_ambiguous_mixed_rejected`; `_check_ambiguous_overlap` raises `AmbiguousSecretsConfigurationError(key)` for any tracked D-67 key present in both sources with differing values BEFORE any credential is loaded |
| T-1-02-06 | Elevation of Privilege | Withdrawal credential accidentally added to the codebase | high | mitigated | `test_no_withdrawal_credential.py::test_every_withdraw_match_cites_D69` (repo-wide `git ls-files *.py *.toml` sweep); `test_bithumb_secrets_has_no_withdrawal_field` (`BithumbSecrets.model_fields` audit) |
| T-1-02-SC | Tampering | npm/pip/cargo installs | low | accepted (per plan) | No new installs — `pydantic-settings` was already introduced by 01-01; every runtime dep is a re-use of the 01-01 lockfile pin |

## Threat Flags

None — no new security-relevant surface was introduced outside the plan's `<threat_model>` block. The trade-cred prohibition, secrets-file containment check, and D-69 no-withdrawal invariant are all pre-existing threats in the plan's own register.

## Known Stubs

None — every module ships with the full behavior its docstring promises. The two forward-references (structlog wiring for `redact_secrets` → plan 01-04, and spec-snapshot values for `core.rounding` → plan 01-04 + Phase 2) are documented in their module docstrings and are correct architectural boundaries, not stubs.

## Next Phase Readiness

Ready for plan **01-03** (`core/` ↔ `broker/` import-direction boundary + CI enforcement scaffolding). Prerequisites this plan provides:

- **Pre-commit is functional.** The `decimal-float-literal-check` hook is wired to the real AST checker, and the `import-linter` id remains reserved with `entry: "true"` — plan 01-03 owns the swap-in of `lint-imports`.
- **`errors.py` is stable.** Every fail-closed condition (`Gate1LoadError`, `UnknownCapabilityError`, `ProhibitedCredentialDetectedError`, `SecretsFileInsideRepoError`, `AmbiguousSecretsConfigurationError`) is declared. Plan 01-03's import-boundary work adds no new exception classes.
- **`bithumb_bot.core.*` has real content.** `core.money` and `core.rounding` are the first substantive core modules — plan 01-03's Import Linter contract can now assert `core.*` never imports from `broker.*` against real code rather than empty packages.
- **`bithumb_bot.secrets.*` is a live subsystem.** Plan 01-04 (M1 spec adapter) can consume `load_secrets(repo_root)` as-is; no additional loader work needed.
- **The `validate()` hook is wired for every capability.** Any future capability registered in the D-88 registry inherits trade-cred prohibition by default (D-97) — no per-capability integration work required.

No blockers or concerns for the next plan.

## Self-Check: PASSED

- Every declared `key-files.created` file exists on disk (33/33 verified).
- Every recorded task commit is present in `git log --oneline --all` (14/14 verified via `git log --grep="01-02"`).
- Every plan `<success_criteria>` bullet re-executed and passed at close-out:
  - AST checker exits `1` on any positive fixture, `0` on real tree, `2` on syntax error (verified via `test_decimal_ast_check.py` 19/19 pass + direct CLI invocation `exit=0`).
  - `BithumbSecrets` exposes exactly 4 credential fields, zero `withdrawal_*` (`test_settings.py` + `test_no_withdrawal_credential.py`).
  - `BITHUMB_BOT_SECRETS_FILE` in-repo → `SecretsFileInsideRepoError` (`test_loader.py::test_absolute_in_repo_path_rejected`).
  - Ambiguous mixed → `AmbiguousSecretsConfigurationError` (`test_loader.py::test_ambiguous_mixed_rejected`).
  - `SecretStr` renders as `***` under `repr`/`str`/`model_dump()` (`test_settings.py` + `test_redaction.py`).
  - `bithumb_bot.core.money` exports `Money`, `Qty`, and all five `NewType`s with docstrings (`test_money.py` + module inspection).
  - Rounding helpers pass exact/below/above/zero/idempotence property tests (`test_rounding.py`).
- Every plan `<acceptance_criteria>` (per-task Oracle) recorded green in the RED→GREEN commit pairs; no criterion was silently skipped.
- Every `<threat_model>` entry has an explicit corresponding test (see Threat Register table above).
- Full repo test suite: **220 passed, 1 skipped** in 4.76s.

---
*Phase: 01-safety-foundation-bithumb-spec-adapter*
*Completed: 2026-09-08 (executed by prior agent 14 commits; SUMMARY + state advancement by close-out agent 2026-09-08)*
