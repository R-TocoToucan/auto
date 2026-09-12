"""Restart-safe persistence for a paper-trading session.

``state.json`` is the single-file ledger + cursor snapshot; it is
overwritten (atomic replace) on every successful invocation.
``fills.jsonl`` / ``signals.jsonl`` are append-only audit logs — a new
invocation only ever appends new lines, never rewrites an existing
one (see :mod:`bithumb_bot.paper.runner`'s fill-replay divergence
check).

Every Decimal-bearing field on :class:`PaperState` is stored as a
string (D-49) — ``hysteresis_bps``, ``final_cash_krw``, and
``final_position_qty`` round-trip exactly through JSON with no
``float`` in between.

``schema_version`` is pinned at :data:`_CURRENT_SCHEMA_VERSION` (``2``).
A ``state.json`` written by the pre-D-erv binary (``schema_version ==
1``) lacks the ``paper_start_*`` invariant fields introduced by the
warmup-isolation fix and is NOT resume-compatible: :func:`load_state`
refuses it with :class:`~bithumb_bot.errors.ForwardDatasetDivergenceError`
rather than silently upgrading it or letting pydantic's ``extra="forbid"``
raise an opaque validation error.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from bithumb_bot.artifact.canonical import canonical_bytes, sha256_hex, write_with_sidecar
from bithumb_bot.errors import ForwardDatasetDivergenceError, SidecarHashMismatchError

#: Length of a hex-encoded SHA-256 digest — used to validate a sidecar's
#: recorded hash column before trusting it as "the" digest.
_SHA256_HEX_LEN = 64

#: The only ``schema_version`` :func:`load_state` accepts. Bumped from
#: ``1`` to ``2`` by the D-erv warmup-isolation fix, which added the
#: four ``paper_start_*`` invariant fields below.
_CURRENT_SCHEMA_VERSION = 2


def _require_decimal_string(value: str) -> str:
    """Fail closed unless ``value`` parses as a :class:`Decimal` (D-49)."""
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} does not parse as a Decimal") from exc
    return value


class PaperState(BaseModel):
    """Frozen, byte-exact ``state.json`` shape (see PLAN's persistence contract).

    ``schema_version`` is pinned at ``1`` for this plan. Every field
    is required (no defaults) so a hand-edited or partially-written
    file fails structural validation rather than silently filling in
    a default that could hide corruption.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: int
    run_purpose: str
    selection_eligible: bool
    holdout_eligible: bool
    hysteresis_bps: str
    paper_start_ts_utc: str
    #: Literal invariants (D-erv): the paper portfolio at
    #: ``paper_start_ts_utc`` is ALWAYS unambiguously fresh — cash
    #: equals ``config.starting_cash_krw``, position/realized-P&L/fees
    #: are all zero — because the fresh-portfolio forward loop never
    #: carries a warmup-sourced fill across the boundary (see
    #: ``paper/runner.py``'s module docstring). Present so a mutation of
    #: the runner that accidentally reintroduces the warmup-leak defect
    #: has a machine-checkable contract to violate.
    paper_start_cash_krw: str
    paper_start_position_qty: str
    paper_start_realized_pnl_krw: str
    paper_start_cumulative_fees_krw: str
    warmup_first_open_utc: str
    warmup_last_open_utc: str
    warmup_candles: int
    warmup_sha256: str
    config_sha256: str
    snapshot_sha256: str
    dataset_market: str
    unit_minutes: int
    last_processed_open_utc: str | None
    forward_candle_count: int
    forward_signal_count: int
    forward_fill_count: int
    final_cash_krw: str
    final_position_qty: str

    @field_validator(
        "hysteresis_bps",
        "final_cash_krw",
        "final_position_qty",
        "paper_start_cash_krw",
        "paper_start_position_qty",
        "paper_start_realized_pnl_krw",
        "paper_start_cumulative_fees_krw",
    )
    @classmethod
    def _validate_decimal_string(cls, value: str) -> str:
        return _require_decimal_string(value)


def load_state(state_dir: Path) -> PaperState | None:
    """Load ``state.json`` from ``state_dir``, or ``None`` if absent.

    Raises:
        SidecarHashMismatchError: the sidecar is missing or the
            on-disk bytes no longer match the recorded hash — the
            same tamper/corruption detection the M1 snapshot uses.
        ForwardDatasetDivergenceError: the on-disk ``schema_version``
            predates the D-erv warmup-isolation fix (see module
            docstring) — the file is refused BEFORE
            ``PaperState.model_validate`` ever sees it, so the operator
            gets a clear message instead of an opaque pydantic
            ``extra="forbid"`` / missing-required-field error.
    """
    path = state_dir / "state.json"
    if not path.is_file():
        return None
    data = path.read_bytes()
    on_disk_hex = sha256_hex(data)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise SidecarHashMismatchError(path)
    recorded = sidecar.read_text(encoding="utf-8").strip().split()
    if (
        len(recorded) < 1
        or len(recorded[0]) != _SHA256_HEX_LEN
        or recorded[0] != on_disk_hex
    ):
        raise SidecarHashMismatchError(path)
    parsed: Any = json.loads(data.decode("utf-8"))
    on_disk_schema = parsed.get("schema_version") if isinstance(parsed, dict) else None
    if on_disk_schema != _CURRENT_SCHEMA_VERSION:
        raise ForwardDatasetDivergenceError(
            f"state.json schema_version={on_disk_schema!r} predates the "
            "D-erv warmup-isolation fix — delete state.json to start a "
            "fresh session"
        )
    return PaperState.model_validate(parsed)


def save_state(state_dir: Path, state: PaperState) -> None:
    """Atomically replace ``state.json`` + its ``.sha256`` sidecar."""
    target = state_dir / "state.json"
    write_with_sidecar(target, canonical_bytes(state.model_dump(mode="json")))


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append one canonical-JSON line to ``path`` (create parents as needed).

    ``canonical_bytes`` already terminates with a single trailing
    ``\\n``, so one call writes exactly one JSONL line. The write is
    flushed and fsync'd so a crash immediately after this call cannot
    lose the line silently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_bytes(obj)
    with path.open("ab") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read every JSON object in ``path``, one per line. ``[]`` if absent.

    Raises:
        ValueError: a non-empty line fails to parse as JSON — a
            malformed audit-trail line is a corruption signal, not a
            state to silently skip past.
    """
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSONL line in {path!s}: {line!r}") from exc
    return out


__all__ = [
    "PaperState",
    "append_jsonl",
    "load_state",
    "read_jsonl",
    "save_state",
]
