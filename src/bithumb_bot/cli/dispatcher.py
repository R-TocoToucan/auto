"""`bt` CLI dispatcher — argparse two-level subparser + validate-before-dispatch.

Dispatch sequence (D-85 / D-89):

  1. Argparse ``--help`` / ``--version`` / empty argv are handled natively
     BEFORE any capability lookup, credential load, or ``BithumbSecrets``
     construction occurs (T-1-03-04). The argparse `SystemExit` (0 for
     help/version, 2 for a usage error) propagates unchanged.
  2. Parse argv → ``(verb, subverb, remaining_args)`` via the argparse
     tree built in :func:`_build_parser`. Unknown verb / subverb → argparse
     writes ``unknown command`` (or ``invalid choice: ...``) to stderr and
     exits ``2`` (D-89 fail-hard).
  3. Look up ``HANDLER_MAP[(verb, subverb)]``. Absent → treated as an
     internal wiring bug (a subparser is registered without a handler);
     print refusal, exit ``1``.
  4. Call ``validate((verb, subverb))``. If ``not result.ok``: print
     ``refusal: <result.reason>`` to stderr, exit ``1``.
  5. Bind structlog contextvars (``capability``, ``command``,
     ``invocation_id``) — guarded by ``try/except ImportError`` until
     plan 01-04 makes structlog live. Failure to bind is NON-FATAL
     (T-1-03-06 tolerates a missing observability layer during Phase 1).
  6. Dispatch to the registered handler with the parsed args namespace.

The dispatcher NEVER catches unknown exceptions — they propagate and
Python emits a traceback with exit code 1. It DOES catch the specific
named exceptions the SAFE-01/SAFE-02/SAFE-03 stack raises for documented
refusals (``UnknownCapabilityError``, ``Gate1LoadError``,
``ProhibitedCredentialDetectedError``, ``SecretsFileInsideRepoError``,
``AmbiguousSecretsConfigurationError``) and translates each into a
clean stderr message + exit ``1``, so an operator sees a documented
refusal, not a Python traceback.

**Do not** import ``bithumb_bot.broker`` or ``bithumb_bot.bithumb_spec.client``
at module top level — the client is imported lazily inside the ``m1
fetch-spec`` handler (plan 01-04). This preserves the "help/version
does not load credentials" property AND avoids pulling httpx / structlog
into the argparse-only code path.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import uuid
from typing import Callable

from bithumb_bot.config.validator import validate
from bithumb_bot.errors import (
    AmbiguousSecretsConfigurationError,
    BithumbBotError,
    Gate1LoadError,
    ProhibitedCredentialDetectedError,
    SecretsFileInsideRepoError,
    UnknownCapabilityError,
)


# Package version — pyproject.toml owns the canonical value. Kept as a
# module-level string so argparse's `--version` action has a value to print
# without touching packaging metadata at parse time (which is slow enough
# to violate the "help/version load nothing" invariant).
_CLI_VERSION = "bithumb_bot 0.1.0"

# Type alias for handler callables — every handler accepts the parsed
# argparse Namespace and returns an integer exit code.
HandlerFn = Callable[[argparse.Namespace], int]


def _resolve_handler(module_name: str, function_name: str) -> HandlerFn:
    """Lazy handler resolver — importlib-based so the dispatcher lands
    before the individual handler modules exist (they arrive in
    01-03-06 / 01-03-07 / 01-03-08 / 01-03-09).
    """
    module = importlib.import_module(module_name)
    return getattr(module, function_name)  # type: ignore[no-any-return]


def _make_lazy_handler(module_name: str, function_name: str) -> HandlerFn:
    """Return a callable that resolves + invokes the real handler on demand.

    Enables ``mock.patch.dict(HANDLER_MAP, {...})`` in tests to bypass
    the missing handler module entirely, so the dispatcher can be tested
    in isolation from the yet-to-land handler implementations.
    """

    def _lazy(args: argparse.Namespace) -> int:
        fn = _resolve_handler(module_name, function_name)
        return fn(args)

    _lazy.__name__ = f"lazy_{module_name.rsplit('.', 1)[-1]}_{function_name}"
    return _lazy


# ---------------------------------------------------------------------------
# HANDLER_MAP — one entry per registered CLI capability
#
# Phase-1 (D-86) verbs land now via lazy handlers so the dispatcher does
# not force the handler modules to exist at import time. Reserved-verb
# entries (D-87) are added by plan 01-03-08's `extend_with_reserved()`.
# ---------------------------------------------------------------------------
HANDLER_MAP: dict[tuple[str, str], HandlerFn] = {
    ("config", "validate"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.config_validate", "handler"
    ),
    ("m0", "selfcheck"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.m0_selfcheck", "handler"
    ),
    # ---- Plan 01-04 replaces the m1_stubs bindings with the real
    # handlers below (D-90 phase discipline). The `m1_stubs` module
    # stays as defense-in-depth for direct Python callers who bypass
    # the dispatcher entirely.
    ("m1", "fetch-spec"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.m1_fetch_spec", "handler"
    ),
    ("m1", "verify-facts"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.m1_verify_facts", "handler"
    ),
    ("m1", "verify-snapshot"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.m1_verify_snapshot", "handler"
    ),
    ("research", "collect-candles"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.research_collect_candles", "handler"
    ),
    ("research", "backtest"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.research_backtest", "handler"
    ),
    # ---- D-87 reserved verbs — every entry bound to reserved_handler ------
    ("m2", "collect-observations"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("m2", "calibrate-costs"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("m2", "replay-known-answer"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("m4", "evaluate-selection"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("m5", "evaluate-module"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("freeze", "strategy"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("m6a", "verify-mock-broker"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("freeze", "final"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
    ("holdout", "evaluate"): _make_lazy_handler(
        "bithumb_bot.cli.handlers.reserved", "reserved_handler"
    ),
}


# ---------------------------------------------------------------------------
# Argparse tree
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the two-level subparser tree for Phase-1 verbs (D-86).

    Reserved-verb subparsers are added by plan 01-03-08's
    ``extend_parser_with_reserved(parser)`` — this function returns the
    Phase-1-only tree and leaves the ``verbs`` subparsers object attached
    as ``parser._bt_verbs`` so 01-03-08 can extend it.
    """
    parser = argparse.ArgumentParser(
        prog="bt",
        description=(
            "Bithumb Autotrading Bot CLI — Phase 1 verbs (D-86). "
            "Reserved future verbs (D-87) refuse uniformly."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_CLI_VERSION,
    )

    verbs = parser.add_subparsers(
        dest="verb",
        metavar="<verb>",
        title="verbs",
        description="Phase-1 verbs (config, m0, m1)",
    )

    # -- config verb --------------------------------------------------------
    p_config = verbs.add_parser(
        "config",
        help="Validate a candidate gate file (D-89 — inspect, never mark approved).",
    )
    p_config_sub = p_config.add_subparsers(
        dest="subverb", metavar="<subverb>", title="config verbs"
    )
    p_config_validate = p_config_sub.add_parser(
        "validate",
        help="Validate config/decisions/gate1.toml against Gate1Decisions.",
    )
    p_config_validate.add_argument(
        "--through",
        choices=["gate1"],
        required=True,
        help="Which gate to validate through (Phase 1: gate1 only).",
    )

    # -- m0 verb ------------------------------------------------------------
    p_m0 = verbs.add_parser(
        "m0",
        help="M0 pre-flight utilities (offline, no network, no creds needed).",
    )
    p_m0_sub = p_m0.add_subparsers(
        dest="subverb", metavar="<subverb>", title="m0 verbs"
    )
    p_m0_sub.add_parser(
        "selfcheck",
        help=(
            "Print resolved Gate-1 decisions, key class, and risk-denominator "
            "vocabulary — refuses if a trade credential class is present."
        ),
    )

    # -- m1 verb ------------------------------------------------------------
    p_m1 = verbs.add_parser(
        "m1",
        help=(
            "M1 Bithumb spec adapter (authenticated read-only; account/read "
            "credential only, per D-97). Handlers land in plan 01-04."
        ),
    )
    p_m1_sub = p_m1.add_subparsers(
        dest="subverb", metavar="<subverb>", title="m1 verbs"
    )
    p_m1_fetch = p_m1_sub.add_parser(
        "fetch-spec",
        help="Fetch and pin the fee/tick/min-order snapshot (plan 01-04).",
    )
    p_m1_fetch.add_argument(
        "--market",
        required=False,
        default="KRW-BTC",
        help="Market symbol (default: KRW-BTC).",
    )
    p_m1_facts = p_m1_sub.add_parser(
        "verify-facts",
        help="Verify a snapshot's facts against the recorded sidecar (plan 01-04).",
    )
    p_m1_facts.add_argument(
        "--bundle",
        required=False,
        help="Path to the snapshot bundle to verify (plan 01-04).",
    )
    p_m1_snap = p_m1_sub.add_parser(
        "verify-snapshot",
        help="Verify a candidate snapshot's hash and structure (plan 01-04).",
    )
    p_m1_snap.add_argument(
        "--snapshot",
        required=False,
        help="Path to the candidate snapshot file (plan 01-04).",
    )
    p_m1_snap.add_argument(
        "--require-execution-ready",
        action="store_true",
        default=False,
        help=(
            "Fail non-zero when execution readiness is unresolved. Default "
            "invocation performs the artifact-integrity check and reports "
            "readiness diagnostically (exits 0 even when unresolved)."
        ),
    )

    # -- research verb -----------------------------------------------------
    p_research = verbs.add_parser(
        "research",
        help=(
            "Research MVP surface — public candle collection + offline "
            "backtest + evaluation. No credentials, no live orders."
        ),
    )
    p_research_sub = p_research.add_subparsers(
        dest="subverb", metavar="<subverb>", title="research verbs"
    )
    p_research_collect = p_research_sub.add_parser(
        "collect-candles",
        help=(
            "Fetch public candles via Bithumb REST and write a "
            "canonical CandleDataset + SHA-256 sidecar."
        ),
    )
    p_research_collect.add_argument("--market", required=True)
    p_research_collect.add_argument(
        "--unit-minutes",
        required=True,
        type=int,
        help="Candle unit in minutes (240 for the v1 strategy).",
    )
    p_research_collect.add_argument(
        "--start-utc",
        required=True,
        help="ISO-8601 UTC start (inclusive, on a unit-minute boundary).",
    )
    p_research_collect.add_argument(
        "--end-utc",
        required=True,
        help="ISO-8601 UTC end (exclusive, on a unit-minute boundary).",
    )
    p_research_collect.add_argument(
        "--out",
        required=True,
        help="Output path for the CandleDataset JSON (sidecar written next to it).",
    )
    p_research_backtest = p_research_sub.add_parser(
        "backtest",
        help=(
            "Run the chronological backtest + performance evaluation "
            "and persist a canonical JSON report + SHA-256 sidecar."
        ),
    )
    p_research_backtest.add_argument("--dataset", required=True)
    p_research_backtest.add_argument("--snapshot", required=True)
    p_research_backtest.add_argument("--config", required=True)
    p_research_backtest.add_argument("--out", required=True)

    # -- D-87 reserved verbs (added by plan 01-03-08) ---------------------
    # Every reserved subparser is labelled `RESERVED — future phase, not
    # implemented` in its help text (per plan Behavior). All bind to the
    # shared reserved_handler via HANDLER_MAP; the dispatcher runs
    # validate() first and, only if it passes, invokes the reserved
    # handler — which then explicitly refuses. This matches D-90's rule
    # that reserved handlers must not be scaffolded as functional.
    _add_reserved_subparsers(verbs)

    # Attach the top-level verbs subparser so 01-03-08 can extend it.
    parser._bt_verbs = verbs  # type: ignore[attr-defined]
    return parser


# Sub-verbs per top-level reserved verb (D-87 registry).
_RESERVED_TREE: dict[str, tuple[str, ...]] = {
    "m2": ("collect-observations", "calibrate-costs", "replay-known-answer"),
    "m4": ("evaluate-selection",),
    "m5": ("evaluate-module",),
    "freeze": ("strategy", "final"),
    "m6a": ("verify-mock-broker",),
    "holdout": ("evaluate",),
}


def _add_reserved_subparsers(verbs: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register argparse subparsers for every D-87 reserved verb.

    Each subverb's help text is labelled ``RESERVED — future phase, not
    implemented`` so ``bt <verb> <subverb> --help`` makes the reserved
    status obvious. The parser never binds ``func`` — the dispatcher's
    HANDLER_MAP owns the reserved-handler wiring.
    """
    for top_verb, subverbs in _RESERVED_TREE.items():
        parser = verbs.add_parser(
            top_verb,
            help=(
                f"RESERVED — future phase, not implemented (D-87). "
                f"Every '{top_verb}' subverb refuses uniformly."
            ),
        )
        subs = parser.add_subparsers(
            dest="subverb",
            metavar="<subverb>",
            title=f"{top_verb} verbs",
        )
        for subverb in subverbs:
            subs.add_parser(
                subverb,
                help=f"RESERVED — future phase, not implemented (D-87 / D-90).",
                description=(
                    f"RESERVED — future phase, not implemented. "
                    f"'bt {top_verb} {subverb}' is registered in the "
                    "capability registry per D-87 so its prerequisites "
                    "exist as data, but its handler is not scaffolded as "
                    "functional (D-90 phase discipline). Refuses uniformly "
                    "via reserved_handler."
                ),
            )


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------


def _write_stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def _bind_contextvars(capability: tuple[str, str], command: str) -> None:
    """Bind structlog context vars, guarded per T-1-03-06.

    Structlog goes live in plan 01-04; until then this is a no-op if the
    module is missing or misconfigured. NEVER let a logging problem block
    a documented refusal or a valid dispatch.
    """
    try:
        import structlog

        structlog.contextvars.bind_contextvars(
            capability=f"{capability[0]}.{capability[1]}",
            command=command,
            invocation_id=str(uuid.uuid4()),
        )
    except Exception:  # noqa: BLE001 — TODO(plan-01-04): promote to structured logging
        # Silent no-op — structlog is not yet wired; this bind is
        # best-effort observability, not a correctness requirement.
        pass


_TRANSLATED_EXCEPTIONS: tuple[type[BithumbBotError], ...] = (
    UnknownCapabilityError,
    Gate1LoadError,
    ProhibitedCredentialDetectedError,
    SecretsFileInsideRepoError,
    AmbiguousSecretsConfigurationError,
)


def dispatch(argv: list[str]) -> int:
    """Parse `argv` and dispatch to the registered handler.

    Returns the handler's integer exit code. Argparse's own
    ``SystemExit`` (help/version → 0; usage error → 2) is allowed to
    propagate — the caller (`main`) converts it to a return code.
    """
    parser = _build_parser()
    # Argparse raises SystemExit on --help, --version, and usage errors.
    # We let SystemExit propagate; `main()` converts it to a return code.
    args = parser.parse_args(argv)

    verb = getattr(args, "verb", None)
    subverb = getattr(args, "subverb", None)
    if verb is None or subverb is None:
        # No verb or no subverb — argparse chose not to exit itself (Python
        # 3.11+ subparsers with `required=False` default). Refuse hard.
        parser.print_usage(sys.stderr)
        _write_stderr("bt: unknown command (no verb / subverb provided)")
        return 2

    capability = (verb, subverb)
    handler = HANDLER_MAP.get(capability)
    if handler is None:
        _write_stderr(
            f"bt: unknown command {' '.join(capability)!r} — no handler "
            f"registered for this capability. This is an internal wiring "
            f"bug; every argparse-registered subparser MUST have a "
            f"HANDLER_MAP entry."
        )
        return 1

    try:
        result = validate(capability)
    except _TRANSLATED_EXCEPTIONS as exc:
        _write_stderr(f"bt: refusal — {type(exc).__name__}: {exc}")
        return 1

    if not result.ok:
        _write_stderr(
            f"bt: refusal: {result.reason} "
            f"(missing: {', '.join(result.missing) or 'unspecified'})"
        )
        return 1

    _bind_contextvars(capability, command=" ".join(argv))

    try:
        return int(handler(args))
    except _TRANSLATED_EXCEPTIONS as exc:
        _write_stderr(f"bt: refusal — {type(exc).__name__}: {exc}")
        return 1


__all__ = [
    "HANDLER_MAP",
    "HandlerFn",
    "dispatch",
]
