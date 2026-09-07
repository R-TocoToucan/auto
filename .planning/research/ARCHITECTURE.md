# Architecture Research

**Domain:** Honest quant backtest + walk-forward validation system for a single-asset (KRW-BTC), long-or-cash Bithumb spot bot — build-only milestone (no live trading; ends at a one-time holdout evaluation of a frozen artifact).
**Researched:** 2026-09-07
**Confidence:** HIGH for component boundaries and build order (derived directly from `docs/RESEARCH.md` §3–§5 and `docs/EXECUTION.md`, both treated as authoritative, project-internal, cross-audited sources). MEDIUM/LOW for the general software-architecture patterns used to structure those requirements (event-driven backtest engines, purged/embargoed CV, reproducible-artifact hashing, exchange-bot idempotency) — these are corroborating industry practice pulled from web search, not project-specific, and are used only to justify *how* to implement decisions the docs already made, never to override them.

---

## Standard Architecture

### System Overview

Two hard rules shape every box below, both stated in `EXECUTION.md`: (1) nothing in the system may execute a fill earlier than *t+1* relative to its signal candle, and (2) the artifact that the holdout evaluates must be **frozen and hashed**, which requires a strict, enforced import boundary between the backtest/research core and the live-broker package. Everything else — how data flows, where state machines live, how the walk-forward harness is organized — is designed to make those two rules mechanically true rather than merely documented.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  GATE-1/2/3 DECISION REGISTER (config, versioned, human-authored)          │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │ loaded + validated at startup
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  CONFIG LOADER + STARTUP SELF-CHECK                                        │
│  fails closed if any Gate-1 decision / risk denominator is missing         │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │ ResolvedConfig (immutable, hashable)
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
┌───────────────┐           ┌────────────────┐           ┌──────────────────┐
│ BithumbSpec    │           │ BithumbData     │           │ (later) M6A/M6B  │
│ (auth READ,    │           │ (candle ingest, │           │ BithumbBroker    │
│ JWT, network)  │           │ pagination,     │           │ package — real   │
│ → fee/tick/    │           │ dedupe, tz/gap, │           │ network + auth,  │
│ min-order      │           │ 6h-from-60m)    │           │ ISOLATED (§below)│
│ SNAPSHOT       │           │ → IMMUTABLE     │           └──────────────────┘
│ (hashed, JSON) │           │ candle STORE    │
└───────┬────────┘           └────────┬────────┘
        │                             │
        └──────────────┬──────────────┘
                        ▼
        ══════════ CORE / RESEARCH PACKAGE (offline, deterministic, no network, no wall-clock) ══════════
┌───────────────────────────────────────────────────────────────────────────┐
│  SIMULATOR (execution engine)                                              │
│  temporal separation (fill ≥ t+1) · intrabar+gap rule · stop-limit state   │
│  machine (WATCH→WAIT→PARTIAL/DONE/CANCEL) · cost model (fees+slip+impact)  │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │ deterministic fill/trade ledger
┌───────────────────────────────────┴───────────────────────────────────────┐
│  STRATEGY / SIGNAL LAYER                                                   │
│  baseline rule (M4) + one-at-a-time modules (M5): ATR sizing, stop choice, │
│  trailing stop, vol-target overlay, regime gate, kill-switch               │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │ per-fold equity/return series + metrics
┌───────────────────────────────────┴───────────────────────────────────────┐
│  WALK-FORWARD / VALIDATION HARNESS                                         │
│  data-role guard (train/selection/holdout) · nested folds · DSR/PBO/       │
│  block-bootstrap · E∧R Boolean acceptance table                           │
└───────────────────────────────────┬───────────────────────────────────────┘
        ══════════════════════════════════════════════════════════════════════
                                     │
                                     ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  ARTIFACT FREEZE + HASH BOUNDARY (a gate, not a running component)         │
