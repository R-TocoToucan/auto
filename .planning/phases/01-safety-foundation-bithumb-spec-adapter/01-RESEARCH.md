# Phase 1 Research: Safety Foundation + Bithumb Spec Adapter

**Researcher:** gsd-phase-researcher
**Written:** 2026-09-08
**Scope:** Implementation-shaped gaps only (frozen Gate-1 decisions in `01-CONTEXT.md` are not re-audited). This document does not re-derive stack choices already fixed in `.planning/research/STACK.md`, and it does not re-open any Gate-1 decision (D-01…D-100).

## User Constraints (from CONTEXT.md)

The full Decision Register (G1.1–G1.16, D-01…D-100) in `01-CONTEXT.md` is frozen and binding; it is not reproduced verbatim here (466 lines — read it directly). The findings below assume every D-number cited is already law. The planner discretion areas most relevant to *this* research pass, copied verbatim from `01-CONTEXT.md`'s `<decisions>` § Claude's Discretion:

- **Repo directory layout** — final package name and module layout under `src/`. Must respect: `core/` never imports from `broker/`; the actual package name must be usable by the Import Linter contract in `pyproject.toml` (D-71); `.planning/research/ARCHITECTURE.md`'s suggested layout is a reasonable starting point but nothing binds Phase 1 to it verbatim.
- **Risk-denominator vocabulary implementation form** — SAFE-07 requires "documented." Whether the vocabulary also lives as pydantic value objects / `NewType` / `Annotated[Decimal, ...]` consumed by the sizing pipeline, or stays as docstrings + naming convention only, is planner discretion.
- **Per-channel token-bucket implementation** — asyncio-native vs threading vs both; concrete internal API. Numeric per-channel rate values are M1 build-time verification items.
- **Test framework layout** — pytest + hypothesis are fixed by `.planning/research/STACK.md`; the layout of `tests/` and hypothesis strategies is planner discretion.

Deferred (out of scope for Phase 1, not touched by this research): full L2-aware simulator, persistent sequenced L2 pipeline, live-phase runtime liquidity guard, legacy `/trade/stop_limit` stack, Gate-2/Gate-3 numeric operational parameters, per-channel rate-limit numeric values (M1 build-time item).

## Executive Summary

- **Import Linter and the Decimal-from-float AST checker are both pure-Python, dependency-light, and mechanically testable with negative fixtures today** — no external verification needed beyond confirming current Import Linter TOML syntax (verified below). The Decimal checker is ~50–80 lines of stdlib `ast` code; do not reach for a Ruff plugin (Rust toolchain, forbidden by D-73).
- **`tomllib` (stdlib, Python ≥3.11) is sufficient — no `tomli` dependency needed** given the STACK.md-recommended Python 3.12/3.13 floor; pin `requires-python = ">=3.11"` explicitly so this is enforced, not assumed.
- **pydantic-settings' default source-priority (init → env vars → dotenv → secrets dir) already gives env-vars-beat-file precedence "for free" (D-66's #1 rule) — but it does NOT detect *ambiguous* mismatched values across sources**, which D-66's third rule requires. This is a genuine implementation gap: the planner must add an explicit pre-validation diff step, not rely on pydantic-settings' layering alone.
- **`Path.resolve(strict=True)` + `Path.is_relative_to(repo_root.resolve())` is the correct, symlink-safe pattern** for rejecting an in-repo secrets-file path (D-65) — `resolve()` follows symlinks to their real target before the containment check runs.
- **A build-time verification discrepancy was found in the JWT claim shape**: the Bithumb API docs (cited below) describe the private-endpoint JWT payload as `access_key`, `nonce`, and (conditionally) `query_hash`/`query_hash_alg` — **no explicit `timestamp` claim was found in this session's citation**, contradicting an assumption in the phase's own task framing. Flagged as an open M1 verification item, not resolved here.
- **Recommend stdlib `argparse` over `click`/`Typer` for the `bt` CLI** — zero new dependency, consistent with the ponytail/no-new-deps posture and with `.planning/research/STACK.md` (which names no CLI library at all). `click` remains a reasonable discretionary upgrade later if subcommand ergonomics become painful, but is a new dependency decision the operator should explicitly approve, not one this research pre-authorizes.
- **`os.replace()` atomicity requires same-filesystem source/destination** — on Windows this means the `.tmp-<pid>` file must be created in the *same directory* as the target, not a global temp dir; flagged explicitly because the project runs on Windows.

## Findings

### 1. Import Linter contract wiring (D-71, D-73)

**Exact block form.** D-71 already specifies the working shape; current Import Linter documentation confirms `pyproject.toml` embedding uses a `[tool.importlinter]` root section plus one or more `[[tool.importlinter.contracts]]` array-of-tables entries (this is the TOML-native equivalent of the older `.importlinter` INI format's `[importlinter:contract:name]` sections — **the key gotcha for the planner: in the TOML form, `source_modules`/`forbidden_modules` are TOML arrays (`["a.b", "a.c"]`), not the INI form's newline/space-separated strings** — do not copy an INI-style example verbatim into `pyproject.toml`).

```toml
[tool.importlinter]
root_package = "<actual_package_name>"
exclude_type_checking_imports = false

[[tool.importlinter.contracts]]
name = "Core must not import broker"
type = "forbidden"
source_modules = ["<actual_package_name>.core"]
forbidden_modules = ["<actual_package_name>.broker"]
```

`exclude_type_checking_imports = false` (per D-71) means imports under `if TYPE_CHECKING:` guards ARE counted as real imports for contract purposes — the stricter, safer setting for a boundary this consequential. `root_package` must be importable (the package must already be installed/importable in whatever environment runs `lint-imports`, e.g. via `uv pip install -e .` or `uv sync`) — Import Linter builds its dependency graph by importing the package, not by parsing files in isolation. **Confidence: MEDIUM** (contract syntax confirmed via official docs this session; exact TOML-vs-INI list-syntax distinction is the one non-obvious gotcha — [CITED: import-linter.readthedocs.io]).

**Running from pre-commit.** Because the contract check needs the *whole project* importable, not a single changed file, this hook is project-wide, not per-file:

```yaml
- repo: local
  hooks:
    - id: import-linter
      name: import-linter (core must not import broker)
      entry: lint-imports
      language: system
      pass_filenames: false
      always_run: true
```

`language: system` (not `language: python`) is the right choice here — pre-commit's isolated-venv `language: python` hooks don't have the project's own package installed by default, and `lint-imports` needs to import `<actual_package_name>` to build its graph. Rely on the operator's active `uv`-managed environment already having both `import-linter` and the project installed. **Confidence: MEDIUM** — [CITED: import-linter.readthedocs.io usage page confirms the import-graph-via-actual-import requirement]; `language: system` choice is a pre-commit-specific inference, not independently doc-confirmed this session — [ASSUMED].

