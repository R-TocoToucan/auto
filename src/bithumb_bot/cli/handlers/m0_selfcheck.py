"""``bt m0 selfcheck`` handler (D-86 / SAFE-02 surface per D-99).

Prints three sections to stdout:

1. **Resolved Gate-1 decisions** — short-form summary loaded via
   :func:`bithumb_bot.config.gate_loader.load_gate1` (same loader
   ``bt config validate`` uses).
2. **Resolved key class** — computed from a freshly-constructed
   :class:`bithumb_bot.secrets.settings.BithumbSecrets` per D-89
   (ephemeral: constructed, inspected, dropped inside this handler):

   * Both account-read env vars set → ``account_read``.
   * Either present but not both → ``account_read (incomplete — missing
     <field>)``.
   * Neither set → ``no credential loaded (public path only)``.
   * Trade cred present (either field) → **refused via
     :func:`bithumb_bot.secrets.loader.reject_trade_credentials`
     (D-70 class-only reporting)**. The exception is caught and
     translated to a clean stderr message + non-zero exit; the
     credential VALUE is never touched, printed, or interpolated.

3. **Risk-denominator vocabulary** — the five terms
   (``planned_stop_loss``, ``max_market_loss``, ``max_operational_loss``,
   ``position_fraction``, ``risk_per_trade``) with brief one-line
   descriptions — sourced from :mod:`bithumb_bot.core.money`.

Defense-in-depth (D-85) — FIRST STATEMENT calls ``validate()`` so
direct Python callers cannot bypass CLI validation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bithumb_bot.cli.handlers import config_validate as _config_validate_handler
from bithumb_bot.config.gate_loader import load_gate1
from bithumb_bot.config.validator import validate
from bithumb_bot.errors import ProhibitedCredentialDetectedError
from bithumb_bot.secrets.loader import load_secrets, reject_trade_credentials


REPO_ROOT: Path = Path.cwd()


# The five SAFE-07 risk denominators — text sourced from
# `bithumb_bot.core.money.__doc__` per plan Behavior spec. Kept as a
# module-level tuple so tests can assert against the exact set without
# regex-matching the printed output.
_RISK_DENOMS: tuple[tuple[str, str], ...] = (
    (
        "planned_stop_loss",
        "Contract-level planned loss if the stop triggers as intended "
        "(entry price minus stop price times quantity).",
    ),
    (
        "max_market_loss",
        "Worst-case market-side loss at trigger — gap risk, book-side "
        "depletion, slippage past the stop. Bounded above by the sleeve, "
        "not by the stop formula.",
    ),
    (
        "max_operational_loss",
        "Worst-case operator-side loss including venue/broker/execution "
        "failure modes (rate-limit block, WebSocket outage, reconcile "
        "mismatch). Bounded above by the sleeve.",
    ),
    (
        "position_fraction",
        "Fraction of the isolated sleeve currently allocated to the open "
        "position. In [0, 1]. Enforced at sizing time.",
    ),
    (
        "risk_per_trade",
        "Fraction of the sleeve the trade is authorized to lose "
        "(planned_stop_loss expressed as a sleeve fraction).",
    ),
)


def _format_key_class_report() -> str:
    """Return a one-line description of the resolved credential class.

    Constructs a fresh ``BithumbSecrets`` per D-89 (ephemeral); never
    unwraps a ``SecretStr``. Only inspects presence via ``is not None``.
    Raises ``ProhibitedCredentialDetectedError`` (via
    :func:`reject_trade_credentials`) if a trade cred is present — the
    caller (:func:`handler`) catches and translates.
    """
    settings = load_secrets(REPO_ROOT)
    # First: defense-in-depth trade-cred rejection (D-70 class-only).
    reject_trade_credentials(settings)

    access = settings.account_read_access_key
    secret = settings.account_read_secret_key
    if access is not None and secret is not None:
        return "key class: account_read"
    if access is not None and secret is None:
        return "key class: account_read (incomplete — missing account_read_secret_key)"
    if secret is not None and access is None:
        return "key class: account_read (incomplete — missing account_read_access_key)"
    return "key class: no credential loaded (public path only)"


def _format_gate1_section(gate1_path: Path) -> str:
    """Short-form Gate-1 summary — the one `bt m0 selfcheck` prints."""
    gate1, _sha256 = load_gate1(gate1_path)
    return (
        "gate: gate1\n"
        f"status: {gate1.status}\n"
        f"schema_version: {gate1.schema_version}\n"
        f"source_commit: {gate1.source_commit[:12]}\n"
        f"provisional_engineering_notional_krw: "
        f"{gate1.provisional_engineering_notional_krw}"
    )


def _format_risk_denominator_section() -> str:
    lines = ["risk-denominator vocabulary (SAFE-07):"]
    for name, description in _RISK_DENOMS:
        lines.append(f"  {name}: {description}")
    return "\n".join(lines)


def handler(args: argparse.Namespace) -> int:
    """`bt m0 selfcheck` handler entry point (defense-in-depth per D-85)."""
    _result = validate(("m0", "selfcheck"))
    if not _result.ok:
        print(
            f"bt m0 selfcheck: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    # Section 1 — Gate-1 short summary.
    gate1_path = REPO_ROOT / "config" / "decisions" / "gate1.toml"
    try:
        gate1_section = _format_gate1_section(gate1_path)
    except Exception as exc:  # noqa: BLE001 — surface a clean refusal
        print(
            f"bt m0 selfcheck: failed to load Gate-1: {exc}",
            file=sys.stderr,
        )
        return 1

    # Section 2 — Key class (may refuse if a trade cred is present).
    try:
        key_class_section = _format_key_class_report()
    except ProhibitedCredentialDetectedError as exc:
        # D-70 class-only reporting — the exception message already
        # contains only the credential CLASS name, never the value.
        print(
            f"bt m0 selfcheck: refused — {exc}",
            file=sys.stderr,
        )
        return 1

    # Section 3 — Risk denominator vocabulary.
    risk_section = _format_risk_denominator_section()

    print(gate1_section)
    print()
    print(key_class_section)
    print()
    print(risk_section)
    return 0


# Re-export a stable REPO_ROOT reference used by the sibling
# config_validate handler for symmetry; the sibling module owns
# the canonical value used by that handler.
_ = _config_validate_handler  # noqa: F841 — keep the sibling import live


__all__ = ["REPO_ROOT", "handler"]