│  hashes: core-package source commit · config · dataset · fee/tick snapshot│
│  · cost model · stop mechanism · statistical protocol                     │
│  ENFORCED RULE: core package imports nothing from the broker package      │
└───────────────────────────────────┬───────────────────────────────────────┘
                                     │
                                     ▼
                    ── open the final holdout exactly once ──
                                     │
                                     ▼
                    M6B: broker package now goes live (trade key)
```

### Component Responsibilities

| Component | Responsibility | Typical Implementation |
|-----------|----------------|-------------------------|
| Config/Decision-Register loader | Parse Gate-1 (and later Gate-2/3) decisions + risk denominators into one immutable, hashable object; startup self-check refuses to run (fail-closed) if anything required is missing or a denominator is ambiguous | Small schema (dataclasses/typing + manual validators is sufficient at this scale — no need for a heavyweight config framework); loader is pure and offline; emits a `config_hash` |
| BithumbSpec | Authenticated (JWT, account/read key), **network-facing**, read-only query of `/v1/orders/chance` for fees/tick/min-order/supported order types; run out-of-band (startup + periodically), never inside the simulation hot path | A thin adapter whose sole output is a timestamped, hashed **snapshot** artifact (e.g. JSON) that the core package consumes as static data — never re-queried mid-backtest |
| BithumbData | **Network-facing** ingestion: REST pagination (≤200/req), dedupe, timezone validation, gap handling; writes to an **immutable, append-only** candle store with integrity checks; derives 6h bars from 60m (or trades), never from 240m, as a re-verifiable pure function over the raw store | Partitioned files or a local embedded DB (SQLite/Parquet) keyed by (market, interval, open_time); content-hash per ingested range; aggregation re-derivable and unit-tested against hand-checked bars, not hand-maintained as separate mutable data |
| Simulator | Pure, deterministic, **offline** execution engine: enforces fill ≥ t+1, the intrabar/gap rule for market/marketable exits, the Gate-1 stop-limit state machine, and the cost model (fees from BithumbSpec snapshot + modeled/calibrated slippage & impact) | A staged transform over the immutable candle store + config + orders, not a generalized live-style event bus (see Anti-Pattern 2) — single-asset v1 does not need multi-strategy pub/sub |
| Strategy/Signal layer | Baseline rule (M4) plus swappable, independently-gated modules (M5): ATR sizing, ATR-vs-fixed-% stop, trailing stop, vol-target overlay, regime gate, kill-switch | One common `Strategy` interface (closed-candle history + config → target orders); each module added and evaluated in isolation per the M5 module-ordering policy, dropped (not retuned) on gate failure |
| Validation/walk-forward harness | Owns the **three disjoint data roles** (train/selection/holdout); runs nested walk-forward folds through Simulator+Strategy; computes the primary metric, PBO/CSCV, DSR (only if Sharpe-based), block-bootstrap CIs; applies the E∧R Boolean acceptance table | Physically withholds holdout-labeled rows from every selection-stage call (a runtime guard, not just a convention) so "selection windows only" is enforced by code, and the holdout is a single explicit, logged call site |
| Artifact freeze/hash boundary | Not a running component — a **build gate**. After M6A, snapshot and record: core-package source commit hash, config hash, dataset hash, fee/tick snapshot hash, cost-model version, stop-mechanism definition, statistical-protocol version | A `freeze_artifact.py`-style script (or CI job) that computes and writes a `FROZEN_ARTIFACT.json` manifest; any post-freeze diff against it is the operational definition of a "material change" candidate |
| BithumbBroker package (M6A mock → M6B live) | Order placement/cancel against v2 endpoints and the Gate-1 stop mechanism; both auth schemes (JWT + legacy `Api-Key/Api-Nonce/Api-Sign` if the legacy stop-limit is in scope); idempotency (client_order_id or legacy intent-log + reconcile-before-retry); durable transactional state; startup reconciliation | A separate top-level package/module tree with a hard, **CI-enforced** import-direction rule: this package may depend on shared pure contracts (money type, the stop-mechanism *definition*, the BithumbSpec snapshot schema) but the core/research package must never import from it |
| Watchdog | Independent process/thread from the main trading loop; detects a dead client-side stop monitor, a failed cancel, or a stuck reconciliation; can halt trading even if the primary process is unresponsive | Runs out-of-process (separate service or scheduled job) so a crash in the trading loop cannot also silence the watchdog |

---

## Recommended Project Structure

```
src/
├── config/                    # Decision-register schema, loader, startup self-check
│   └── decisions.py           # Gate-1/2/3 fields, risk denominators, ResolvedConfig + hash
├── spec_snapshot/              # Pure data contracts shared by core AND broker (no I/O)
│   ├── schema.py               # Fee/tick/min-order snapshot shape; stop-mechanism definition
│   └── snapshots/               # Frozen, hashed JSON snapshots produced by bithumb_spec/
├── bithumb_spec/                # M1 — authenticated READ adapter (network, JWT)
│   └── client.py                # Queries /v1/orders/chance, writes a spec_snapshot
├── bithumb_data/                 # Candle ingestion (network) → immutable store
│   ├── ingest.py                  # Pagination, dedupe, tz validation, gap handling
│   ├── aggregate.py                # 6h-from-60m (never 240m), pure + unit-tested
│   └── store/                      # Immutable candle files/DB + integrity checks
├── core/                            # ═══ THE HASHED ARTIFACT — offline, deterministic ═══
│   ├── simulator/                    # M2 — temporal separation, intrabar+gap, cost model
│   │   ├── engine.py
│   │   └── stop_limit_fsm.py          # WATCH → WAIT → PARTIAL/DONE/CANCEL
│   ├── strategy/                       # M4 baseline + M5 modules (one interface, swappable)
│   │   ├── baseline.py
│   │   └── modules/                     # atr_sizing.py, trailing_stop.py, regime_gate.py, ...
│   └── validation/                       # M3 preregistration + walk-forward harness
│       ├── harness.py                     # train/selection/holdout role guard, folds
│       └── stats.py                        # DSR, PBO/CSCV, block bootstrap, E∧R table
├── broker/                                  # M6A (mock) / M6B (live) — ISOLATED package
│   ├── mock_broker.py                        # M6A: place/cancel v2 order types, partials
│   ├── live_broker.py                         # M6B only, built after the holdout opens
│   ├── idempotency.py                          # client_order_id + legacy intent-log
│   ├── reconciliation.py                        # state diff vs. exchange, startup recon
│   └── watchdog.py                               # independent process
├── artifact/                                       # Freeze/hash boundary tooling (a gate, not a service)
│   └── freeze.py                                    # writes FROZEN_ARTIFACT.json
└── cli/                                               # Entry points per milestone (M0 self-check, M4 run, etc.)
```

### Structure Rationale

- **`config/` and `spec_snapshot/` sit outside `core/`** but `core/` may read their *outputs* (a `ResolvedConfig` object, a frozen JSON snapshot) without importing `bithumb_spec/`'s network client — the boundary is "no network I/O reachable from `core/`," which is stricter than and subsumes "no broker imports."
- **`bithumb_spec/` and `bithumb_data/` are network-facing but still excluded from the live-broker isolation problem** — they run *before* a backtest, produce immutable artifacts, and are never invoked from inside `core/` at evaluation time. Their code is still part of the source-commit hash (it must exist and be correct before freeze), but their *execution* is out-of-band.
- **`core/` is the literal definition of "the hashed artifact."** Nothing under `core/` performs network I/O, reads the wall clock, or imports from `broker/`. This makes the artifact-freeze step mechanical: hash the `core/` source tree + `config/` + the dataset file + the spec snapshot file, and you have reproduced exactly what `docs/EXECUTION.md`'s M5-FINAL-FREEZE asks for.
- **`broker/` is a separate top-level package specifically so the "package boundary" clause in M5-FINAL-FREEZE is enforceable, not aspirational.** Use an import-linter (or an equivalent static check run in CI) with a rule like "`core` must not import `broker`" — a code review comment is not sufficient given the stakes (a violated boundary silently invalidates the freeze).
- **`spec_snapshot/schema.py` is the shared kernel.** The stop-mechanism *definition* (states, transitions, the deterministic candle-only tie-break rule) must be identical between the simulator's state machine (`core/simulator/stop_limit_fsm.py`) and whatever the live broker actually submits/tracks (`broker/live_broker.py`), or M6B's real behavior silently diverges from what was validated. Putting the *definition* in a dependency-free schema module both packages can import (but that itself imports nothing from either) keeps them consistent without recreating the isolation problem.

---

## Architectural Patterns

### Pattern 1: Immutable-snapshot boundary around all network I/O

**What:** Every component that talks to Bithumb over the network (`bithumb_spec`, `bithumb_data`, and later `broker`) produces a versioned, hashed, file-based artifact as its only output that the rest of the system consumes. Nothing downstream re-queries the network mid-run.
**When to use:** Always, for this project — it is what makes "freeze and hash the artifact" possible at all, and it is what makes the holdout evaluation reproducible byte-for-byte.
**Trade-offs:** Slightly more ceremony than "just call the API when you need a number," but it is the only way to (a) make the simulator deterministic and (b) make "what exactly did the holdout see" an auditable question with a hash-backed answer, not a claim.

### Pattern 2: Staged pipeline, not a generalized event bus

**What:** Industry event-driven backtesters (Backtrader, Zipline, QuantStart-style engines, NautilusTrader) route MARKET → SIGNAL → ORDER → FILL events through a central queue so the same code can run in backtest and live modes. For a **single-asset, single-strategy, no-live-trading-this-milestone** system, that generality is not needed yet: a simpler staged pipeline (ingest → simulate → evaluate) gets the same look-ahead-safety guarantee (sequential processing that never lets a later timestamp inform an earlier decision) with far less machinery.
**When to use:** Keep the staged-pipeline shape through M2–M5. Revisit an event-bus shape only if a later milestone (out of this project's scope: multi-coin, live automated operation) needs to interleave multiple concurrent data/strategy streams in real time — M6A/M6B's live broker loop is the one place an event/queue shape is genuinely warranted, and it is already isolated in its own package.
**Trade-offs:** A staged pipeline is easier to reason about, test, and hash, but the interface between `core/strategy` and `core/simulator` should still be designed as if orders were discrete events (target position / order intent objects, not implicit function-call side effects) — that keeps the eventual M6A/M6B live loop from requiring a rewrite of the strategy layer, even though the backtest itself doesn't need a literal queue.
**Confidence:** MEDIUM (general architecture pattern, corroborated across multiple independent backtesting-engine write-ups, applied here as an explicit simplification for YAGNI reasons rather than inherited wholesale).

### Pattern 3: Explicit state machine for anything with an ambiguous "unfilled" outcome

**What:** The stop-limit lifecycle (`WATCH → WAIT → PARTIAL/DONE/CANCEL`) and the intrabar/gap rule are modeled as an explicit finite state machine with a predeclared, deterministic tie-break (no fill unless trade data establishes trigger-before-fill ordering) — not as an implicit branch buried in the fill-calculation function.
**When to use:** Any exit mechanism where "no fill, retained exposure" is a valid and *conservative* outcome (unlike a market order, where "worse price" is the conservative outcome). This is exactly the Gate-1 stop-limit case documented in `RESEARCH.md` §3.1 and `EXECUTION.md` M2.
**Trade-offs:** More upfront design than a single `if price crossed stop: fill` branch, but it is the only way to make the many documented edge cases (gap-below-a-sell-limit, partial-then-decline, cancel-vs-fill races, a triggered order becoming an ordinary resting limit) individually testable with known-answer replay tests, per the M2 test checklist.

### Pattern 4: Data-role access control as a runtime guard, not a naming convention

**What:** The validation harness does not just *label* rows train/selection/holdout in a config file — it structurally withholds holdout-tagged data from every code path except the single, explicitly logged final-evaluation call. Concretely: the harness exposes `get_training_view()` / `get_selection_folds()` during M3–M5, and `get_holdout()` either doesn't exist as a callable until the artifact-freeze manifest has been written, or raises if called more than once / before freeze.
**When to use:** From M3 onward. This is the single highest-leverage guard against the exact failure mode the whole project exists to prevent — a holdout that quietly informed a decision.
**Trade-offs:** Adds a small amount of harness complexity (the "give me all the data" convenience path has to be disabled outside of ad hoc exploratory notebooks that are clearly out of the frozen pipeline), which is precisely the point.

### Pattern 5: Config/Decision-Register as a fail-closed, hashable value object

**What:** All Gate-1 (and later Gate-2/3) decisions load into one immutable object at startup; the self-check enumerates every required field/denominator and refuses to proceed (loud failure, not a silent default) if anything is missing or a unit/denominator is ambiguous (e.g., "0.9%" without stating "of total equity").
**When to use:** M0, and every milestone after that reads config — this is what turns "the docs say denominators must be explicit" into an enforced runtime property instead of a code-review hope.
**Trade-offs:** None significant at this scale — a hand-rolled dataclass + validator function is enough (stdlib `dataclasses` + `decimal.Decimal` for money fields); no need for a config-management framework for a single-operator, single-asset system.

---

## Data Flow

### Ingestion → Simulation → Validation → Freeze → Holdout

```
Bithumb REST/WS
     │ (network, out-of-band)
     ▼