**CI invocation** (no provider chosen yet — either works): `uv run lint-imports` as a step after `uv sync`, in whatever CI file the project eventually adopts (GitHub Actions step, or a plain `scripts/quality.sh` invoked by any CI). This should be the same single command pre-commit runs, so "the quality command" (D-73's phrase) is one script/command, not two divergent invocations.

**Negative fixture test proving the contract fires.** Since `lint-imports` needs a real importable package to run against, don't try to mutate the production tree in a test (fragile, order-dependent). Instead: commit a small, permanently-broken scratch package under `tests/fixtures/import_linter_violation/` (e.g. `badpkg/core/leak.py` containing `from badpkg.broker import x`) with its own tiny `pyproject.toml`-equivalent Import Linter config pointing `root_package` at `badpkg` and a `forbidden` contract identical in shape to the real one. The test invokes `lint-imports` via `subprocess.run(["lint-imports", "--config", <fixture-config-path>], cwd=<fixture-dir>)` and asserts a **nonzero** exit code. This is a static, always-broken fixture — nothing to "restore" — which is simpler and more robust than trying to inject-then-revert a violation into the real tree. **Confidence: MEDIUM** — pattern inferred from Import Linter's own documented `forbidden` contract semantics [CITED: import-linter.readthedocs.io/en/stable/contract_types/forbidden/]; the specific "separate scratch package" test-harness shape is [ASSUMED] (a reasonable test-engineering choice, not itself documented by the tool).

Pin the exact `import-linter` version in the `uv` lockfile per D-73; do not float it.

---

### 2. Repo-owned Decimal-from-float AST checker (D-72, D-73)

**Do not use a Ruff plugin** (forbidden by D-73 — Ruff plugins require the Rust toolchain and Ruff's plugin API; this is bespoke, safety-critical logic that must live in auditable, unit-testable pure Python). Ruff keeps running for everything it already covers natively; this checker is a separate, additional tool.

**AST shapes to detect** (single-file walk, `ast.parse` + `ast.NodeVisitor` or a flat `ast.walk`):

1. **Direct name call:** `Call(func=Name(id="Decimal"), args=[Constant(value=<float|complex>)])` — matches `Decimal(0.1)` when `Decimal` was imported via `from decimal import Decimal`.
2. **Aliased direct-name call:** `Call(func=Name(id="D"), args=[...])` — matches `D(0.1)` when `D` was imported via `from decimal import Decimal as D`.
3. **Attribute call:** `Call(func=Attribute(value=Name(id="decimal"), attr="Decimal"), args=[...])` — matches `decimal.Decimal(0.1)` when the module was imported via `import decimal`.
4. **Aliased-module attribute call:** `Call(func=Attribute(value=Name(id="dec"), attr="Decimal"), args=[...])` — matches `dec.Decimal(0.1)` when imported via `import decimal as dec`.
5. **Signed literal wrapper:** the flagged argument may be wrapped in `UnaryOp(op=UAdd() | USub(), operand=Constant(value=<float>))` — this is how `+0.1`/`-0.1` parse; unwrap one level of `UnaryOp` before checking `Constant`.
6. **Complex literal:** `Constant(value=<complex>)` (e.g. `Decimal(1j)`) — same treatment as float; `Decimal` never legitimately accepts a complex argument, so this is an unambiguous flag.

**Resolving aliases within a single file (no cross-file inference — explicit non-goal).** Do one pass over the module's top-level `ast.Import` / `ast.ImportFrom` nodes first, building a small dict: module aliases pointing at `decimal` (`import decimal`, `import decimal as dec` → `{"decimal": "decimal", "dec": "decimal"}`) and name aliases pointing at `decimal.Decimal` (`from decimal import Decimal`, `from decimal import Decimal as D` → `{"Decimal": "decimal.Decimal", "D": "decimal.Decimal"}`). Then walk `Call` nodes and only flag a call whose callable resolves through this table — this is what makes the checker correctly ignore an unrelated project-local class that happens to also be named `Decimal` in a file that never imported the stdlib module. **Explicitly out of scope:** re-binding an alias mid-file (`Decimal = SomethingElse`) or any cross-module/type-inference tracking — document this as a known, accepted false-negative surface, not a bug to fix.

**Positive fixtures that MUST flag** (per the task's own list, confirmed against the AST shapes above):
`Decimal(0.1)`, `Decimal(-0.1)`, `dec.Decimal(+0.1)`, `D(0.1)`, `decimal.Decimal(0.1)`.

**Negative fixtures that MUST NOT flag:**
`Decimal("0.1")` (str constant — never flagged), `Decimal(1)` (int constant — exact, allowed), `Decimal(-1)` (`UnaryOp(USub, Constant(int))` — allowed), `Decimal("-0.1")` (str), `Decimal(some_string_var)` (arg is a `Name`, not a `Constant` — statically unknown type, so **must not** be flagged; this is a deliberate false-negative to avoid false positives on legitimate `Decimal(str_var)` calls, matching D-72's "rejection of statically identifiable float literals" scope).

**Pre-commit hook stanza** (per-file, unlike Import Linter above):

```yaml
- repo: local
  hooks:
    - id: decimal-float-literal-check
      name: reject Decimal() constructed from a float/complex literal
      entry: python -m tools.decimal_float_lint
      language: system
      files: \.py$
      pass_filenames: true
```

Mirrored CI invocation: the same `python -m tools.decimal_float_lint <files>` command, run over the full tracked `*.py` set (e.g. `git ls-files '*.py'`) rather than only changed files, so CI catches anything pre-commit was bypassed for.

**Confidence: HIGH on the AST shapes and alias-tracking design** (this is a direct, mechanical reading of Python's `ast` grammar for `Call`/`Attribute`/`Name`/`UnaryOp`/`Constant` nodes — stable stdlib behavior, not fetched via a live source this session, so tagged **[ASSUMED]** per this session's provenance rule, but it is standard, well-documented `ast` module structure with very low risk of being wrong). Include syntax-error and nested-directory fixtures per D-73 (a file that fails to parse should produce a clear tool error, not a silent pass or crash; the checker should walk `src/`/`tests/` recursively when invoked without explicit file args from CI).

---

### 3. `config/decisions/gate1.toml` loader (D-55…D-63)

**`tomllib` vs `tomli`.** Pin `requires-python = ">=3.11"` in `pyproject.toml` (consistent with STACK.md's Python 3.12/3.13 recommendation) and use only stdlib `tomllib` — no `tomli` dependency. **Gotcha:** `tomllib.load()` requires a **binary-mode** file object (`open(path, "rb")`); it raises if given a text-mode handle. **Confidence: MEDIUM** — stable, well-known stdlib behavior; [ASSUMED] (not independently re-fetched this session, but this is documented, unambiguous stdlib API shape unlikely to have changed).

**Frozen pydantic v2 model shape:**

```python
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, BeforeValidator

def _decimal_from_str(v: object) -> object:
    if isinstance(v, str):
        return Decimal(v)
    if isinstance(v, Decimal):
        return v
    raise TypeError("decimal fields must be TOML strings, never TOML floats/ints")

StrictDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_str)]

class Gate1Decisions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    schema_version: Literal[1]
    status: Literal["frozen"]
    approved_at_utc: str
    source_commit: str
    research_spec_sha256: str
    execution_spec_sha256: str
    max_validated_notional_krw: StrictDecimal | None
    provisional_engineering_notional_krw: StrictDecimal
    # ...remaining Gate-1 fields per the Decision Register
```

`frozen=True` makes instances immutable (attribute assignment raises) — this is what makes `ResolvedConfig`-style objects safe to pass around and hash. `extra="forbid"` rejects any TOML key not declared on the model (fail-closed on typos/unexpected fields, per D-60's "malformed... required gate files → fail closed"). `strict=True` at the model level prevents pydantic's normal lax-mode type coercion (e.g. an int silently becoming a str) for every field, not just the decimal ones — combined with the custom `BeforeValidator`, this forces every Gate-1 TOML author to write decimal-bearing fields as **quoted TOML strings** (`max_validated_notional_krw = "100000"`, never `100000` or `100000.0`), which is the same string-not-float discipline D-75 already requires for the spec-snapshot JSON. **Confidence: MEDIUM** — this is standard, documented pydantic v2 `Annotated` + `BeforeValidator` + `ConfigDict` usage; [ASSUMED] (context7 was unavailable this session to independently re-verify against the currently-pinned pydantic version — flag for a quick smoke-test against the pinned `pydantic` version at build time).

**Canonical hashing recipe for per-gate provenance (D-61/D-62).** The simplest, most auditable approach: hash the **exact committed bytes** of `config/decisions/gate1.toml` itself (`hashlib.sha256(path.read_bytes()).hexdigest()`) rather than re-serializing the parsed model — TOML has no single canonical byte form the way this project's JSON convention does (D-76), so hashing the file git already tracks avoids inventing a second, parallel canonicalization rule just for TOML. This SHA-256 becomes `gate1_sha256` (and, in later phases, `gate2_sha256`/`gate3_sha256`).

**`config_hash` manifest recipe (D-62).** Reuse D-76's JSON canonicalization convention (see Finding 9) for the manifest that aggregates gate hashes:

```python
manifest = {"schema_version": 1, "gate1_sha256": gate1_hash, "gate2_sha256": None, "gate3_sha256": None}
canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
config_hash = hashlib.sha256(canonical).hexdigest()
```

Phase 1 implements this function generically and exercises it against a Gate-1-only manifest (`gate2_sha256`/`gate3_sha256` = `null`) — the *final*, all-three-gates `config_hash` is only meaningful at Phase 5's final freeze (FRZ-02), but the hashing mechanism itself is frozen now (D-62) and should not be re-invented in Phase 3/5.

**Capability-scoped validation contract shape** (function signature only — CLI/dispatcher design is out of scope for this research, per the phase's non-goals):

```python
def validate(capability: CapabilityName) -> ValidationResult: ...
```

Where `CapabilityName` is a closed set (e.g. `Literal["config_validate_gate1", "m0_selfcheck", "m1_fetch_spec", "m1_verify_facts", "m1_verify_snapshot"]` — an enum/`StrEnum` also works) matching the Phase-1 command registry (D-86), and `ValidationResult` is a small frozen value object (`ok: bool`, `missing: tuple[str, ...]`, `reason: str | None`). Internally the validator consults the D-88 guard matrix to determine which gate files, snapshot state, cap value, and credential class a given capability needs, loads *only* those (D-59: "capability-scoped validation loads only the gates required"), and fails closed on anything missing/malformed/hash-mismatched (D-60). **Confidence: MEDIUM** — this is a design synthesis of already-frozen D-55…D-63 rules, not itself requiring external verification.

---

### 4. Secrets via `pydantic-settings` + `SecretStr` (D-64…D-70)

**`BaseSettings` subclass pattern:**

```python
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class BithumbSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BITHUMB_", case_sensitive=False, secrets_dir=None, extra="forbid")
    account_read_access_key: SecretStr | None = None
    account_read_secret_key: SecretStr | None = None
    trade_access_key: SecretStr | None = None       # must be rejected pre-M6B (D-68)
    trade_secret_key: SecretStr | None = None        # must be rejected pre-M6B (D-68)
```

`env_prefix="BITHUMB_"` plus `case_sensitive=False` (the pydantic-settings default) maps `account_read_access_key` → the env var `BITHUMB_ACCOUNT_READ_ACCESS_KEY` exactly per D-67's naming. **Confidence: MEDIUM** — [CITED: pydantic.dev pydantic-settings docs, this session: "env_prefix... not only for env settings but also for dotenv files, secrets, and other sources"; "environment variable names are case-insensitive by default... unless case_sensitive=True"].

**Outside-repository secrets file (D-65) + symlink-safe rejection.** The `BITHUMB_BOT_SECRETS_FILE` path itself must come from the raw process environment *before* constructing the settings object (a bootstrap step, not a `BaseSettings` field — otherwise you have a chicken-and-egg problem: you need the path to know which dotenv file to layer in). Pattern:

```python
import os
from pathlib import Path

raw_path = os.environ.get("BITHUMB_BOT_SECRETS_FILE")
if raw_path is not None:
    resolved = Path(raw_path).resolve(strict=True)          # follows symlinks; raises if missing
    repo_root_resolved = repo_root.resolve(strict=True)
    if resolved.is_relative_to(repo_root_resolved):
        raise SecretsFileInsideRepoError(resolved)
    settings = BithumbSecrets(_env_file=resolved, _env_file_encoding="utf-8")
else:
    settings = BithumbSecrets()  # env vars only
```

`Path.resolve(strict=True)` is the load-bearing call: it resolves symlinks to their real target *and* requires the path to exist, so a symlink placed in an allowed external directory but pointing back into the repo resolves to the real in-repo path before the `is_relative_to()` containment check runs — this is exactly what defeats a symlink-based bypass of D-65's "reject a secrets-file path that resolves inside the git repository, including through a symlink." **Confidence: MEDIUM** — standard, stable `pathlib` behavior (`Path.is_relative_to` is Python ≥3.9, well within the 3.12/3.13 floor); [ASSUMED] (not independently re-fetched this session, but low-risk, well-known stdlib API).

**Precedence (D-66) — mostly free, one gap.** pydantic-settings' documented default source order is: init-time kwargs → environment variables → dotenv file → secrets directory — so **environment variables already win over the dotenv-style secrets file automatically**, satisfying D-66 rule #1/#2 without extra code. **However, this layering silently lets the env var value simply override the file value — it does NOT detect or reject the "ambiguous mixed configuration" case D-66 rule #3 requires** (same credential defined differently in both sources). This is a genuine gap the planner must build: before constructing `BithumbSecrets`, read the raw env var (if set) and separately parse the secrets file's raw key/value pairs (pydantic-settings' own dotenv parser can be reused, or a two-line manual parse since it's `KEY=value` lines), and if the same key is present in both with **different** values, raise an explicit `AmbiguousSecretsConfigurationError` — do not let the silent override happen. **Confidence: MEDIUM** — [CITED: pydantic.dev pydantic-settings docs, this session, re: `secrets_dir` and general settings-source precedence]; the "no built-in mismatch detection" gap is a reasoned inference from that documented layering behavior, not itself independently confirmed — flag as a design note for the planner, not a certified library limitation.

**`SecretStr` repr protection.** `SecretStr.__repr__()` renders as a fixed masked string (never the underlying value); accessing the real value requires the explicit `.get_secret_value()` call. A pydantic v2 model's default `repr()`/`str()`/`model_dump()` (JSON mode or not) will show the masked form for any `SecretStr` field, not the raw secret — this is documented pydantic v2 core behavior. For defense-in-depth beyond pydantic's own masking (per D-70's "never... expose secrets through model `repr`" and the general instruction to never let secrets reach a log line), add a small structlog processor:

```python
def redact_secrets(logger, method_name, event_dict):
    for key, value in event_dict.items():
        if isinstance(value, SecretStr):
            event_dict[key] = "***"
    return event_dict
```

Place it in the processor chain after context-merging but before the final renderer (see Finding 11).

**Trade-credential rejection pre-M6B (D-68).** The validator reports class, never value:

```python
def reject_trade_credentials(settings: BithumbSecrets) -> None:
    if settings.trade_access_key is not None or settings.trade_secret_key is not None:
        raise ProhibitedCredentialDetectedError(credential_class="trade")
```

Per D-97, this check must run for **every** pre-M6B command, not only trade-adjacent ones — fold it into the shared `validate()` entrypoint (Finding 3/5), not into a single capability's handler.

**Ephemeral loading pattern.** Do not instantiate `BithumbSecrets()` once at process/CLI startup and hold it globally. Construct it (or a narrower `AccountReadCredentials` object with just the two account-read fields) **inside** the exact function scope that performs the authenticated network call — e.g. inside `bithumb_spec.client.fetch_spec()`, not in `bt`'s top-level `main()`. This directly satisfies D-89's "Account/read credentials are loaded only by the exact M1 network operation that requires them. Offline verification must not load them" — e.g. `bt m1 verify-facts`/`verify-snapshot` (offline, per D-88's guard matrix) must never trigger a `BithumbSecrets()` construction at all.

---

### 5. `bt` CLI capability registry + dispatcher (D-85…D-90)

**CLI library verdict: stdlib `argparse`, not `click`.** `.planning/research/STACK.md` names no CLI library at all — adopting `click` (or `Typer`, which itself depends on `click`) would be a *new* dependency this research pass is instructed not to introduce. `argparse`'s two-level subparser pattern (`bt <verb> <subverb> [options]`) maps cleanly onto the required `bt config validate --through gate1`, `bt m0 selfcheck`, `bt m1 fetch-spec --market KRW-BTC`, `bt m1 verify-facts --bundle <path>`, `bt m1 verify-snapshot --snapshot <path>` shapes via nested `add_subparsers()` calls, at zero dependency cost — consistent with both D-73's dependency-conservative posture and the project's general "don't add a dependency until the manual approach gets unwieldy" ethos (`.planning/research/STACK.md`'s own `pandera` treatment makes the same call). **`click` remains a legitimate discretionary upgrade** later if `argparse`'s subcommand/help ergonomics become genuinely painful (its two-level-subparser boilerplate is real), but that is a new-dependency decision for the operator to explicitly approve, not something this research pre-authorizes. `Typer` is mentioned only because it wraps `click` — it is not a lighter-weight option than `click` itself. **Confidence: MEDIUM** — `argparse` subparser mechanics are stable, well-known stdlib behavior [ASSUMED]; the "no CLI lib in STACK.md" fact is [VERIFIED: this session's re-read of `.planning/research/STACK.md`, Development Tools table].

**Registry data shape** — one row per `(verb, subverb)`, encoding the entire D-88 guard-matrix row as data (not scattered `if` statements):

```python
@dataclass(frozen=True)
class CapabilityRequirements:
    gate1: bool
    gate2: bool
    gate3: bool
    snapshot: SnapshotRequirement       # NONE | EVIDENCE_INPUT | CANDIDATE | VERIFIED_REQUIRED
    cap: CapRequirement                  # NOT_REQUIRED | MAY_BE_NULL | REQUIRED_NON_NULL
    cred: CredRequirement                # NONE | ACCOUNT_READ_REQUIRED
    trade_cred_prohibited: bool          # always True for every Phase-1 command
    human_auth: HumanAuthRequirement     # NONE | INVOCATION_ONLY | EXPLICIT_APPROVAL | ONE_TIME_APPROVAL

REGISTRY: dict[tuple[str, str], CapabilityRequirements] = {
    ("config", "validate"): CapabilityRequirements(gate1=True, gate2=False, gate3=False, snapshot=SnapshotRequirement.NONE, cap=CapRequirement.NOT_REQUIRED, cred=CredRequirement.NONE, trade_cred_prohibited=True, human_auth=HumanAuthRequirement.NONE),
    ("m0", "selfcheck"): CapabilityRequirements(gate1=True, gate2=False, gate3=False, snapshot=SnapshotRequirement.NONE, cap=CapRequirement.NOT_REQUIRED, cred=CredRequirement.NONE, trade_cred_prohibited=True, human_auth=HumanAuthRequirement.NONE),
    ("m1", "fetch-spec"): CapabilityRequirements(gate1=True, gate2=False, gate3=False, snapshot=SnapshotRequirement.NONE, cap=CapRequirement.NOT_REQUIRED, cred=CredRequirement.ACCOUNT_READ_REQUIRED, trade_cred_prohibited=True, human_auth=HumanAuthRequirement.INVOCATION_ONLY),
    ("m1", "verify-facts"): CapabilityRequirements(gate1=True, gate2=False, gate3=False, snapshot=SnapshotRequirement.EVIDENCE_INPUT, cap=CapRequirement.NOT_REQUIRED, cred=CredRequirement.NONE, trade_cred_prohibited=True, human_auth=HumanAuthRequirement.NONE),
    ("m1", "verify-snapshot"): CapabilityRequirements(gate1=True, gate2=False, gate3=False, snapshot=SnapshotRequirement.CANDIDATE, cap=CapRequirement.NOT_REQUIRED, cred=CredRequirement.NONE, trade_cred_prohibited=True, human_auth=HumanAuthRequirement.NONE),
    # Future verbs (D-87) also get entries here — registered + schema/registry-tested, handlers NOT implemented (D-90)
}
```

This table is directly transcribable from D-88's guard-matrix rows and is itself the single artifact a "registry/schema test" (D-90) asserts against — one test per row, plus one test asserting every future verb from D-87 is present as a registry key even though its handler raises `NotImplementedError`.

**Dispatcher algorithm** (no side effects before validation passes):

1. Parse `argv` → `(verb, subverb, remaining_args)`.
2. `help`/`version` are special-cased *before* step 3 — no capability, no credential load (D-85).
3. Look up `REGISTRY[(verb, subverb)]`; **absent → `UnknownCommandError`** (D-89: "a future command that is not in the registry fails as unknown; capability requirements may not default to an empty set" — this must be a hard failure, not an empty/permissive `CapabilityRequirements()`).
4. Call `validate(capability=(verb, subverb))` (Finding 3's shared validator) — this is the **same function** internal service code calls (see next paragraph).
5. If `not result.ok`: print the refusal + `result.reason`; exit nonzero. **No handler code has run, no network/file/credential access has occurred.**
6. Else: dispatch to the registered handler.

**Defence in depth (D-85's "sensitive application-service functions repeat the capability check internally").** Every internal function reachable via a direct Python import — e.g. `bithumb_spec.client.fetch_spec()` — must itself call `validate(capability="m1_fetch_spec")` as its own first statement and raise if not ok, so a test, notebook, or future script that imports and calls the function directly cannot bypass the CLI's gate. This means the registry + validator module must live somewhere both `cli/` and the service modules (`bithumb_spec/`, etc.) can import without creating a `core` ↔ `broker` boundary violation — a dependency-light shared module, consistent with `.planning/research/ARCHITECTURE.md`'s "shared kernel" pattern (`spec_snapshot/`-style: importable by everyone, imports nothing from either side).

---

### 6. PyJWT construction for Bithumb private endpoints (SPEC-01)

**A discrepancy was found and must be flagged, not silently resolved.** This session's citation of Bithumb's own API documentation (`apidocs.bithumb.com`, page "인증 헤더 만들기" / "Building the Authentication Header") describes the private-REST JWT payload as: `access_key` (the issued access key), `nonce` (a unique identifier per request), and — **only when query parameters exist** — `query_hash` (SHA-512 hash of the query string) plus `query_hash_alg` set to `"SHA512"`. **No explicit `timestamp` claim was found in this citation.** This contradicts an assumption stated in the phase's own task framing (which described a `timestamp` claim as part of the payload). Do not build against either version as fact — this is exactly the kind of "documentation statement vs. observed API response" distinction D-78 exists to force through the `VERIFICATION.md` bundle. **Treat the presence/absence and exact name of a `timestamp`-like claim as an open M1 verification item** (listed below), not something this research certifies either way.

**`query_hash` construction (GET).** Per the same citation: SHA-512 hex digest of the URL-encoded, sorted query string (`hashlib.sha512(urllib.parse.urlencode(sorted(params.items())).encode()).hexdigest()`), included only when query parameters exist.

**`query_hash` construction (POST) — analogy only, not confirmed for Bithumb.** Upbit (Kakao-affiliate exchange with a documented near-identical JWT scheme — `global-docs.upbit.com/reference/auth`, which surfaced in the same search) hashes the form-urlencoded request body the same way as a GET query string. Bithumb's own docs page found this session did not explicitly confirm this for POST bodies specifically — **flag as build-time verification, established only by analogy to a sibling exchange's documented behavior, not by a Bithumb-specific citation.**

**PyJWT usage.** `jwt.encode(payload, secret_key, algorithm="HS256")` returns a `str` (not `bytes`) in PyJWT ≥2.0 — this was a breaking change from PyJWT 1.x. **Confidence: MEDIUM-HIGH** — [CITED: pyjwt.readthedocs.io, this session: "In PyJWT 2.x, `jwt.encode()` always returns a string, not bytes as in version 1.x"]. `secret_key` is the account-read secret key (a plain `str` obtained via `.get_secret_value()` on the `SecretStr`, used only inside the ephemeral scope from Finding 4 — never logged).

**Authorization header:** `Authorization: Bearer <token>`. **Confidence: MEDIUM-HIGH** — [CITED: apidocs.bithumb.com, this session, via the "Bearer ${jwtToken}" format description].

**Do not certify this claim shape as final.** This entire finding is exactly the kind of fact D-78's `VERIFICATION.md` bundle exists to attest per-fact against a real authenticated response, distinguishing "a documentation page says X" from "an observed response confirmed X." The M1 verification spike must record: the exact claim names present in a real generated/validated token, whether `timestamp` (or any similarly-named claim) exists, and the confirmed POST query-hash behavior.

---

### 7. Per-channel token-bucket rate limiter (SPEC-05)

**Recommend asyncio**, consistent with `httpx.AsyncClient` and `websockets` both already being asyncio-native per `.planning/research/STACK.md`.

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float                 # tokens per second
    tokens: float = field(init=False)
    _last_refill: float = field(init=False, default_factory=time.monotonic)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self._last_refill = now
            if self.tokens < n:
                wait_s = (n - self.tokens) / self.refill_rate
                await asyncio.sleep(wait_s)
                self.tokens = 0.0
            else:
                self.tokens -= n
```

**Per-channel means (at minimum) three distinct bucket instances**, per D-78's scope for this phase: `public_rest`, `private_rest`, `public_ws` (the WS bucket governs *connection establishment*, per `docs/RESEARCH.md` §4's note that the commonly-cited "~10/sec" figure is a connection limit, not a per-message cap — this framing itself needs M1 re-verification, not just the numeric value). **Do not invent `capacity`/`refill_rate` numbers now** — these are explicitly M1 build-time verification items (recorded in the `VERIFICATION.md` bundle per D-78), consistent with the project's general "numeric values are never invented at Gate 1 unless explicitly listed" discipline.

**Backoff on HTTP 429.** Read the `Retry-After` response header (seconds, per the HTTP spec) when present and parseable as a non-negative number, and prefer it over the D-40 computed backoff for that single retry (the server is telling you exactly how long to wait) — while still respecting `m1_spec_http_max_attempts` as the overall attempt ceiling. This "prefer server-supplied `Retry-After`, else use the exponential-backoff-with-full-jitter formula" split is a reasonable design choice for the planner to confirm, not itself a D-40-frozen rule (D-40 specifies the *fallback* backoff shape, not what happens when the server supplies its own value).

---

### 8. `httpx.AsyncClient` retry/backoff with the D-40 timeout values

```python
client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=15.0)
)
```

`connect=5.0` (5000 ms) and `read=15.0` (15000 ms) are D-40's frozen Gate-1 values, converted from ms to the seconds `httpx.Timeout` expects. **`write`/`pool` are not separately specified by D-40** — setting them equal to `read` is a reasonable, explicitly-flagged planner default, not itself a frozen number.

**`httpx` has no built-in retry mechanism** — wrap calls in an explicit ~30-line retry loop rather than reaching for a third-party retry-transport package (none is in `.planning/research/STACK.md`, and D-40's retry semantics are specific enough — network errors, connect/read timeouts, HTTP 408, HTTP 429 per valid `Retry-After`, retryable 5xx only — that a generic retry library would need heavy configuration to match anyway):

```python
import random

async def request_with_retry(client, method, url, *, max_attempts, backoff_initial_ms, backoff_cap_ms, **kwargs):
    RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.NetworkError) as exc:
            if attempt >= max_attempts:
                raise
        else:
            if resp.status_code not in RETRYABLE_STATUS:
                return resp
            if attempt >= max_attempts:
                return resp
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None and retry_after.isdigit():
                await asyncio.sleep(int(retry_after))
                continue
        delay_ms = min(backoff_cap_ms, backoff_initial_ms * (2 ** (attempt - 1)))
        await asyncio.sleep(random.uniform(0, delay_ms) / 1000)
    raise RuntimeError("unreachable")  # loop always returns or raises above
```

`max_attempts=3`, `backoff_initial_ms=500`, `backoff_cap_ms=5000`, full jitter (`random.uniform(0, delay_ms)`) per D-40. **Retryable:** network/connection errors, `ConnectTimeout`, `ReadTimeout`, HTTP 408, HTTP 429 (honoring a valid `Retry-After`), retryable 5xx (500/502/503/504). **Not retryable:** 401/403 (auth), 400/422 (validation), any other non-retryable 4xx — return/raise on the first attempt, per D-40's explicit exclusion list. **Confidence: MEDIUM** — the "httpx has no built-in retry" fact and the general `httpx.Timeout`/exception-class shapes are stable, well-known `httpx` behavior [ASSUMED] (not independently re-fetched via a live source this session — low risk, this is core, long-stable `httpx` API surface).

---

### 9. Canonical JSON + atomic write + sidecar (D-74…D-76)

**Canonicalization** (exact convention, operationalizing D-76):

```python
import json

def canonical_bytes(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
```

`sort_keys=True` + `separators=(",", ":")` (no insignificant whitespace) + `ensure_ascii=False` (keeps any non-ASCII bytes as literal UTF-8 rather than `\uXXXX` escapes, for determinism/readability if Korean text ever appears) + an explicit trailing `\n`. Hash the exact output of this function, never a re-serialization at a later point.

**Atomic write — Windows-specific correctness note.** `os.replace(src, dst)` is documented as atomic on both POSIX and Windows **only when `src` and `dst` are on the same filesystem/volume** — since this project runs on Windows (per the environment), the `.tmp-<pid>` file **must be created in the same directory as the target**, not a global OS temp directory (which may be on a different drive/volume), or the "atomic" guarantee silently degrades to a non-atomic copy+delete:

```python
import os

def atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, target)   # same directory as target => same filesystem => atomic
```

**Sidecar format:** `f"{hexdigest}  {target.name}\n"` — two spaces, matching the conventional `sha256sum`/`shasum -c` checkable format. Write the sidecar file through the same atomic-temp-then-replace helper for consistency (D-76 doesn't state this explicitly for the sidecar specifically, but treating both files identically is the more defensible, symmetric choice — flagged as a reasoned extension, not itself a separately-frozen decision).

**Windows-safe timestamp:** `datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")` → e.g. `20260908T012345Z`, matching D-74's example exactly (no `:` characters, safe as a Windows path component).

**"Never overwrite a previously consumed snapshot" — detection logic:**

```python
def guard_against_overwrite(target: Path, sidecar: Path) -> None:
    if not target.exists():
        return  # first write, nothing to guard
    existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    recorded_hash = sidecar.read_text().split()[0] if sidecar.exists() else None
    if recorded_hash == existing_hash:
        raise SnapshotAlreadyConsumedError(target)          # refuse silently overwriting
    corrupt_path = target.with_name(f"_corrupt_{utc_timestamp()}_{target.name}")
    target.rename(corrupt_path)
    raise CriticalCorruptionAlert(corrupt_path)               # tag + abort loudly
```

If the target exists and its sidecar hash matches → refuse to proceed (the artifact was already produced and is presumed consumed). If it exists and the sidecar **mismatches** → something corrupted it after the fact; rename it out of the way with a `_corrupt_<timestamp>_` prefix and abort with a critical alert, rather than silently overwriting evidence of corruption.

---

### 10. `state/runtime.sqlite3` schema policy (D-31…D-34) — Phase-1 groundwork only

**Phase 1 code MUST NOT create this file on disk.** M2/M6A own actually instantiating and populating `state/runtime.sqlite3`; any Phase-1 test of the opening-sequence pragmas below must run against `sqlite3.connect(":memory:")` or a pytest `tmp_path` fixture, never a path under the real `state/` directory.

**Opening sequence, and which PRAGMAs are per-connection vs. persistent:**

```python
conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL;")      # persists in the DB file after first set
conn.execute("PRAGMA synchronous=FULL;")       # per-connection; re-issue every open
conn.execute("PRAGMA foreign_keys=ON;")         # per-connection; SQLite does NOT persist this — must re-set every time
conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms};")  # per-connection
```

**Gotcha for the planner:** `journal_mode=WAL` is stored in the database file itself and survives across connections/restarts once set — but `foreign_keys` is explicitly **not** persisted by SQLite and must be re-enabled on every single new connection, or foreign-key constraints silently stop being enforced after a restart. Recommend issuing all four pragmas unconditionally on every connection open regardless of which ones technically persist, for clarity and defense against a future SQLite version changing persistence semantics.

**Decimal-as-canonical-TEXT (D-33).** Every price/qty/decimal-bearing column is declared `TEXT` — never `REAL` (SQLite's `REAL` is an 8-byte IEEE-754 float, exactly the precision-loss defect the project's `Decimal`-everywhere rule exists to prevent). Application code parses `Decimal(text_value)` on read and writes `str(decimal_value)` on write — the same string-not-float discipline already established for the spec-snapshot JSON (D-75) and the Gate TOML files (Finding 3).

---

### 11. structlog wiring for capability-scoped execution

```python
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,                              # custom, Finding 4
        structlog.processors.JSONRenderer(),          # production
        # swap the final renderer for structlog.dev.ConsoleRenderer(colors=True) in dev
    ],
)
```

**Confidence: MEDIUM** — [CITED: structlog.org, this session: a documented JSON-output configuration uses `merge_contextvars`, `add_log_level`, `TimeStamper(fmt="iso", utc=True)`, and `JSONRenderer`; a documented console configuration swaps in `structlog.dev.ConsoleRenderer()`]. The dev-vs-prod renderer choice should be driven by an explicit config/env toggle (e.g. `LOG_FORMAT=console|json`), not hardcoded — mechanism is planner discretion.

**Bind at CLI entry** (per invocation, via `contextvars` so every subsequent log line in that call tree inherits these fields without manual threading):

```python
structlog.contextvars.bind_contextvars(
    capability=f"{verb}.{subverb}",
    command=" ".join(sys.argv),
    invocation_id=str(uuid.uuid4()),
)
```

**Redaction processor** (repeated from Finding 4 for completeness — this is the one processor doing double duty between the secrets-handling and logging findings):

```python
def redact_secrets(logger, method_name, event_dict):
    for key, value in event_dict.items():
        if isinstance(value, SecretStr):
            event_dict[key] = "***"
    return event_dict
```

Place it after `merge_contextvars`/`add_log_level` (so it sees the fully-assembled event dict, including any bound context that might itself carry a `SecretStr`) and before the final renderer.

---

## 12. Validation Architecture

> Nyquist-style sampling matrix for Phase 1's 12 requirements (SAFE-01…07, SPEC-01…05). Test *bodies* are the planner's job — this is the architecture: what gets sampled, against which oracle, how often. Two offline oracles anchor everything M1-related: the `VERIFICATION.md` evidence bundle (`verification/bithumb/<ts>/VERIFICATION.md`, D-78) and the sanitized, committed response fixtures (`tests/fixtures/bithumb/sanitized/<endpoint>/<ts>.json`, D-77/D-79) — CI **never** calls Bithumb; every offline test replays one of these two artifact classes.

| Req ID | Requirement (from REQUIREMENTS.md) | Sampling points | Oracle | Frequency |
|---|---|---|---|---|
| SAFE-01 | Config loader holds Gate-1/2/3 decisions as immutable typed values | Unit test: mutating a `Gate1Decisions` instance raises; property test (hypothesis) generating valid/invalid field combos against `extra="forbid"` | The frozen pydantic v2 model itself (`ConfigDict(frozen=True, extra="forbid")`) | Per commit |
| SAFE-02 | Capability-scoped startup self-check refuses to run if any required decision/denominator is missing | Unit test per registry row: missing-gate / missing-cred / missing-cap fixtures each assert the correct refusal `reason`; contract test asserting every registry row has ≥1 corresponding test | `validate()`'s own documented refusal contract (Finding 3/5) | Per commit |
| SAFE-03 | Three-class key policy enforced; no key ever has withdrawal permission | Unit test: `BithumbSecrets`' field set is exactly the four documented credential fields, no "withdrawal" field is definable; negative-path test: trade creds present → rejected (D-68); static grep-CI job: no source file references a withdrawal permission/key | Structural (field-set) assertion + D-68's rejection contract | Per commit (unit/negative-path) + per PR (grep sweep) |
| SAFE-04 | Secrets loaded from env/secret store only, never `config.json`/source; secret paths git-ignored | Unit test: in-repo secrets-file path (incl. symlink fixture) is rejected; unit test: ambiguous mixed-source config is rejected; CI check: `.gitignore` contains the external secrets pattern | `Path.resolve(strict=True)` + `is_relative_to()` contract (Finding 4) | Per commit |
| SAFE-05 | Exact-decimal money type; lint/type rule rejects `Decimal(<float literal>)` | The AST checker's own positive/negative fixture suite (Finding 2) run as pytest cases; mandatory pre-commit hook; mandatory CI job (D-73) | The fixture suite itself (known-answer flag/no-flag per fixture file) | Per commit AND per PR (both pre-commit and CI, per D-73) |
| SAFE-06 | `core/` never imports `broker/`; import-direction boundary checked automatically | Import Linter negative-fixture test (Finding 1): scratch package with a deliberate violation asserts nonzero `lint-imports` exit; the real project's own `lint-imports` run as a contract test | Import Linter's documented `forbidden`-contract exit-code semantics | Per commit AND per PR (pre-commit + CI, per D-73) |
| SAFE-07 | Risk-denominator vocabulary defined and documented | Presence/shape test: whatever module/constants the planner chooses (docstring-only or typed value objects, per discretion) defines and documents `planned_stop_loss`, `max_market_loss`, `max_operational_loss`, `position_fraction`, `risk_per_trade` with non-empty docstrings | A documentation-presence assertion, not a behavioral oracle | Per commit |
| SPEC-01 | Spec adapter queries fee/tick/min-order info via JWT at startup + periodically | Offline replay test: parse a sanitized fixture into the internal fee/tick model; known-answer test on the JWT-construction function (fixed payload+secret → expected token, via PyJWT's own encode/decode round-trip); the live M1 verification spike itself (not CI-automatable) | Sanitized fixtures (D-77/D-79) for offline tests; `VERIFICATION.md` (D-78, human-approved per D-84) for the live fact | Per commit (offline) / once per M1 execution event (live, human-approved) |
| SPEC-02 | Fees recorded per experiment | Unit test: an experiment/run record's fee fields are sourced from the loaded snapshot, never hardcoded | The snapshot-consumption contract (Finding 9's guard-against-overwrite / sidecar-validated load path) | Per commit |
| SPEC-03 | Price-tick/qty-step rounding + minimum-order rules pass boundary unit tests | Property tests (hypothesis) at exact tick boundaries, one-tick-below, one-tick-above, zero | Boundary-value correctness against the documented rounding rule (direction chosen per D-49's "never round up a user-controlled spend/sell quantity") | Per commit |
| SPEC-04 | Fee/tick/min-order snapshot persisted as a hashed artifact, never re-queried mid-simulation | Tamper-detection test (mutate one byte, assert sidecar mismatch caught); never-overwrite test; corrupt-rename test; Import-Linter-style check that `core/`/`simulator`-side consumers never import `bithumb_spec`'s network client | Finding 9's atomic-write/sidecar/guard functions | Per commit |
| SPEC-05 | Per-channel token-bucket rate limiting + backoff for public REST, private REST, WebSocket | Unit tests on `TokenBucket.acquire()` using a monkeypatched/fake clock (never real `asyncio.sleep` durations in CI); retry-loop test asserting `Retry-After` is honored and non-retryable status codes are never retried | Finding 7/8's token-bucket and retry-loop logic, exercised with a controlled fake clock | Per commit |

---

## Open Verification Items (deferred to M1 execution)

1. **JWT claim shape — does a `timestamp` (or similarly-named) claim exist in Bithumb's private-endpoint JWT payload?** This session's citation of `apidocs.bithumb.com` found `access_key`, `nonce`, and conditional `query_hash`/`query_hash_alg` — no `timestamp` claim was found, which conflicts with an assumption in the phase's task framing. Resolve via a real generated/observed token during the M1 verification spike; record in `VERIFICATION.md`.
2. **`query_hash` construction for POST request bodies** — confirmed by citation for GET query strings; assumed by analogy to Upbit's documented scheme for POST bodies, not independently confirmed for Bithumb.
3. **Per-channel rate-limit numeric values** (`public_rest`, `private_rest`, `public_ws` capacity/refill figures) — explicitly deferred to M1 build-time verification per D-78; do not invent numbers.
4. **Whether `/v1/orders/chance`'s reported fee applies to the legacy automatic-order product** — explicitly called out as unverifiable from that endpoint alone (D-96/G1.11 area); moot for Phase 1 since the legacy stop-limit is out of v1 scope (D-17/D-94), but the spec-snapshot schema's `verification_status` fields should still record this as `unresolved_until_M6B` or similar if ever queried.
5. **Exact pydantic v2 strict-mode interaction between `Annotated[Decimal, BeforeValidator(...)]` and `ConfigDict(strict=True)`** at the specific pinned `pydantic` version — the design in Finding 3 is standard, documented usage, but was not independently re-verified against the exact pinned version this session (context7 unavailable); a quick smoke test at build time is cheap insurance.
6. **Amount unit, volume step, fee currency, and buy-side fee-reservation semantics** (D-49/D-54) — explicitly M1 verification items feeding the simulator's rounding-order invariants; out of this research pass's scope (M2 concern), noted here only because SPEC-01's expanded scope (D-100) touches the same adapter.
7. **Private WebSocket path v1-vs-v2** — already flagged as an unresolved factual conflict in `.planning/research/PITFALLS.md` Pitfall 11; D-92 freezes it as v2 for this project's Decision Register, but the live path string itself is still an M1/M6A build-time verification item, not re-litigated here.

## Sources

### Primary (HIGH/MEDIUM confidence, official docs, cited this session)
- [Import Linter — Usage docs (stable)](https://import-linter.readthedocs.io/en/stable/usage.html) — `root_package`, `exclude_type_checking_imports`, TOML-embedding shape. MEDIUM (official docs, web search this session)
- [Import Linter — Forbidden contract type](https://import-linter.readthedocs.io/en/stable/contract_types/forbidden/) — `forbidden` contract field semantics. MEDIUM
- [PyJWT — Usage Examples (latest)](https://pyjwt.readthedocs.io/en/latest/usage.html) — `jwt.encode()` returns `str` in PyJWT ≥2.0, HS256 usage. MEDIUM-HIGH (official docs, web search this session)
- [pydantic-settings / Pydantic Settings Management docs](https://docs.pydantic.dev/dev/concepts/pydantic_settings/) — `env_prefix`, `case_sensitive`, `secrets_dir`, source precedence. MEDIUM (official docs, web search this session)
- [structlog — Getting Started](https://www.structlog.org/en/stable/getting-started.html) — processor chain shapes for JSON and console output. MEDIUM (official docs, web search this session)
- [apidocs.bithumb.com — 인증 헤더 만들기 (Building the Authentication Header)](https://apidocs.bithumb.com/docs/%EC%9D%B8%EC%A6%9D-%ED%97%A4%EB%8D%94-%EB%A7%8C%EB%93%A4%EA%B8%B0) — JWT payload fields (`access_key`, `nonce`, `query_hash`, `query_hash_alg`), `Authorization: Bearer` header. MEDIUM (official venue docs, web search this session — this is a *documentation citation*, not an observed live response; D-78's evidence-class distinction applies. **`timestamp` claim not found in this citation — see Open Verification Items #1.**)
- [global-docs.upbit.com — Auth reference](https://global-docs.upbit.com/reference/auth) — sibling-exchange analog for POST-body `query_hash` construction. LOW-MEDIUM (used only for the by-analogy POST-body inference in Finding 6, explicitly flagged as unconfirmed for Bithumb)

### Secondary (project-internal, authoritative, re-read this session)
- `.planning/phases/01-safety-foundation-bithumb-spec-adapter/01-CONTEXT.md` — the full frozen Gate-1 Decision Register (D-01…D-100); every D-number cited above traces here.
- `docs/EXECUTION.md` §Gate 1, §M0, §M1 — milestone spine and Decision Register source of truth.
- `docs/RESEARCH.md` §4, §5 — Bithumb integration facts, security/idempotency model.
- `.planning/research/STACK.md` — confirms no CLI library is pre-vetted (informs Finding 5's `argparse` verdict); confirms the `httpx`/`websockets`/`PyJWT`/`pydantic`/`pydantic-settings`/`structlog` stack this research builds on top of.
- `.planning/research/ARCHITECTURE.md` — shared-kernel / import-boundary pattern referenced in Finding 5's defence-in-depth discussion.
- `.planning/research/PITFALLS.md` Pitfall 11 — private WS v1-vs-v2 conflict, cross-referenced in Open Verification Items #7.

### Tertiary (stable stdlib/library knowledge, not independently re-fetched this session — [ASSUMED] per this session's provenance rule, low risk)
- `tomllib` binary-mode file requirement; `ast` module `Call`/`Attribute`/`Name`/`UnaryOp`/`Constant` node shapes; `pathlib.Path.resolve(strict=True)` / `Path.is_relative_to()` symlink-safe containment check; `os.replace()` same-filesystem atomicity; SQLite `PRAGMA foreign_keys` non-persistence across connections vs. `journal_mode=WAL` persistence; `httpx` having no built-in retry mechanism; `pydantic.SecretStr` masked-repr behavior.

## Metadata

**Confidence breakdown:**
- Import Linter / Decimal AST checker wiring: MEDIUM-HIGH — mechanics are either official-docs-cited this session or stable, low-risk stdlib/tool behavior; the one genuine unknown (exact pre-commit `language:` choice) is flagged.
- Gate-1 TOML loader / secrets loading: MEDIUM — design is a direct synthesis of already-frozen D-numbers plus stable pydantic v2 / pydantic-settings patterns; the ambiguous-mixed-config gap and the pydantic strict-mode interaction are flagged as build-time smoke-test items.
- Bithumb JWT construction: MEDIUM, with one flagged discrepancy (Finding 6) — this is the area of highest remaining uncertainty in this research pass and is explicitly deferred to the M1 `VERIFICATION.md` bundle, per the phase's own non-goal against re-running that verification here.
- Validation Architecture: MEDIUM-HIGH — sampling points follow mechanically from the findings above; oracle assignments for SPEC-01 correctly separate the offline-fixture-replay tier from the once-per-execution live-verification tier per D-78/D-79's evidence-class distinction.

**Research date:** 2026-09-08
**Valid until:** 30 days for the pure-Python/stdlib findings (Import Linter, AST checker, SQLite, JSON/atomic-write); 7 days for anything touching `apidocs.bithumb.com` claims (Finding 6) since that surface is explicitly flagged elsewhere in this project as subject to change/re-verification at build time.
