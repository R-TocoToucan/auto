"""One-cycle live-trading coordinator for the BTC breakout candidate.

A single invocation performs exactly ONE cycle: validate → reconcile →
evaluate → at-most-one submit → persist → report. A restart-safe outer
loop (e.g. a PowerShell polling script) calls this once per tick; no
daemon, scheduler, WebSocket subscription, or thread is spun up here.

Priority order inside a cycle:

1. Load and strictly validate the persisted :class:`LiveState` (or its
   absence). Snapshot / config drift, missing sidecar, mutated processed
   candles, or any type/schema violation refuses BEFORE any broker
   mutation and leaves ``state.json`` byte-identical.
2. Reconcile every persisted managed order against the venue (even
   when HALT is present).
3. Fold newly-observed monotonic managed-order fill deltas into
   ``bot_owned_position_qty`` / ``bot_owned_cost_basis_krw``. Protective
   sells decrement only bot-owned qty; protective fills flip
   ``stopped_out_lockout``.
4. Refuse unexplained venue KRW/BTC drift (current available+locked vs.
   previous reconciled available+locked plus known fill deltas).
5. If ``state_dir/HALT`` exists: persist the reconciled state and report
   ``HALTED``.
6. Refuse if the venue reports an unmanaged open order for the market.
7. If a managed order is still non-terminal: persist and report its
   state.
8. Existing-BTC adoption (only on the very first live init).
9. Protective-stop dispatch when an active stop is persisted AND
   ``bot_owned_position_qty > 0``.
10. Breakout dispatch on the newest completed candle only.

Every fail-closed refusal happens BEFORE any broker mutation AND leaves
``state.json``, its sidecar, and every candle fingerprint byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from bithumb_bot.artifact.canonical import canonical_bytes
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.broker.interface import Broker
from bithumb_bot.broker.state import TERMINAL_STATES, BrokerOrder, OrderState
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import BithumbBotError
from bithumb_bot.execution.stop import ProtectiveStop
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.order_flow.breakout import (
    DispatchRefused,
    dispatch_breakout_signal,
)
from bithumb_bot.order_flow.live_state import (
    PROTECTIVE_STOP_FRACTION,
    ActiveStop,
    LiveState,
    LiveStateError,
    ManagedFill,
    append_processed,
    compute_config_sha256,
    compute_snapshot_sha256,
    initial_state,
    load_state,
    verify_processed_prefix,
    write_state,
)
from bithumb_bot.order_flow.protective import (
    ProtectiveDispatchError,
    StopObservation,
    dispatch_protective_stop,
)
from bithumb_bot.strategy.breakout import (
    BREAKOUT_MARKET,
    BREAKOUT_UNIT_MINUTES,
    generate_breakout_signals,
)

CycleStatus = Literal[
    "NOOP",
    "SUBMITTED",
    "OPEN",
    "PARTIAL",
    "FILLED",
    "HALTED",
]


class CycleRefused(BithumbBotError):
    """A cycle refused fail-closed BEFORE any broker mutation."""


class LiveBrokerCapabilityProtocol(Protocol):
    """The narrow live-broker surface the coordinator depends on."""

    def reconcile_all(self) -> list[Any]: ...

    def fetch_balances(self) -> Any: ...

    def fetch_current_price(self) -> Money: ...

    def fetch_current_candle(self) -> dict[str, Any]: ...

    def list_open_venue_orders(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CycleInputs:
    """The exact per-cycle inputs handed to :func:`run_one_cycle`."""

    dataset: CandleDataset
    snapshot: SnapshotV1
    state_dir: Path
    max_notional_krw: Money
    broker: Broker
    now_utc: datetime
    current_price_fetcher: Callable[[], Money] | None = None
    current_candle_fetcher: Callable[[], dict[str, Any]] | None = None
    balances_fetcher: Callable[[], Any] | None = None
    venue_open_lister: Callable[[], list[dict[str, Any]]] | None = None
    starting_cash_krw: Money | None = None
    adopt_existing_btc: bool = False


@dataclass(frozen=True)
class CycleResult:
    """Immutable outcome of one :func:`run_one_cycle` call."""

    status: CycleStatus
    submitted_order: BrokerOrder | None
    active_order: BrokerOrder | None
    stopped_out_lockout: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_one_cycle(inputs: CycleInputs) -> CycleResult:
    """Execute exactly one cycle. See module docstring for priority order."""
    _validate_inputs(inputs)

    state_dir = inputs.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load + strictly validate persisted state (or start fresh).
    try:
        persisted = load_state(state_dir)
    except LiveStateError as exc:
        raise CycleRefused(f"persisted state refused: {exc}") from exc

    current_snapshot_sha = compute_snapshot_sha256(inputs.snapshot)
    current_config_sha = compute_config_sha256(
        market=inputs.dataset.market,
        unit_minutes=inputs.dataset.unit_minutes,
        max_notional_krw=inputs.max_notional_krw,
    )

    if persisted is None:
        state = initial_state(
            market=inputs.dataset.market,
            unit_minutes=inputs.dataset.unit_minutes,
            snapshot_sha256=current_snapshot_sha,
            config_sha256=current_config_sha,
        )
        first_init = True
    else:
        if persisted.snapshot_sha256 != current_snapshot_sha:
            raise CycleRefused(
                f"snapshot drift: persisted snapshot_sha256={persisted.snapshot_sha256} "
                f"!= current {current_snapshot_sha}; refuse fail-closed"
            )
        if persisted.config_sha256 != current_config_sha:
            raise CycleRefused(
                f"config drift: persisted config_sha256={persisted.config_sha256} "
                f"!= current {current_config_sha}; refuse fail-closed"
            )
        try:
            verify_processed_prefix(persisted, tuple(inputs.dataset.candles))
        except LiveStateError as exc:
            raise CycleRefused(f"processed-candle prefix refused: {exc}") from exc
        state = persisted
        first_init = False

    broker = inputs.broker
    if hasattr(broker, "reconcile_all"):
        all_managed = broker.reconcile_all()
        managed_orders: list[BrokerOrder] = [
            m.to_broker_order() for m in all_managed
        ]
        managed_cids: set[str] = {m.internal_client_order_id for m in all_managed}
        managed_venue_wire_cids: set[str] = {
            m.wire_client_order_id for m in all_managed
        }
    else:
        # Merge currently-open orders with every previously-known
        # managed cid so a terminal fill applied between cycles is
        # still folded (MockBroker path).
        managed_orders = list(broker.list_open())
        managed_cids = {o.client_order_id for o in managed_orders}
        for known_cid in state.known_managed_fills.keys():
            if known_cid in managed_cids:
                continue
            found = broker.get(known_cid)
            if found is not None:
                managed_orders.append(found)
                managed_cids.add(found.client_order_id)
        managed_venue_wire_cids = managed_cids

    # 2. Fold monotonic fill deltas into bot-owned totals.
    prior_fills = dict(state.known_managed_fills)
    state, cycle_btc_delta, cycle_krw_delta = _fold_managed_fills(
        state, managed_orders
    )
    # 2b. Reconcile active_stop + stopped_out_lockout with every newly
    #     observed fill (fresh or restart-discovered) BEFORE we consider
    #     returning early for an open managed order.
    state = _reconcile_stop_and_lockout(
        state, managed_orders, prior_fills, inputs.now_utc
    )

    # 3. Balances + drift check (skip on first init or when no fetcher).
    balances = None
    if inputs.balances_fetcher is not None:
        balances = inputs.balances_fetcher()

    adoption_applies = False
    if balances is not None:
        cur_krw_avail, cur_krw_locked, cur_btc_avail, cur_btc_locked = (
            _extract_balances(balances)
        )
        if first_init:
            # First run: no prior baseline to diff against. Adoption path
            # may fire; otherwise refuse on unexplained BTC below.
            if cur_btc_avail.value + cur_btc_locked.value > 0:
                if not inputs.adopt_existing_btc:
                    raise CycleRefused(
                        "venue reports nonzero BTC but no managed order history "
                        "exists; pass --adopt-existing-btc to explicitly adopt "
                        "the pre-existing position (one-time)"
                    )
                if managed_orders:
                    raise CycleRefused(
                        "adopt-existing-btc requires no managed order history; "
                        "found managed orders on disk"
                    )
                avg_buy = _extract_avg_buy_price(balances)
                if avg_buy is None or avg_buy.value <= 0:
                    raise CycleRefused(
                        "adopt-existing-btc requires a positive verified "
                        "avg_buy_price from the venue"
                    )
                total_btc = Qty(cur_btc_avail.value + cur_btc_locked.value)
                cost_basis = Money(avg_buy.value * total_btc.value)
                stop_price = Money(
                    avg_buy.value * (Decimal("1") - PROTECTIVE_STOP_FRACTION)
                )
                if stop_price.value <= 0:
                    raise CycleRefused(
                        "adopt-existing-btc computed stop_price <= 0"
                    )
                adopted_stop = ActiveStop(
                    stop_price=stop_price,
                    qty=total_btc,
                    activated_at_utc=inputs.now_utc,
                    entry_fill_price=avg_buy,
                )
                state = replace(
                    state,
                    bot_owned_position_qty=total_btc,
                    bot_owned_cost_basis_krw=cost_basis,
                    active_stop=adopted_stop,
                    strategy_state="LONG",
                    adopted_existing_btc=True,
                )
                adoption_applies = True
        else:
            if inputs.adopt_existing_btc:
                raise CycleRefused(
                    "adopt-existing-btc refused: state already exists on disk "
                    "(cannot re-adopt after restart)"
                )
            _refuse_balance_drift(
                state=state,
                cur_krw_avail=cur_krw_avail,
                cur_krw_locked=cur_krw_locked,
                cur_btc_avail=cur_btc_avail,
                cur_btc_locked=cur_btc_locked,
                cycle_btc_delta=cycle_btc_delta,
                cycle_krw_delta=cycle_krw_delta,
            )
        state = replace(
            state,
            last_reconciled_krw_available=cur_krw_avail,
            last_reconciled_krw_locked=cur_krw_locked,
            last_reconciled_btc_available=cur_btc_avail,
            last_reconciled_btc_locked=cur_btc_locked,
        )
    elif inputs.adopt_existing_btc and not first_init:
        raise CycleRefused(
            "adopt-existing-btc refused: state already exists on disk"
        )
    elif inputs.adopt_existing_btc and first_init and balances is None:
        raise CycleRefused(
            "adopt-existing-btc requires balances_fetcher to verify BTC + "
            "avg_buy_price"
        )

    # Cash for signal dispatch: prefer reconciled venue cash, else the
    # dry-run starting cash the operator supplied.
    if balances is not None:
        cash = _get_cash(balances)
    else:
        cash = inputs.starting_cash_krw or Money(Decimal("0"))
    position = state.bot_owned_position_qty

    halt_file = state_dir / "HALT"
    if halt_file.exists():
        result = CycleResult(
            status="HALTED",
            submitted_order=None,
            active_order=None,
            stopped_out_lockout=state.stopped_out_lockout,
            notes=("HALT file present; reconciled but submitting nothing",),
        )
        write_state(state_dir, state)
        _append_cycle_event(state_dir, inputs.now_utc, result, phase="halt")
        return result

    # 4. Refuse on unmanaged open venue orders.
    if inputs.venue_open_lister is not None:
        for row in inputs.venue_open_lister():
            row_cid = row.get("client_order_id")
            if row_cid not in managed_venue_wire_cids:
                raise CycleRefused(
                    f"unmanaged open venue order detected "
                    f"(client_order_id={row_cid!r}); halt and reconcile "
                    f"before proceeding"
                )

    # 5. Non-terminal managed orders → do not submit again.
    open_managed = [o for o in managed_orders if o.state not in TERMINAL_STATES]
    if open_managed:
        active = open_managed[0]
        status: CycleStatus = _map_state_to_status(active.state)
        result = CycleResult(
            status=status,
            submitted_order=None,
            active_order=active,
            stopped_out_lockout=state.stopped_out_lockout,
            notes=(
                f"managed order {active.client_order_id[:12]} still "
                f"non-terminal ({active.state.value}); no new order",
            ),
        )
        write_state(state_dir, state)
        _append_cycle_event(state_dir, inputs.now_utc, result, phase="open")
        return result

    if adoption_applies:
        write_state(state_dir, state)
        result = CycleResult(
            status="NOOP",
            submitted_order=None,
            active_order=None,
            stopped_out_lockout=state.stopped_out_lockout,
            notes=("adopted pre-existing BTC position",),
        )
        _append_cycle_event(state_dir, inputs.now_utc, result, phase="adopt")
        return result

    # 6. Protective-stop priority — persisted, not reconstructed.
    if (
        state.active_stop is not None
        and state.bot_owned_position_qty.value > 0
        and inputs.current_price_fetcher is not None
        and inputs.current_candle_fetcher is not None
    ):
        active_stop = state.active_stop
        stop = ProtectiveStop(
            stop_price=active_stop.stop_price,
            qty=state.bot_owned_position_qty,
            activated_at_utc=active_stop.activated_at_utc,
            unit_minutes=BREAKOUT_UNIT_MINUTES,
            entry_fill_price=active_stop.entry_fill_price,
        )
        observation = _build_stop_observation(
            inputs.current_candle_fetcher(),
            inputs.now_utc,
            current_price=inputs.current_price_fetcher(),
        )
        try:
            dispatch_result = dispatch_protective_stop(
                observation=observation,
                stop=stop,
                position=state.bot_owned_position_qty,
                state_dir=state_dir,
                broker=broker,
            )
        except ProtectiveDispatchError as exc:
            raise CycleRefused(
                f"protective dispatch refused: {type(exc).__name__}: {exc}"
            ) from exc
        if dispatch_result.triggered and dispatch_result.order is not None:
            # Re-fold the freshly-submitted protective order's fills,
            # then let the reconciler resize / clear the persisted stop
            # and flip the lockout on any observed protective fill.
            protective_prior_fills = dict(state.known_managed_fills)
            state, _btc_d, _krw_d = _fold_managed_fills(
                state, [dispatch_result.order]
            )
            state = _reconcile_stop_and_lockout(
                state,
                [dispatch_result.order],
                protective_prior_fills,
                inputs.now_utc,
            )
            if dispatch_result.stopped_out_lockout:
                state = replace(state, stopped_out_lockout=True)
            write_state(state_dir, state)
            result = CycleResult(
                status=_map_state_to_status(dispatch_result.order.state),
                submitted_order=dispatch_result.order,
                active_order=dispatch_result.order,
                stopped_out_lockout=state.stopped_out_lockout,
                notes=("protective stop triggered",),
            )
            _append_cycle_event(
                state_dir, inputs.now_utc, result, phase="protective"
            )
            return result

    # 7. Breakout signal on newly completed candles.
    unit_delta = timedelta(minutes=BREAKOUT_UNIT_MINUTES)
    completed = tuple(
        c for c in inputs.dataset.candles
        if c.open_time_utc + unit_delta <= inputs.now_utc
    )
    if not completed:
        return _noop_persist(
            state_dir, inputs.now_utc, state,
            note="no completed candle in dataset",
        )

    last_processed = state.last_processed_open_time_utc
    latest_candle = completed[-1]
    if last_processed is not None and latest_candle.open_time_utc <= last_processed:
        return _noop_persist(
            state_dir, inputs.now_utc, state,
            note="latest completed candle already processed",
        )

    # New candles since last cycle — append fingerprints BEFORE any
    # broker mutation succeeds; a strategy-dispatch refusal below will
    # unwind the pre-computed state by NOT writing.
    new_candles: list[Candle] = [
        c for c in completed
        if last_processed is None or c.open_time_utc > last_processed
    ]

    signals = generate_breakout_signals(completed)

    prior_last_processed_open_time = state.last_processed_open_time_utc
    prospective_state = append_processed(state, new_candles)
    latest_signal = signals[-1] if signals else None
    if latest_signal is None or latest_signal.source_open_time_utc != latest_candle.open_time_utc:
        # Update strategy_state marker if the last emitted signal is on a
        # candle whose fingerprint is now processed; also clear lockout
        # if we just observed a CASH transition while flat.
        prospective_state = _apply_signal_bookkeeping(
            prospective_state,
            latest_signal,
            position=state.bot_owned_position_qty,
            prior_last_processed=prior_last_processed_open_time,
        )
        write_state(state_dir, prospective_state)
        note = (
            "no breakout signal on latest completed candles"
            if latest_signal is None
            else (
                f"latest signal fires on "
                f"{latest_signal.source_open_time_utc.isoformat()}, "
                f"not on latest completed candle "
                f"{latest_candle.open_time_utc.isoformat()}"
            )
        )
        return _noop_result(state_dir, inputs.now_utc, prospective_state, note=note)

    bid_fee_rate = Decimal(str(inputs.snapshot.fee_rates.bid))
    try:
        submitted = dispatch_breakout_signal(
            signal=latest_signal,
            cash=cash,
            position=position,
            bid_fee_rate=bid_fee_rate,
            max_notional_krw=inputs.max_notional_krw,
            stopped_out_lockout=prospective_state.stopped_out_lockout,
            observed_at_utc=inputs.now_utc,
            broker=broker,
        )
    except DispatchRefused as exc:
        raise CycleRefused(f"breakout dispatch refused: {exc}") from exc

    prospective_state = _apply_signal_bookkeeping(
        prospective_state,
        latest_signal,
        position=state.bot_owned_position_qty,
        prior_last_processed=prior_last_processed_open_time,
    )
    if submitted is not None:
        submit_prior_fills = dict(prospective_state.known_managed_fills)
        prospective_state, _btc_d, _krw_d = _fold_managed_fills(
            prospective_state, [submitted]
        )
        # Freshly-submitted buys/sells also drive the active-stop /
        # lockout reconciliation, so the same rules apply here as in
        # the top-of-cycle reconcile phase.
        prospective_state = _reconcile_stop_and_lockout(
            prospective_state, [submitted], submit_prior_fills, inputs.now_utc
        )

    write_state(state_dir, prospective_state)

    if submitted is None:
        return _noop_result(
            state_dir, inputs.now_utc, prospective_state,
            note="breakout dispatch returned no-op",
        )

    result = CycleResult(
        status=_map_state_to_status(submitted.state),
        submitted_order=submitted,
        active_order=submitted,
        stopped_out_lockout=prospective_state.stopped_out_lockout,
        notes=("breakout signal dispatched",),
    )
    _append_cycle_event(state_dir, inputs.now_utc, result, phase="submit")
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_inputs(inputs: CycleInputs) -> None:
    if inputs.dataset.market != BREAKOUT_MARKET:
        raise CycleRefused(
            f"dataset market {inputs.dataset.market!r} must equal "
            f"{BREAKOUT_MARKET!r}"
        )
    if inputs.dataset.unit_minutes != BREAKOUT_UNIT_MINUTES:
        raise CycleRefused(
            f"dataset unit_minutes {inputs.dataset.unit_minutes} must equal "
            f"{BREAKOUT_UNIT_MINUTES}"
        )
    if inputs.snapshot.market != BREAKOUT_MARKET:
        raise CycleRefused(
            f"snapshot market {inputs.snapshot.market!r} must equal "
            f"{BREAKOUT_MARKET!r}"
        )
    if inputs.now_utc.tzinfo is None or inputs.now_utc.tzinfo.utcoffset(
        inputs.now_utc
    ) is None:
        raise CycleRefused("now_utc must be timezone-aware")
    if type(inputs.max_notional_krw.value) is not Decimal:  # noqa: E721
        raise CycleRefused("max_notional_krw must be an exact Decimal Money")
    if inputs.max_notional_krw.value <= 0:
        raise CycleRefused(
            f"max_notional_krw must be > 0, got "
            f"{inputs.max_notional_krw.value}"
        )


# ---------------------------------------------------------------------------
# Balance-reconciliation helpers
# ---------------------------------------------------------------------------


def _extract_balances(
    balances: Any,
) -> tuple[Money, Money, Qty, Qty]:
    """Return ``(krw_available, krw_locked, btc_available, btc_locked)``.

    Missing ``locked`` fields default to zero — the reconciler still
    compares against ``available + locked`` so a venue that doesn't
    surface a separate locked value collapses to available.
    """
    cash = _get_cash(balances)
    coin = _get_coin(balances)
    krw_locked = _get_optional_money(balances, "cash_krw_locked")
    btc_locked = _get_optional_qty(balances, "coin_qty_locked")
    return cash, krw_locked, coin, btc_locked


def _get_cash(balances: Any) -> Money:
    v = getattr(balances, "cash_krw", None)
    if not isinstance(v, Money):
        raise CycleRefused("balances.cash_krw must be Money")
    return v


def _get_coin(balances: Any) -> Qty:
    v = getattr(balances, "coin_qty", None)
    if not isinstance(v, Qty):
        raise CycleRefused("balances.coin_qty must be Qty")
    return v


def _get_optional_money(balances: Any, attr: str) -> Money:
    v = getattr(balances, attr, None)
    if v is None:
        return Money(Decimal("0"))
    if not isinstance(v, Money):
        raise CycleRefused(f"balances.{attr} must be Money or None")
    return v


def _get_optional_qty(balances: Any, attr: str) -> Qty:
    v = getattr(balances, attr, None)
    if v is None:
        return Qty(Decimal("0"))
    if not isinstance(v, Qty):
        raise CycleRefused(f"balances.{attr} must be Qty or None")
    return v


def _extract_avg_buy_price(balances: Any) -> Money | None:
    v = getattr(balances, "avg_buy_price", None)
    if v is None:
        return None
    if not isinstance(v, Money):
        raise CycleRefused("balances.avg_buy_price must be Money or None")
    return v


def _refuse_balance_drift(
    *,
    state: LiveState,
    cur_krw_avail: Money,
    cur_krw_locked: Money,
    cur_btc_avail: Qty,
    cur_btc_locked: Qty,
    cycle_btc_delta: Decimal,
    cycle_krw_delta: Decimal,
) -> None:
    """Refuse if current venue totals disagree with prev + this-cycle deltas.

    ``cycle_btc_delta`` / ``cycle_krw_delta`` are the *new* per-order
    fill deltas observed this cycle (positive = coin acquired via buy /
    KRW proceeds gained via sell; negative = KRW spent / coin sold).
    Anything the venue reports that cannot be explained by those
    deltas is unexplained drift and refuses fail-closed.
    """
    prev_total_krw = (
        state.last_reconciled_krw_available.value
        + state.last_reconciled_krw_locked.value
    )
    prev_total_btc = (
        state.last_reconciled_btc_available.value
        + state.last_reconciled_btc_locked.value
    )
    cur_total_krw = cur_krw_avail.value + cur_krw_locked.value
    cur_total_btc = cur_btc_avail.value + cur_btc_locked.value
    if cur_total_btc != prev_total_btc + cycle_btc_delta:
        raise CycleRefused(
            f"unexplained BTC drift: prev_total={prev_total_btc} "
            f"cur_total={cur_total_btc} known_fill_delta_this_cycle="
            f"{cycle_btc_delta}"
        )
    if cur_total_krw != prev_total_krw + cycle_krw_delta:
        raise CycleRefused(
            f"unexplained KRW drift: prev_total={prev_total_krw} "
            f"cur_total={cur_total_krw} known_fill_delta_this_cycle="
            f"{cycle_krw_delta}"
        )


# ---------------------------------------------------------------------------
# Managed-fill folding
# ---------------------------------------------------------------------------


def _fold_managed_fills(
    state: LiveState, orders: list[BrokerOrder]
) -> tuple[LiveState, Decimal, Decimal]:
    """Fold newly-observed monotonic fill deltas into bot-owned totals.

    Only the delta between an order's *current* cumulative fills and the
    persisted ``known_managed_fills`` entry for that order is applied —
    a restart cannot double-count a fill that was already folded in a
    previous cycle.

    KRW arithmetic uses the venue's cumulative ``paid_fee_krw`` (never
    an estimate):

    * Buy: ``krw_delta -= (new_executed_funds + new_paid_fee)``
    * Sell: ``krw_delta += (new_executed_funds - new_paid_fee)``

    BTC deltas are always ``executed_volume`` deltas.

    Returns ``(new_state, cycle_btc_delta, cycle_krw_delta)`` where the
    per-cycle deltas represent this cycle's net BTC/KRW movement at the
    venue (positive = coin acquired / KRW gained).
    """
    new_fills = dict(state.known_managed_fills)
    position_qty = state.bot_owned_position_qty.value
    cost_basis = state.bot_owned_cost_basis_krw.value
    btc_delta = Decimal("0")
    krw_delta = Decimal("0")
    for order in orders:
        prev = new_fills.get(order.client_order_id)
        prev_qty = prev.filled_qty.value if prev is not None else Decimal("0")
        prev_notional = (
            prev.filled_notional_krw.value if prev is not None else Decimal("0")
        )
        prev_fee = (
            prev.paid_fee_krw.value if prev is not None else Decimal("0")
        )
        cur_qty = order.filled_qty.value
        cur_notional = order.filled_notional_krw.value
        cur_fee = order.paid_fee_krw.value
        if cur_qty < prev_qty or cur_notional < prev_notional:
            raise CycleRefused(
                f"managed order {order.client_order_id[:12]} fills regressed"
            )
        if cur_fee < prev_fee:
            raise CycleRefused(
                f"managed order {order.client_order_id[:12]} paid_fee regressed"
            )
        qty_delta = cur_qty - prev_qty
        notional_delta = cur_notional - prev_notional
        fee_delta = cur_fee - prev_fee
        if qty_delta > 0 or notional_delta > 0 or fee_delta > 0:
            if order.intent.side == "buy":
                position_qty += qty_delta
                cost_basis += notional_delta
                btc_delta += qty_delta
                krw_delta -= notional_delta + fee_delta
            else:
                if qty_delta > position_qty:
                    raise CycleRefused(
                        f"managed sell {order.client_order_id[:12]} would drive "
                        f"bot-owned position negative"
                    )
                if position_qty > 0:
                    basis_removed = cost_basis * (qty_delta / position_qty)
                else:
                    basis_removed = Decimal("0")
                position_qty -= qty_delta
                cost_basis -= basis_removed
                if cost_basis < 0:
                    cost_basis = Decimal("0")
                btc_delta -= qty_delta
                krw_delta += notional_delta - fee_delta
        new_fills[order.client_order_id] = ManagedFill(
            side="buy" if order.intent.side == "buy" else "sell",
            filled_qty=Qty(cur_qty),
            filled_notional_krw=Money(cur_notional),
            paid_fee_krw=Money(cur_fee),
        )
    return (
        replace(
            state,
            bot_owned_position_qty=Qty(position_qty),
            bot_owned_cost_basis_krw=Money(cost_basis),
            known_managed_fills=new_fills,
        ),
        btc_delta,
        krw_delta,
    )


_PROTECTIVE_REASONS: frozenset[str] = frozenset(
    {"protective_stop_gap", "protective_stop_intrabar"}
)


def _reconcile_stop_and_lockout(
    state: LiveState,
    orders: list[BrokerOrder],
    prior_fills: dict[str, ManagedFill],
    now_utc: datetime,
) -> LiveState:
    """Arm/resize ``active_stop`` and flip ``stopped_out_lockout``.

    Called immediately after :func:`_fold_managed_fills`, before the
    "open managed order → early return" branch. Uses the diff between
    ``prior_fills`` (snapshotted before folding) and each order's
    current cumulative fills so restart-discovered fills also count.

    Rules:

    * Any newly-observed buy fill (partial or complete): create or
      resize ``active_stop`` to the total bot-owned position, with
      ``stop_price = VWAP * (1 - PROTECTIVE_STOP_FRACTION)`` using
      cumulative bot-owned cost basis / qty.
    * Any newly-observed sell fill: resize ``active_stop.qty`` to the
      new bot-owned position; clear when it hits zero.
    * Any newly-observed fill on a protective-stop intent: set
      ``stopped_out_lockout=True`` (regardless of whether the fill was
      discovered fresh or after a restart).
    """
    buy_fill_this_cycle = False
    sell_fill_this_cycle = False
    protective_fill_this_cycle = False
    for order in orders:
        prev = prior_fills.get(order.client_order_id)
        prev_qty = prev.filled_qty.value if prev is not None else Decimal("0")
        qty_delta = order.filled_qty.value - prev_qty
        if qty_delta <= 0:
            continue
        if order.intent.side == "buy":
            buy_fill_this_cycle = True
        else:
            sell_fill_this_cycle = True
            if order.intent.reason in _PROTECTIVE_REASONS:
                protective_fill_this_cycle = True

    active_stop = state.active_stop
    position_qty = state.bot_owned_position_qty
    cost_basis = state.bot_owned_cost_basis_krw

    if buy_fill_this_cycle and position_qty.value > 0:
        vwap = cost_basis.value / position_qty.value
        stop_price_value = vwap * (Decimal("1") - PROTECTIVE_STOP_FRACTION)
        if stop_price_value > 0:
            active_stop = ActiveStop(
                stop_price=Money(stop_price_value),
                qty=position_qty,
                activated_at_utc=(
                    active_stop.activated_at_utc
                    if active_stop is not None
                    else now_utc
                ),
                entry_fill_price=Money(vwap),
            )
    if sell_fill_this_cycle:
        if position_qty.value == 0:
            active_stop = None
        elif active_stop is not None:
            active_stop = ActiveStop(
                stop_price=active_stop.stop_price,
                qty=position_qty,
                activated_at_utc=active_stop.activated_at_utc,
                entry_fill_price=active_stop.entry_fill_price,
            )

    new_lockout = state.stopped_out_lockout or protective_fill_this_cycle
    return replace(
        state,
        active_stop=active_stop,
        stopped_out_lockout=new_lockout,
    )


def _apply_signal_bookkeeping(
    state: LiveState,
    latest_signal: Any,
    *,
    position: Qty,
    prior_last_processed: datetime | None,
) -> LiveState:
    """Update strategy_state, and clear lockout on newly processed CASH+flat.

    ``prior_last_processed`` is ``last_processed_open_time_utc`` from
    BEFORE the append of this cycle's new candles. A CASH signal whose
    source candle is at or before that timestamp is a re-emission of a
    historical transition — it must NOT clear the lockout.
    """
    if latest_signal is None:
        return state
    ts = latest_signal.target_state
    if ts == "LONG":
        return replace(state, strategy_state="LONG")
    if ts == "CASH":
        signal_from_new_candle = (
            prior_last_processed is None
            or latest_signal.source_open_time_utc > prior_last_processed
        )
        clear_lockout = (
            state.stopped_out_lockout
            and signal_from_new_candle
            and position.value == 0
            and state.bot_owned_position_qty.value == 0
        )
        return replace(
            state,
            strategy_state="CASH",
            stopped_out_lockout=(False if clear_lockout else state.stopped_out_lockout),
        )
    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_persist(
    state_dir: Path, now_utc: datetime, state: LiveState, *, note: str
) -> CycleResult:
    write_state(state_dir, state)
    return _noop_result(state_dir, now_utc, state, note=note)


def _noop_result(
    state_dir: Path, now_utc: datetime, state: LiveState, *, note: str
) -> CycleResult:
    result = CycleResult(
        status="NOOP",
        submitted_order=None,
        active_order=None,
        stopped_out_lockout=state.stopped_out_lockout,
        notes=(note,),
    )
    _append_cycle_event(state_dir, now_utc, result, phase="noop")
    return result


def _map_state_to_status(state: OrderState) -> CycleStatus:
    if state is OrderState.ACCEPTED:
        return "SUBMITTED"
    if state is OrderState.PARTIALLY_FILLED:
        return "PARTIAL"
    if state is OrderState.FILLED:
        return "FILLED"
    if state is OrderState.CANCELED:
        return "OPEN"
    return "HALTED"


def _build_stop_observation(
    current_candle_row: dict[str, Any],
    now_utc: datetime,
    current_price: Money,
) -> StopObservation:
    open_iso = current_candle_row.get("candle_date_time_utc")
    open_price_raw = current_candle_row.get("opening_price")
    if not isinstance(open_iso, str) or open_price_raw is None:
        raise CycleRefused(
            "current-candle fetch returned malformed candle_date_time_utc / "
            "opening_price"
        )
    try:
        open_dt = datetime.fromisoformat(open_iso)
    except ValueError as exc:
        raise CycleRefused(
            f"current-candle open time not parseable: {open_iso!r}"
        ) from exc
    if open_dt.tzinfo is None:
        open_dt = open_dt.replace(tzinfo=UTC)
    try:
        open_price = Money(Decimal(str(open_price_raw)))
    except Exception as exc:
        raise CycleRefused(
            f"current-candle opening_price not decimal-parseable"
        ) from exc
    return StopObservation(
        market=BREAKOUT_MARKET,
        candle_open_time_utc=open_dt,
        unit_minutes=BREAKOUT_UNIT_MINUTES,
        observed_at_utc=now_utc,
        candle_open_price=open_price,
        observed_price=current_price,
    )


# ---------------------------------------------------------------------------
# Append-only cycle log
# ---------------------------------------------------------------------------


_CYCLE_LOG_NAME = "cycle_events.jsonl"


def _append_cycle_event(
    state_dir: Path, now_utc: datetime, result: CycleResult, *, phase: str
) -> None:
    payload = {
        "at_utc": now_utc.astimezone(UTC).isoformat(),
        "phase": phase,
        "status": result.status,
        "stopped_out_lockout": result.stopped_out_lockout,
        "submitted_client_order_id": (
            result.submitted_order.client_order_id
            if result.submitted_order is not None
            else None
        ),
        "active_client_order_id": (
            result.active_order.client_order_id
            if result.active_order is not None
            else None
        ),
        "notes": list(result.notes),
    }
    line = canonical_bytes(payload)
    target = state_dir / _CYCLE_LOG_NAME
    with target.open("ab") as fh:
        fh.write(line)


__all__ = [
    "CycleInputs",
    "CycleRefused",
    "CycleResult",
    "CycleStatus",
    "LiveBrokerCapabilityProtocol",
    "run_one_cycle",
]