BithumbSpec ──► fee/tick/min-order SNAPSHOT (hashed JSON)   ─┐
     │                                                        │
BithumbData ──► pagination/dedupe/tz/gap ──► IMMUTABLE        │
     │                                        candle STORE     │
     │                                        (60m + 240m      │
     │                                        native; 6h        │
     │                                        aggregated        │
     │                                        from 60m)          │
     ▼                                                            │
             ═══ everything below this line is OFFLINE, DETERMINISTIC ═══
                                                                    │
     ┌──────────────────────────────────────────────────────────┘
     ▼
Validation Harness assigns three DISJOINT data roles over the store:
   TRAIN (fit)  →  SELECTION folds (choose/gate modules, nested walk-forward)  →  HOLDOUT (sealed)
     │                              │                                              │
     ▼                              ▼                                              │
 Strategy fit               Strategy + Simulator run per fold,                     │
 (baseline + modules,       metrics computed, E∧R Boolean gate applied,            │
 M4/M5, selection-only)     PBO/DSR/bootstrap per Gate-2 protocol                  │
     │                              │                                              │
     └──────────────┬───────────────┘                                              │
                     ▼                                                             │
        Strategy FREEZE (M5-strategy-freeze: signal, params, sizing,               │
        stop/exit, cost model, statistical protocol — all fixed)                   │
                     │                                                             │
                     ▼                                                             │
        M6A: broker package built (mock/read-only) — ISOLATED from core           │
                     │                                                             │
                     ▼                                                             │
        ARTIFACT FREEZE + HASH (core source commit + config + dataset +           │
        spec snapshot + cost model + stop mechanism + stat protocol)              │
                     │                                                             │
                     ▼                                                             │
        Gate-3 risk limits recorded                                                │
                     │                                                             │
                     ▼                                                             │
        HOLDOUT opened exactly once ◄───────────────────────────────────────────────┘
        (frozen strategy evaluated by frozen simulator on holdout-role data)
                     │
                     ▼
        M6B: live broker (real key) — only package that changes post-holdout;
        a MATERIAL change to core/simulator/cost forces re-freeze + a NEW
        holdout drawn only from FORWARD data collected after the re-freeze
        (a previously-inspected historical period can never become a new holdout)
