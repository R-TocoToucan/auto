"""Pure ``execute_intent`` — the marketable-order fill + accounting core.

The engine is a single function. Every path is deterministic Decimal
arithmetic; there is no wall-clock read, no randomness, no I/O. Same
inputs → byte-identical output.

Fail-closed contract (in evaluation order):

1. Fee-model verification status (per side, from the snapshot) must
   be ``confirmed_read_only``, or ``provisional_documented`` with the
   caller's explicit ``ExecutionConfig.allow_provisional_fee_model = True``.
   Anything else — including a missing status key — raises
   :class:`~bithumb_bot.errors.UnverifiedFeeModelError`.
2. Snapshot's ``default_tick`` and ``default_step`` MUST exist. Missing
   either raises :class:`~bithumb_bot.errors.SnapshotValidationError`
   — the engine never assumes a tick or step (correction #3).
3. The fill candle is the earliest ``Candle`` in the dataset whose
   ``open_time_utc >= intent.signal_ts_utc``. None found →
   :class:`~bithumb_bot.errors.NoNextCandleError`. If candles are
   missing in the interval, the first genuinely available later
   candle is used — no fabricated or forward-filled prices.
4. Structural no-same-candle assert: ``fill_candle.open_time_utc !=
   intent.source_open_time_utc``. A violation is an internal invariant
   failure, not a normal fail-closed path.
5. Buy: qty-step floor of ``requested_notional / fill_price``;
   min-order check on ``order_notional_krw``; notional-cap check on
   ``order_notional_krw``; cash check on ``total_cash_debit_krw``.
6. Sell: qty-step floor of ``requested_qty``; min-order (coin qty)
   check on ``filled_qty``; notional-cap check on
   ``gross_proceeds_krw``; position check on ``filled_qty``.
"""

from __future__ import annotations

from decimal import Decimal

from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.bithumb_spec.tick_schedule import resolve_krw_tick
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.core.rounding import (
    quantize_price_tick_down,
    quantize_price_tick_up,
    quantize_volume_down,
)
from bithumb_bot.errors import (
    BelowMinimumOrderError,
    InsufficientCashError,
    InsufficientPositionError,
    NoNextCandleError,
    NotionalCapExceededError,
    SnapshotValidationError,
    UnverifiedFeeModelError,
)
from datetime import datetime