```

### Key Data Flows

1. **Config resolution:** Decision-register files → ConfigLoader → self-check (fail closed) → one immutable `ResolvedConfig` consumed by every downstream component; its hash is one of the six inputs to the final artifact hash.
2. **Data acquisition (out-of-band):** `BithumbSpec` and `BithumbData` are the only components that touch the network before the holdout opens (aside from the M6A mock, which touches nothing real). Their outputs are files, not live calls — the simulator and harness never phone home.
3. **Simulation:** For each candidate strategy/module, the harness feeds `(ResolvedConfig, candle store slice, spec snapshot)` into the Simulator, which enforces temporal separation and returns a deterministic per-period return/trade ledger — the same ledger given the same inputs, every time (this determinism is itself a testable property: replay must be bit-identical).
4. **Role-gated selection:** Every module (M5) sees only train+selection data; the harness's access guard is the single control point that must never be bypassed, because it is the only thing standing between "preregistered validation" and "the holdout quietly informed a choice."
5. **Freeze → holdout:** The freeze step is a pure function of already-existing, already-hashed artifacts (it doesn't compute anything new about the strategy — it records what already exists). The holdout call is the one place the harness's guard permits reading holdout-role data, and it must be logged/idempotent so it is provably opened only once.
6. **Post-holdout divergence:** After the holdout, only the `broker/` package is expected to change (M6B is literally "make the mock real"). If a real-fill discovery forces a change inside `core/`, that is by definition material (per the doc's material-change list) and burns the holdout; the import-isolation boundary is what lets you *quickly and confidently* tell whether a given M6B bugfix is confined to `broker/` (safe) or leaked into `core/` (burns it) — this is the concrete payoff of enforcing the package boundary rather than merely stating it.

---

## Scaling Considerations

This project explicitly does not scale to "more users" — it scales along different axes: data volume, module count, and fold count. Framed against the milestone spine:

| Scale axis | Now (v1, this milestone) | If it grows (still v1) | Out of scope (v2+) |
|---|---|---|---|
| Assets | 1 (KRW-BTC) | — | Multi-coin universe needs survivorship/point-in-time handling — explicitly deferred |
| History length | Whatever Bithumb's 240m history covers | Longer history just means more immutable-store files; ingestion is already paginated/dedupe-safe | — |
| Strategy modules | Baseline + up to ~6 M5 modules, evaluated one at a time | Module-ordering/interaction policy (Gate 2) already anticipates this; keep modules behind one interface so adding one doesn't touch the simulator | A combinatorial module search is explicitly against the "add one at a time, drop don't retune" discipline — resist it even if tooling makes it easy |
| Walk-forward folds | Small, nested, nothing exotic | Fold evaluation is embarrassingly parallel (each fold is independent given frozen config+data) — parallelize before optimizing the simulator itself | — |
| Concurrency/live throughput | None — M6A is mock/read-only, no real order flow | — | M7/M8 real automated operation is out of this milestone entirely |

### Scaling Priorities

1. **First likely friction point:** walk-forward fold count × module count as M5 progresses — mitigate by making each `(fold, module-config)` evaluation a pure, independently cacheable function of its inputs (content-addressed by config+data hash) so re-running M5 after adding one module doesn't require re-simulating everything.
2. **Second:** candle-store size if history is long and 60m data is retained for 6h aggregation — a simple partitioned file/SQLite store with per-range hashes is sufficient at this scale; do not reach for a distributed data store for a single-asset system.

---

## Anti-Patterns

### Anti-Pattern 1: Same-candle execution (the defect this whole project exists to fix)

**What people do:** Compute a signal from a closed candle and fill at that same candle's close, because it's the simplest thing to code (this is literally `bot.py`'s defect, per `RESEARCH.md` §0).
**Why it's wrong:** It assumes execution at an already-known price. No amount of signal sophistication fixes a wrong fill model — the bias direction is undetermined, so "it usually works out" cannot be assumed either.
**Do this instead:** Structural temporal separation in the Simulator itself (fill ≥ t+1, enforced by the type/shape of the interface between Strategy and Simulator, not by a code comment) — see Pattern 2 and Pattern 3.

### Anti-Pattern 2: Generalized live-trading event architecture applied prematurely to the backtest

**What people do:** Reach for a full pub/sub event bus (as real live-trading systems need) for a single-asset, no-live-trading-this-milestone backtest, because that's what tutorials/frameworks show.
**Why it's wrong:** Adds indirection and testing surface with no payoff at this milestone's scope, and — worse — makes the "what exactly is deterministic and hashed" question harder to answer, since queues/timers introduce implicit ordering assumptions.
**Do this instead:** Staged pipeline for the backtest (Pattern 2); reserve event/queue architecture for the already-isolated `broker/` package where live asynchrony is real.

### Anti-Pattern 3: Treating the broker/core import boundary as a naming convention

**What people do:** Put broker code in a differently-named folder and assume nobody will import across it.
**Why it's wrong:** `EXECUTION.md`'s M5-FINAL-FREEZE explicitly says this boundary "must be defined and enforced... not assumed." An accidental import (e.g., a shared "utils" module that both sides depend on and that itself pulls in the broker's network client) silently invalidates the freeze and nobody notices until a holdout-burn investigation.
**Do this instead:** A CI-enforced dependency-direction check (import-linter or equivalent) that fails the build if `core/` imports anything from `broker/`, plus a genuinely dependency-free shared-contract module for the handful of things both sides must agree on (Pattern 5's "shared kernel").

### Anti-Pattern 4: Re-deriving 6h candles as independently fetched/stored data

**What people do:** Fetch or hand-populate a separate 6h candle series, because it's tempting to treat it like any other native interval.
**Why it's wrong:** Bithumb has no native 360-minute interval; a 6h series stored independently of its 60m source can silently drift from the aggregation definition (boundary/timezone/gap handling changes without the derived series being regenerated), and 240m cannot produce it at all (240 doesn't divide into 360-minute boundaries cleanly against a 6-per-day grid the way 60m does).
**Do this instead:** Store only the native intervals (BithumbData); derive 6h as a pure, unit-tested function over the 60m (or trade) store, re-run whenever the source range changes — never hand-maintained as separate mutable data.

### Anti-Pattern 5: Letting "read-only" broker code run inside the simulation loop

**What people do:** Call `BithumbSpec` (or, later, the mock/live broker) directly from inside the simulator/strategy loop "just to get a current number," because it's convenient and it's "only a read."
**Why it's wrong:** Breaks determinism (the same backtest run can now produce different results depending on when it was run), breaks offline reproducibility (no network = no backtest), and quietly reintroduces a live dependency into what is supposed to be the frozen, hashed artifact.
**Do this instead:** Pattern 1 — snapshot first, consume the snapshot everywhere else.

### Anti-Pattern 6: Relying on programmer discipline for the train/selection/holdout boundary

**What people do:** Keep the holdout in the same dataframe/store as everything else, trusting that "we just won't look at those rows until the end."
**Why it's wrong:** This is precisely the failure mode multiple-testing/overfitting corrections (PBO, DSR, purged CV) exist to catch after the fact — better to prevent it structurally, since a preregistered protocol that was actually violated in code is worse than no preregistration (false confidence).
**Do this instead:** Pattern 4 — a runtime guard in the harness that makes "accidentally reading the holdout during selection" a thrown exception, not a possibility.

---

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Bithumb public REST (candles) | `bithumb_data/ingest.py`, out-of-band, paginated (≤200/req), deduped | Native intervals 1/3/5/10/15/30/60/240 min only; validate timezone explicitly; handle gaps rather than silently interpolating |
| Bithumb private REST (`/v1/orders/chance`) | `bithumb_spec/client.py`, JWT-authenticated account/read key, out-of-band | Output is a snapshot artifact, not a live dependency; re-verify at build time which account permission this endpoint actually requires (per `EXECUTION.md` M1) |
| Bithumb legacy automatic-order endpoint (`/trade/stop_limit`) | Only if Gate-1 selects it; separate `Api-Key`/`Api-Nonce`/`Api-Sign` auth scheme, no `client_order_id` | Lives entirely in `broker/`; the *definition* of its state machine is shared with `core/simulator/stop_limit_fsm.py` via the dependency-free `spec_snapshot/schema.py`, but the network client itself is isolated |
| Bithumb public/private WebSocket | `broker/` only (M6A mock now, M6B real later); public WS `wss://ws-api.bithumb.com/websocket/v1`, private WS v2 | Per-channel rate limits, not one global cap — separate token buckets/backoff per channel; out of scope for `core/` entirely in this milestone |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `config/` → everything | One immutable `ResolvedConfig` value object, passed down, never mutated | Its hash is a freeze-manifest input; nothing recomputes config mid-run |
| `bithumb_spec/` / `bithumb_data/` → `core/` | File-based artifacts (JSON snapshot, immutable candle store), never a live call | This is the honesty boundary that makes offline determinism possible |
| `core/simulator` → `core/strategy` | Discrete order/target-position objects passed at candle boundaries (event-shaped even though the pipeline is staged, per Pattern 2) | Keeps the interface stable if a later, out-of-scope milestone needs a real event loop |
| `core/validation` (harness) → `core/simulator` + `core/strategy` | Harness owns which data-role slice is visible for a given call; simulator/strategy are unaware of data roles — they just run on whatever slice they're handed | Keeps the role-guarding logic in exactly one place (Pattern 4) |
| `core/` ↔ `broker/` | **One-directional only, and only via `spec_snapshot/`'s dependency-free contracts.** `core` must not import `broker`; `broker` may import shared contracts but not `core/simulator` or `core/strategy` internals | This is the boundary `EXECUTION.md` explicitly calls out as needing enforcement, and the one most likely to be violated by a well-meaning "shared utils" module if not actively guarded |
| `artifact/freeze.py` → everything | Read-only: hashes source trees/files, writes a manifest; never itself computes strategy behavior | Keeps "freezing" from ever becoming a place where behavior could silently change |
| Watchdog → broker | Separate process/service, not a thread inside the trading loop | So a hang/crash in the primary loop can't also disable the safety check — this only matters once M6A/M6B are live-connected, but the isolation should exist from M6A |