from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.execution.intent import OrderIntent, Side
from bithumb_bot.execution.ledger import (
    ExecutionReason,
    LedgerEntry,
    LedgerState,
    TimingSemantics,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset

_TICK_KEY = "default_tick"
_STEP_KEY = "default_step"
_BPS_DENOMINATOR = Decimal("10000")
_ZERO_MONEY = Money(Decimal("0"))
_ZERO_QTY = Qty(Decimal("0"))


def execute_intent(
    state: LedgerState,
    intent: OrderIntent,
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: ExecutionConfig,
) -> tuple[LedgerState, LedgerEntry]:
    """Execute one ``intent`` against ``state``, producing a new state + entry."""
    _check_fee_verification(intent.side, snapshot, config)
    _ensure_tick_available(snapshot)
    step = _resolve_step(snapshot, config)

    fill_candle = _find_fill_candle(dataset, intent.signal_ts_utc)
    if fill_candle is None:
        raise NoNextCandleError(
            f"no candle in dataset with open_time_utc >= "
            f"{intent.signal_ts_utc.isoformat()} — cannot fill without "
            "fabricating a price"
        )
    # Structural no-same-candle rule (correction #1). This must be true by
    # construction because signal_ts = source_open + unit > source_open; an
    # equality here means the dataset is malformed or the intent was
    # constructed with unit_minutes <= 0. Hard assert, not a fail-closed
    # path — a violation is a bug in the caller/data, not an operating
    # condition the engine can recover from.
    assert fill_candle.open_time_utc != intent.source_open_time_utc, (
        "engine invariant violated: fill_candle.open_time_utc "
        f"({fill_candle.open_time_utc.isoformat()}) == "
        f"intent.source_open_time_utc "
        f"({intent.source_open_time_utc.isoformat()})"
    )

    fill_price_d = _slippage_and_tick(
        base=fill_candle.open.value,
        side=intent.side,
        slippage_bps=config.slippage_bps_per_side,
        tick=_resolve_tick_for_price(snapshot, fill_candle.open.value),
    )

    if intent.side == "buy":
        entry = _execute_buy(
            intent=intent,
            state=state,
            fill_candle=fill_candle,
            fill_price_d=fill_price_d,
            step=step,
            snapshot=snapshot,
            config=config,
        )
    else:
        entry = _execute_sell(
            intent=intent,
            state=state,
            fill_candle=fill_candle,
            fill_price_d=fill_price_d,
            step=step,
            snapshot=snapshot,
            config=config,
        )
    return state.apply(entry), entry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_fee_verification(
    side: Side, snapshot: SnapshotV1, config: ExecutionConfig
) -> None:
    key = "market_buy_fee_reservation" if side == "buy" else "general_fee_rate"
    status = snapshot.verification_status.get(key)
    if status == "confirmed_read_only":
        return
    if status == "provisional_documented":
        if not config.allow_provisional_fee_model:
            raise UnverifiedFeeModelError(
                f"snapshot.verification_status[{key!r}] == "
                f"'provisional_documented' for side={side!r}. Strategy "
                "evaluation must fail closed; set "
                "ExecutionConfig(allow_provisional_fee_model=True) only "
                "for engine tests with an explicit conservative fixture."
            )
        return
    raise UnverifiedFeeModelError(
        f"snapshot.verification_status[{key!r}] == {status!r} for "
        f"side={side!r} — engine refuses. Only 'confirmed_read_only' "
        "or 'provisional_documented' (with explicit opt-in) are usable."
    )


def _require_tick(snapshot: SnapshotV1) -> Decimal:
    """Legacy default-tick lookup, retained for callers that pre-date
    the Batch 1B official schedule (e.g. hand-built engine-test snapshots).

    New code paths call :func:`_resolve_tick_for_price`, which prefers
    the official price-band schedule stored via
    ``snapshot.price_tick_schedule_provenance`` and falls back to the
    single ``default_tick`` when the snapshot pre-dates the schedule.
    """
    tick = snapshot.price_tick_rules.get(_TICK_KEY)
    if tick is None:
        raise SnapshotValidationError(
            f"snapshot.price_tick_rules missing required key {_TICK_KEY!r} "
            "— no verified tick rule for this market; engine refuses."
        )
    return tick


def _resolve_tick_for_price(snapshot: SnapshotV1, price: Decimal) -> Decimal:
    """Return the KRW price tick for ``price``.

    Prefers the official schedule when the snapshot carries schedule
    provenance (Batch 1B). Falls back to the legacy ``default_tick``
    key so pre-schedule snapshots (and hand-built engine-test snapshots)
    still work.
    """
    if snapshot.price_tick_schedule_provenance is not None:
        return resolve_krw_tick(price)
    return _require_tick(snapshot)


def _ensure_tick_available(snapshot: SnapshotV1) -> None:
    """Fail-closed eager check: reject a snapshot with no tick source.

    A snapshot missing BOTH ``price_tick_schedule_provenance`` and a
    legacy ``price_tick_rules['default_tick']`` cannot produce a tick
    for any price. Callers run this up-front so the refusal happens
    before any fill attempt, matching the pre-Batch-1B contract.
    """
    if snapshot.price_tick_schedule_provenance is not None:
        return
    if _TICK_KEY in snapshot.price_tick_rules:
        return
    raise SnapshotValidationError(
        f"snapshot has no price-tick source (neither price_tick_schedule_"
        f"provenance nor price_tick_rules[{_TICK_KEY!r}]) — engine refuses."
    )


def _require_step(snapshot: SnapshotV1) -> Decimal:
    step = snapshot.quantity_step_rules.get(_STEP_KEY)
    if step is None:
        raise SnapshotValidationError(
            f"snapshot.quantity_step_rules missing required key {_STEP_KEY!r} "
            "— no verified quantity-step rule for this market; engine refuses."
        )
    return step


def _resolve_step(snapshot: SnapshotV1, config: ExecutionConfig) -> Decimal:
    """Return the base-asset quantity step the engine should use.

    Research simulation (Batch 1B): when the caller supplies
    ``config.simulation_quantity_quantum``, the engine floors quantities
    to that quantum — a research assumption, NOT a claim about
    Bithumb's live-accepted quantity step.

    Live path: no quantum → require ``snapshot.default_step``. Bithumb's
    live order-volume precision remains unresolved until M6B; a snapshot
    without ``default_step`` and no research quantum is refused.
    """
    if config.simulation_quantity_quantum is not None:
        return config.simulation_quantity_quantum
    return _require_step(snapshot)


def _find_fill_candle(
    dataset: CandleDataset, signal_ts_utc: datetime
) -> Candle | None:
    for candle in dataset.candles:
        if candle.open_time_utc >= signal_ts_utc:
            return candle
    return None


def _slippage_and_tick(
    *, base: Decimal, side: Side, slippage_bps: Decimal, tick: Decimal
) -> Decimal:
    """Apply per-side slippage once, then adverse tick-snap once."""
    factor = slippage_bps / _BPS_DENOMINATOR
    if side == "buy":
        adjusted = base * (Decimal("1") + factor)
        return quantize_price_tick_up(adjusted, tick)
    # sell
    adjusted = base * (Decimal("1") - factor)
    return quantize_price_tick_down(adjusted, tick)


# ---------------------------------------------------------------------------
# buy / sell paths
# ---------------------------------------------------------------------------


def _execute_buy(
    *,
    intent: OrderIntent,
    state: LedgerState,
    fill_candle: Candle,
    fill_price_d: Decimal,
    step: Decimal,
    snapshot: SnapshotV1,
    config: ExecutionConfig,
) -> LedgerEntry:
    assert intent.requested_notional_krw is not None  # narrowed by OrderIntent
    requested_d = intent.requested_notional_krw.value

    # Desired coin qty from pre-fee KRW request; floor to step.
    desired_qty_d = requested_d / fill_price_d
    filled_qty_d = quantize_volume_down(desired_qty_d, step)
    if filled_qty_d <= 0:
        raise BelowMinimumOrderError(
            f"buy filled_qty=0 after step floor: "
            f"requested_notional_krw={requested_d}, "
            f"fill_price={fill_price_d}, step={step}"
        )

    order_notional_d = fill_price_d * filled_qty_d
    fee_d = order_notional_d * snapshot.fee_rates.bid
    total_debit_d = order_notional_d + fee_d

    min_bid = snapshot.minimums.krw_min_total_bid
    if min_bid is not None and order_notional_d < min_bid:
        raise BelowMinimumOrderError(
            f"buy order_notional_krw={order_notional_d} < "
            f"snapshot.minimums.krw_min_total_bid={min_bid}"
        )

    if order_notional_d > config.max_notional_krw.value:
        raise NotionalCapExceededError(
            f"buy order_notional_krw={order_notional_d} > "
            f"config.max_notional_krw={config.max_notional_krw.value}"
        )

    if total_debit_d > state.cash_krw.value:
        raise InsufficientCashError(
            f"buy total_cash_debit_krw={total_debit_d} > "
            f"state.cash_krw={state.cash_krw.value} "
            f"(order_notional={order_notional_d}, fee={fee_d})"
        )

    cash_after_d = state.cash_krw.value - total_debit_d
    position_after_d = state.position_qty.value + filled_qty_d

    return LedgerEntry(
        side="buy",
        source_open_time_utc=intent.source_open_time_utc,
        signal_ts_utc=intent.signal_ts_utc,
        fill_ts_utc=fill_candle.open_time_utc,
        fill_price=Money(fill_price_d),
        requested_notional_krw=intent.requested_notional_krw,
        requested_qty=_ZERO_QTY,
        order_notional_krw=Money(order_notional_d),
        filled_qty=Qty(filled_qty_d),
        net_acquired_coin=Qty(filled_qty_d),
        gross_proceeds_krw=_ZERO_MONEY,
        net_proceeds_krw=_ZERO_MONEY,
        fee_krw=Money(fee_d),
        total_cash_debit_krw=Money(total_debit_d),
        cash_before_krw=state.cash_krw,
        cash_after_krw=Money(cash_after_d),
        position_before_qty=state.position_qty,
        position_after_qty=Qty(position_after_d),
    )


def _execute_sell(
    *,
    intent: OrderIntent,
    state: LedgerState,
    fill_candle: Candle,
    fill_price_d: Decimal,
    step: Decimal,
    snapshot: SnapshotV1,
    config: ExecutionConfig,
) -> LedgerEntry:
    assert intent.requested_qty is not None
    # Ordinary strategy-signal sell — the shared builder does the
    # math. Defaults keep ``execution_reason="strategy_signal"`` and
    # ``timing_semantics="open_boundary"`` on the resulting entry so
    # pre-existing behavior is byte-identical.
    return _build_sell_entry(
        state=state,
        snapshot=snapshot,
        config=config,
        source_open_time_utc=intent.source_open_time_utc,
        signal_ts_utc=intent.signal_ts_utc,
        fill_ts_utc=fill_candle.open_time_utc,
        fill_price_d=fill_price_d,
        step=step,
        requested_qty=intent.requested_qty,
    )


def _build_sell_entry(
    *,
    state: LedgerState,
    snapshot: SnapshotV1,
    config: ExecutionConfig,
    source_open_time_utc: datetime,
    signal_ts_utc: datetime,
    fill_ts_utc: datetime,
    fill_price_d: Decimal,
    step: Decimal,
    requested_qty: Qty,
    execution_reason: ExecutionReason = "strategy_signal",
    timing_semantics: TimingSemantics = "open_boundary",
    trigger_price: Money | None = None,
) -> LedgerEntry:
    """Package-internal sell-entry builder.

    The single sell arithmetic path — step-floor, min-order,
    position-sufficiency, fee, notional-cap, cash/position ledger
    deltas. Called from both :func:`_execute_sell` (strategy signal)
    and :mod:`bithumb_bot.execution.stop` (protective stop). The
    ``execution_reason`` / ``timing_semantics`` / ``trigger_price``
    parameters are the ONLY differences between the two call sites —
    every arithmetic step is identical and lives here, not duplicated.
    """
    requested_qty_d = requested_qty.value

    filled_qty_d = quantize_volume_down(requested_qty_d, step)
    if filled_qty_d <= 0:
        raise BelowMinimumOrderError(
            f"sell filled_qty=0 after step floor: "
            f"requested_qty={requested_qty_d}, step={step}"
        )

    if filled_qty_d > state.position_qty.value:
        raise InsufficientPositionError(
            f"sell filled_qty={filled_qty_d} > "
            f"state.position_qty={state.position_qty.value}"
        )

    gross_d = fill_price_d * filled_qty_d
    fee_d = gross_d * snapshot.fee_rates.ask
    net_proceeds_d = gross_d - fee_d

    # KRW-vs-KRW min-order check: Bithumb's `min_total` on the ask
    # side is a KRW-denominated minimum notional, not a coin quantity.
    # Compare gross_proceeds_krw (KRW) against krw_min_total_ask (KRW).
    # Uses the same step-floored qty and the same conservative sell
    # fill_price, so execution and evaluation cannot diverge.
    min_ask = snapshot.minimums.krw_min_total_ask
    if min_ask is not None and gross_d < min_ask:
        raise BelowMinimumOrderError(
            f"sell gross_proceeds_krw={gross_d} < "
            f"snapshot.minimums.krw_min_total_ask={min_ask}"
        )

    if gross_d > config.max_notional_krw.value:
        raise NotionalCapExceededError(
            f"sell gross_proceeds_krw={gross_d} > "
            f"config.max_notional_krw={config.max_notional_krw.value}"
        )

    cash_after_d = state.cash_krw.value + net_proceeds_d
    position_after_d = state.position_qty.value - filled_qty_d

    return LedgerEntry(
        side="sell",
        source_open_time_utc=source_open_time_utc,
        signal_ts_utc=signal_ts_utc,
        fill_ts_utc=fill_ts_utc,
        fill_price=Money(fill_price_d),
        requested_notional_krw=_ZERO_MONEY,
        requested_qty=requested_qty,
        order_notional_krw=Money(gross_d),  # pre-fee notional == gross_proceeds
        filled_qty=Qty(filled_qty_d),
        net_acquired_coin=_ZERO_QTY,
        gross_proceeds_krw=Money(gross_d),
        net_proceeds_krw=Money(net_proceeds_d),
        fee_krw=Money(fee_d),
        total_cash_debit_krw=_ZERO_MONEY,
        cash_before_krw=state.cash_krw,
        cash_after_krw=Money(cash_after_d),
        position_before_qty=state.position_qty,
        position_after_qty=Qty(position_after_d),
        execution_reason=execution_reason,
        timing_semantics=timing_semantics,
        trigger_price=trigger_price,
    )


__all__ = ["execute_intent"]