---

## Sources

- **Primary (HIGH confidence, project-internal, authoritative):** `docs/RESEARCH.md` §3 (validation spine: temporal separation, intrabar+gap, stop-limit state machine, cost model, preregistration/walk-forward/DSR/PBO/bootstrap, holdout discipline), §4 (Bithumb integration facts: order types, candle intervals/pagination, rate limits, WS endpoints), §5 (security/idempotency/watchdog); `docs/EXECUTION.md` (milestone spine M0–M8, the three-gate Decision Register, the M5-FINAL-FREEZE package-boundary clause, the material-change definition, the build-order diagram).
- **Corroborating (MEDIUM/LOW confidence, general software-architecture patterns, web-sourced, used only to justify implementation shape — not to add or override requirements):**
  - Event-driven backtest engine component separation (DataHandler/Strategy/Portfolio/ExecutionHandler over a central event queue) — QuantStart's "Event-Driven Backtesting with Python" series and comparable write-ups on Backtrader/Zipline/NautilusTrader-style architectures.
  - Purged/embargoed/nested walk-forward cross-validation for financial ML (López de Prado's purged CV; combinatorial purged CV for PBO) — general ML-in-finance literature and open-source implementations (e.g. `purged-cross-validation`).
  - Reproducible pipeline practice (hashed/pinned artifacts, config+dataset+commit manifests for exact replay) — general MLOps/reproducibility literature.
  - Production exchange-bot patterns (order-state-machine OMS, idempotent client-order-id + check-before-retry, periodic reconciliation job, out-of-process watchdog) — general crypto trading-infrastructure write-ups.

---
*Architecture research for: honest quant backtest + validation system, Bithumb KRW spot, single-asset long-or-cash*
*Researched: 2026-09-07*
