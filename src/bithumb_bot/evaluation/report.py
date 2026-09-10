"""Post-hoc performance evaluation.

Pure, deterministic, Decimal-native. No wall clock, no randomness, no
I/O. Consumes a :class:`~bithumb_bot.backtest.BacktestResult` alongside
the same :class:`CandleDataset` / :class:`SnapshotV1` /
:class:`BacktestConfig` used by the runner; produces a frozen
:class:`PerformanceReport`.

Reuse discipline: valuation, tick/step rounding, adverse slippage,
and fee arithmetic are delegated to the engine's private helpers
(:func:`_check_fee_verification`, :func:`_require_tick`,
:func:`_require_step`, :func:`_slippage_and_tick`) and
:func:`quantize_volume_down`. This module does NOT re-derive any of
that math.

Time-window invariants (per operator's Gate-2 corrections, 2026-09-09):

* Evaluation equity curve BEGINS at the close of candle
  ``lookback_candles - 1`` (post-warm-up), with starting cash.
* First executable candle is the next one.
* Final valuation is the last processed candle close.
* Strategy and benchmark share EXACTLY the same first equity
  timestamp and the same final valuation timestamp — enforced by
  slicing both against the same indices.

Fail-closed disposition:

* Any of these conditions produces an evaluation-invalid report with
  headline metrics set to ``None`` (partial ledger + optional
  diagnostic curve retained):
  - unverified sell-fee status / missing tick / missing step;
  - insufficient candles after warm-up;
  - hypothetical liquidation notional exceeds
    ``execution.max_notional_krw`` at any evaluated candle;
  - ledger consistency violation.
* Invalid *source* :class:`BacktestResult` — headline metrics set to
  ``None``; diagnostic fields (actual fees, slippage, entries,
  equity curve where possible) are retained. Actual fees and modeled
  slippage from the partial path are labelled diagnostic, never
  performance.
* Any benchmark-construction failure yields
  ``benchmark_net_return = None`` and
  ``strategy_minus_benchmark_net_return = None`` with a stable
  ``benchmark_refusal_code`` — the strategy metrics still stand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import BacktestResult
from bithumb_bot.bithumb_spec.snapshot import SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.core.rounding import quantize_volume_down
from bithumb_bot.errors import (
    BelowMinimumOrderError,
    InsufficientCashError,
    InsufficientPositionError,
    NoNextCandleError,
    NotionalCapExceededError,
    SnapshotValidationError,
    UnverifiedFeeModelError,
)
from bithumb_bot.execution import (
    LedgerEntry,
    LedgerState,
    OrderIntent,
    execute_intent,
)

# NB: engine's private helpers reused deliberately to avoid duplicating
# the fee-verify / tick / step / slippage-snap arithmetic. Precedent:
# ``bithumb_bot.execution.stop`` already imports the same helpers.
from bithumb_bot.execution.engine import (
    _check_fee_verification,
    _ensure_tick_available,
    _resolve_step,
    _resolve_tick_for_price,
    _slippage_and_tick,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

#: 24/7 crypto convention: 6 * 365 native 240-minute periods per year.
PERIODS_PER_YEAR: int = 2_190

#: Risk-free rate fixed explicitly at zero for the MVP (documented per
#: operator instruction; NOT a claim about the real risk-free rate).
RISK_FREE_RATE: Decimal = Decimal("0")

_ZERO: Decimal = Decimal("0")
_ONE: Decimal = Decimal("1")
_BPS_DENOM: Decimal = Decimal("10000")
_ZERO_MONEY: Money = Money(_ZERO)
_ZERO_QTY: Qty = Qty(_ZERO)


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityPoint:
    """One snapshot at end-of-candle.

    ``candle_open_time_utc`` identifies the candle whose state this
    row reflects; ``mark_to_market_equity_krw`` and
    ``net_liquidation_equity_krw`` are valued at that candle's
    ``close_price``. ``unliquidatable_dust_qty`` is the sub-min-order
    coin quantity that could NOT be sold at that candle (per the
    conservative liquidation rule); ``estimated_liquidation_fee_krw``
    is the fee the venue would take on the hypothetical sell IF one
    executed (0 when the position is entirely dust).
    """

    candle_open_time_utc: datetime
    cash_krw: Money
    position_qty: Qty
    close_price: Money
    mark_to_market_equity_krw: Money
    net_liquidation_equity_krw: Money
    unliquidatable_dust_qty: Qty
    estimated_liquidation_fee_krw: Money


@dataclass(frozen=True)
class PerformanceReport:
    """Immutable performance + diagnostic report.

    Field groups (semantics carried by names, so gross/net and
    exact/estimated cannot be confused):

    * Validity — ``source_*``, ``evaluation_*``, ``benchmark_*``.
      A metric is trustworthy only if all three sets are ``None``.
    * Timing — the processed and evaluation timestamp ranges.
    * Equity curve — post-warm-up window when both source and
      evaluation are valid; the runner's partial-path diagnostic
      curve when the source refused.
    * Diagnostic accounting — populated even on invalidity
      (fee/slippage/turnover-input totals, trade count, pending intent,
      open position). NEVER used as headline performance on an invalid
      report.
    * Headline metrics — all ``None`` on any source, evaluation, or
      benchmark invalidity. On success, all ``Decimal``.
    * Constants used — ``periods_per_year``, ``risk_free_rate``.
    """

    # --- validity ---
    source_invalid_reason: str | None
    source_refusal_code: str | None
    evaluation_invalid_reason: str | None
    evaluation_refusal_code: str | None
    benchmark_invalid_reason: str | None
    benchmark_refusal_code: str | None

    # --- timing / range ---
    processed_first_open_utc: datetime | None
    processed_last_open_utc: datetime | None
    evaluation_first_open_utc: datetime | None
    evaluation_last_open_utc: datetime | None

    # --- equity curve (post-warm-up when valid; diagnostic when source invalid) ---
    equity_curve: tuple[EquityPoint, ...]

    # --- diagnostic accounting (populated even on invalidity) ---
    entries: tuple[LedgerEntry, ...]
    total_actual_fees_krw: Money
    total_modeled_slippage_krw: Money
    estimated_final_liquidation_fee_krw: Money
    trade_count: int
    open_position_qty: Qty
    pending_intent_count: int
    refused_run_count: int
    source_run_refused: bool
    used_provisional_fee_model: bool

    # --- headline strategy metrics (None on any invalidity) ---
    starting_equity_krw: Money | None
    ending_mark_to_market_equity_krw: Money | None
    ending_net_liquidation_equity_krw: Money | None
    strategy_net_return: Decimal | None
    gross_before_fees_after_slippage_return: Decimal | None
    max_drawdown_fraction: Decimal | None
    annualized_volatility: Decimal | None
    annualized_sharpe: Decimal | None
    closed_trade_win_rate: Decimal | None
    average_holding_period_hours: Decimal | None
    turnover: Decimal | None

    # --- benchmark ---
    benchmark_net_return: Decimal | None
    strategy_minus_benchmark_net_return: Decimal | None

    # --- constants used (recorded for auditability) ---
    periods_per_year: int = field(default=PERIODS_PER_YEAR)
    risk_free_rate: Decimal = field(default=RISK_FREE_RATE)


# ---------------------------------------------------------------------------
# liquidation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LiquidationOutcome:
    """Result of a hypothetical (valuation-only) sell.

    * ``net_proceeds_krw`` is post-fee KRW the operator would credit
      IF the sale executed.
    * ``estimated_fee_krw`` is the fee the venue would take, held
      SEPARATE from :attr:`PerformanceReport.total_actual_fees_krw`.
    * ``unliquidatable_dust_qty`` is the coin quantity that cannot
      be sold at this candle — either because the whole position is
      below the min-order (in which case dust == position), or
      because the step-floor left a residual (dust = position - filled).
    """

    net_proceeds_krw: Decimal
    estimated_fee_krw: Decimal
    unliquidatable_dust_qty: Decimal


def _hypothetical_liquidation(
    *,
    position_qty: Decimal,
    close_price: Decimal,
    snapshot: SnapshotV1,
    step: Decimal,
    ask_fee: Decimal,
    krw_min_total_ask: Decimal | None,
    slippage_bps: Decimal,
    max_notional_krw: Decimal,
) -> _LiquidationOutcome:
    """Compute the conservative hypothetical liquidation at one candle.

    Raises :class:`NotionalCapExceededError` if the gross proceeds
    exceed ``max_notional_krw`` — the caller (evaluator orchestrator)
    catches this and marks the whole evaluation invalid rather than
    clipping or extrapolating the slippage model.

    Side-effect-free: nothing is appended to any ledger, no state is
    mutated, no ``LedgerEntry`` is constructed.
    """
    if position_qty <= 0:
        return _LiquidationOutcome(_ZERO, _ZERO, _ZERO)

    filled_qty = quantize_volume_down(position_qty, step)
    if filled_qty <= 0:
        # Below the venue's step; not sellable as anything.
        return _LiquidationOutcome(_ZERO, _ZERO, position_qty)

    fill_price = _slippage_and_tick(
        base=close_price,
        side="sell",
        slippage_bps=slippage_bps,
        tick=_resolve_tick_for_price(snapshot, close_price),
    )
    gross = fill_price * filled_qty
    # KRW-vs-KRW dust check: Bithumb's `min_total` on the ask side is
    # KRW-denominated, not a coin quantity. Compare the hypothetical
    # gross_proceeds_krw against krw_min_total_ask so this decision
    # matches the execution engine's `_build_sell_entry` decision on
    # the same inputs.
    if krw_min_total_ask is not None and gross < krw_min_total_ask:
        return _LiquidationOutcome(_ZERO, _ZERO, position_qty)

    if gross > max_notional_krw:
        raise NotionalCapExceededError(
            f"hypothetical liquidation gross={gross} exceeds "
            f"max_notional_krw={max_notional_krw} at close={close_price} "
            f"— refusing to clip or extrapolate the slippage model"
        )
    fee = gross * ask_fee
    net_proceeds = gross - fee
    dust = position_qty - filled_qty  # < step, but not necessarily zero
    return _LiquidationOutcome(net_proceeds, fee, dust)


# ---------------------------------------------------------------------------
# equity-curve reconstruction (O(N + E) walk, no future leakage)
# ---------------------------------------------------------------------------


def _reconstruct_equity_curve(
    dataset: CandleDataset,
    entries: tuple[LedgerEntry, ...],
    starting_cash: Money,
    *,
    snapshot: SnapshotV1,
    step: Decimal,
    ask_fee: Decimal,
    krw_min_total_ask: Decimal | None,
    slippage_bps: Decimal,
    max_notional_krw: Decimal,
) -> list[EquityPoint]:
    """Walk candles in order, applying each fill at its recorded
    ``fill_ts_utc`` before snapshotting equity at that candle's close.
    Uses only ledger entries whose ``fill_ts_utc <= candle.open_time_utc``
    — no future entry, no future candle.
    """
    points: list[EquityPoint] = []
    cash: Decimal = starting_cash.value
    position: Decimal = _ZERO
    entry_idx = 0
    n_entries = len(entries)

    for candle in dataset.candles:
        # Apply all entries whose fill instant is at or before this candle.
        # In practice the runner emits fills at candle-open boundaries, so
        # equality is common; multiple fills at the same instant
        # (entry + same-candle intrabar stop) are applied in order.
        while (
            entry_idx < n_entries
            and entries[entry_idx].fill_ts_utc <= candle.open_time_utc
        ):
            entry = entries[entry_idx]
            cash = entry.cash_after_krw.value
            position = entry.position_after_qty.value
            entry_idx += 1

        close = candle.close.value
        outcome = _hypothetical_liquidation(
            position_qty=position,
            close_price=close,
            snapshot=snapshot,
            step=step,
            ask_fee=ask_fee,
            krw_min_total_ask=krw_min_total_ask,
            slippage_bps=slippage_bps,
            max_notional_krw=max_notional_krw,
        )
        mtm = cash + position * close
        net_liq = cash + outcome.net_proceeds_krw
        points.append(
            EquityPoint(
                candle_open_time_utc=candle.open_time_utc,
                cash_krw=Money(cash),
                position_qty=Qty(position),
                close_price=Money(close),
                mark_to_market_equity_krw=Money(mtm),
                net_liquidation_equity_krw=Money(net_liq),
                unliquidatable_dust_qty=Qty(outcome.unliquidatable_dust_qty),
                estimated_liquidation_fee_krw=Money(outcome.estimated_fee_krw),
            )
        )

    return points


# ---------------------------------------------------------------------------
# ledger validation + trade pairing
# ---------------------------------------------------------------------------


def _validate_and_pair_ledger(
    entries: tuple[LedgerEntry, ...],
) -> tuple[list[tuple[LedgerEntry, LedgerEntry]], LedgerEntry | None, str | None]:
    """Return ``(closed_pairs, trailing_unmatched_buy, invalid_reason)``.

    Invariants checked:

    * Position never negative.
    * Sell has a preceding matching buy (no orphan sell).
    * No new buy while a position is already open (single-position
      long-only design).
    * Each buy-sell pair closes the position exactly to zero (with
      exact-Decimal equality — the engine's step-floor makes this
      cleanly true).
    * A trailing unmatched buy is permitted (open position at end).
    """
    closed: list[tuple[LedgerEntry, LedgerEntry]] = []
    current_buy: LedgerEntry | None = None
    position: Decimal = _ZERO
    for entry in entries:
        if entry.side == "buy":
            if current_buy is not None or position != 0:
                return (
                    [],
                    None,
                    (
                        f"buy at {entry.fill_ts_utc.isoformat()} while a "
                        f"position of {position} is already open — the "
                        "single-position long-only invariant is violated"
                    ),
                )
            current_buy = entry
            position = entry.filled_qty.value
            if position < 0:
                return (
                    [],
                    None,
                    (
                        f"buy at {entry.fill_ts_utc.isoformat()} produced "
                        f"a negative position {position}"
                    ),
                )
        else:  # sell
            if current_buy is None:
                return (
                    [],
                    None,
                    (
                        f"sell at {entry.fill_ts_utc.isoformat()} has no "
                        "matching prior buy"
                    ),
                )
            new_position = position - entry.filled_qty.value
            if new_position < 0:
                return (
                    [],
                    None,
                    (
                        f"sell at {entry.fill_ts_utc.isoformat()} would drive "
                        f"position negative ({position} - "
                        f"{entry.filled_qty.value})"
                    ),
                )
            if new_position != 0:
                return (
                    [],
                    None,
                    (
                        f"sell at {entry.fill_ts_utc.isoformat()} does not "
                        f"close the position exactly: residual={new_position}"
                    ),
                )
            closed.append((current_buy, entry))
            current_buy = None
            position = _ZERO
    return closed, current_buy, None


# ---------------------------------------------------------------------------
# modeled slippage
# ---------------------------------------------------------------------------


def _compute_modeled_slippage_krw(
    entries: tuple[LedgerEntry, ...],
    by_open: dict[datetime, Candle],
) -> Decimal:
    """Sum of per-entry modeled-slippage KRW.

    * BUY: ``filled_qty * (fill_price - fill_candle.open)``.
    * SELL (strategy signal): ``filled_qty * (fill_candle.open - fill_price)``.
    * SELL (protective stop, gap or intrabar): use
      ``entry.trigger_price`` as the reference — no intrabar
      timestamp is inferred.
    """
    total: Decimal = _ZERO
    for entry in entries:
        if entry.side == "buy":
            fill_candle = by_open[entry.fill_ts_utc]
            ref = fill_candle.open.value
            total += entry.filled_qty.value * (entry.fill_price.value - ref)
        else:
            if entry.execution_reason == "strategy_signal":
                fill_candle = by_open[entry.fill_ts_utc]
                ref = fill_candle.open.value
            else:
                # protective_stop_gap / protective_stop_intrabar
                assert entry.trigger_price is not None, (
                    f"stop entry at {entry.fill_ts_utc.isoformat()} lacks "
                    "trigger_price — engine invariant violated"
                )
                ref = entry.trigger_price.value
            total += entry.filled_qty.value * (ref - entry.fill_price.value)
    return total


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _max_drawdown_fraction(curve: tuple[EquityPoint, ...]) -> Decimal:
    """Peak-to-trough max drawdown of the ``net_liquidation_equity_krw``
    series, expressed as a POSITIVE fraction in ``[0, 1]``. 0.15 means
    a 15% drop from the running peak.
    """
    if not curve:
        return _ZERO
    running_max: Decimal = curve[0].net_liquidation_equity_krw.value
    max_dd: Decimal = _ZERO
    for point in curve:
        v = point.net_liquidation_equity_krw.value
        if v > running_max:
            running_max = v
        if running_max > 0:
            dd = (running_max - v) / running_max
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _annualized_vol_and_sharpe(
    curve: tuple[EquityPoint, ...],
    *,
    periods_per_year: int,
    risk_free_rate: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    """Annualized volatility + Sharpe from per-candle equity returns.

    Returns ``(None, None)`` if there are fewer than 2 return
    observations. Returns ``(Decimal('0'), None)`` for zero-variance
    series (Sharpe undefined) so a flat cash-only run is honestly
    reported as "no volatility, Sharpe undefined" rather than 0 or
    infinity.
    """
    if len(curve) < 2:
        return None, None
    returns: list[Decimal] = []
    for i in range(1, len(curve)):
        prev = curve[i - 1].net_liquidation_equity_krw.value
        curr = curve[i].net_liquidation_equity_krw.value
        if prev <= 0:
            # Return undefined from a non-positive base; refuse to
            # fabricate one.
            return None, None
        returns.append(curr / prev - _ONE)
    if len(returns) < 2:
        return None, None

    n = Decimal(len(returns))
    mean = sum(returns, _ZERO) / n
    ss = sum(((r - mean) * (r - mean) for r in returns), _ZERO)
    variance = ss / (n - _ONE)
    if variance == 0:
        return _ZERO, None
    std = variance.sqrt()
    py_sqrt = Decimal(periods_per_year).sqrt()
    ann_vol = std * py_sqrt
    ann_return = mean * Decimal(periods_per_year)
    sharpe = (ann_return - risk_free_rate) / ann_vol
    return ann_vol, sharpe


def _closed_trade_stats(
    closed: list[tuple[LedgerEntry, LedgerEntry]],
) -> tuple[Decimal | None, Decimal | None]:
    """``(win_rate, avg_holding_period_hours)`` from validated pairs.

    Win = ``sell.net_proceeds_krw > buy.total_cash_debit_krw``. Holding
    period computed from integer seconds — no ``total_seconds()``
    (which returns float). Both ``None`` when there are no closed
    trades.
    """
    if not closed:
        return None, None
    wins = 0
    total_hold_seconds: int = 0
    for buy, sell in closed:
        if sell.net_proceeds_krw.value > buy.total_cash_debit_krw.value:
            wins += 1
        delta = sell.fill_ts_utc - buy.fill_ts_utc
        # candle boundaries have zero microseconds; days*86400 + seconds is exact.
        total_hold_seconds += delta.days * 86400 + delta.seconds
    n = len(closed)
    win_rate = Decimal(wins) / Decimal(n)
    avg_hours = Decimal(total_hold_seconds) / Decimal(3600) / Decimal(n)
    return win_rate, avg_hours


def _turnover(entries: tuple[LedgerEntry, ...], starting_equity: Decimal) -> Decimal:
    """(sum of buy order-notional + sum of sell gross-proceeds) / starting_equity."""
    total: Decimal = _ZERO
    for entry in entries:
        if entry.side == "buy":
            total += entry.order_notional_krw.value
        else:
            total += entry.gross_proceeds_krw.value
    if starting_equity <= 0:
        return _ZERO
    return total / starting_equity


# ---------------------------------------------------------------------------
# aligned benchmark
# ---------------------------------------------------------------------------


def _run_benchmark(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    config: BacktestConfig,
    *,
    first_executable_idx: int,
    liq_kwargs: dict[str, object],
) -> tuple[Decimal | None, str | None, str | None]:
    """Aligned long-only buy-and-hold benchmark.

    Enters at ``dataset.candles[first_executable_idx].open`` using the
    same starting sleeve, sizing formula, fee, adverse slippage, tick,
    step, min-order, and cap rule as the strategy engine — by calling
    :func:`execute_intent` on a fresh :class:`LedgerState`. Holds
    without exits. Values at the last candle's close via the same
    conservative hypothetical-liquidation function.

    Returns ``(benchmark_net_return, invalid_reason, refusal_code)``.
    Any construction failure yields ``None`` for the return.
    """
    if first_executable_idx >= len(dataset.candles):
        return None, "insufficient_candles_for_benchmark_entry", "InsufficientCandlesError"

    signal_candle = dataset.candles[first_executable_idx - 1]
    fee_rate_bid = snapshot.fee_rates.bid
    target_debit = config.starting_cash_krw.value * config.target_sleeve_fraction
    intended = target_debit / (_ONE + fee_rate_bid)
    if intended > config.execution.max_notional_krw.value:
        return (
            None,
            (
                f"benchmark intended pre-fee notional {intended} exceeds "
                f"max_validated_notional_krw {config.execution.max_notional_krw.value}"
            ),
            "NotionalCapExceededError",
        )

    intent = OrderIntent.buy_from_signal(signal_candle, Money(intended))
    view = dataset.model_copy(
        update={
            "candles": dataset.candles[: first_executable_idx + 1],
            "missing_intervals_utc": [],
        }
    )
    fresh_state = LedgerState(
        cash_krw=config.starting_cash_krw, position_qty=_ZERO_QTY
    )
    try:
        new_state, _entry = execute_intent(
            fresh_state, intent, view, snapshot, config.execution
        )
    except (
        UnverifiedFeeModelError,
        SnapshotValidationError,
        BelowMinimumOrderError,
        InsufficientCashError,
        NotionalCapExceededError,
        NoNextCandleError,
        InsufficientPositionError,
    ) as exc:
        return None, str(exc), type(exc).__name__

    last_close = dataset.candles[-1].close.value
    try:
        outcome = _hypothetical_liquidation(
            position_qty=new_state.position_qty.value,
            close_price=last_close,
            **liq_kwargs,  # type: ignore[arg-type]
        )
    except NotionalCapExceededError as exc:
        return None, str(exc), "NotionalCapExceededError"

    benchmark_end = new_state.cash_krw.value + outcome.net_proceeds_krw
    benchmark_return = benchmark_end / config.starting_cash_krw.value - _ONE
    return benchmark_return, None, None


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def evaluate_backtest(
    dataset: CandleDataset,
    snapshot: SnapshotV1,
    backtest_config: BacktestConfig,
    backtest_result: BacktestResult,
) -> PerformanceReport:
    """Produce a :class:`PerformanceReport` from an existing run.

    Does NOT re-run the strategy. Reuses the engine's fee/tick/step/
    slippage helpers and :func:`execute_intent` (for the benchmark
    entry only).
    """
    entries = backtest_result.entries
    source_run_refused = backtest_result.invalid_reason is not None
    source_invalid_reason = backtest_result.invalid_reason
    source_refusal_code = backtest_result.refusal_code

    # ---- Sell-side fee/tick/step verification (used for every liquidation) ----
    try:
        _check_fee_verification("sell", snapshot, backtest_config.execution)
    except UnverifiedFeeModelError as exc:
        return _refused_report(
            source_invalid_reason=source_invalid_reason,
            source_refusal_code=source_refusal_code,
            source_run_refused=source_run_refused,
            evaluation_invalid_reason=str(exc),
            evaluation_refusal_code=type(exc).__name__,
            backtest_result=backtest_result,
            equity_curve=(),
            actual_fees=_sum_fees(entries),
            trade_count=0,
            open_position_qty=backtest_result.final_position_qty,
        )
    try:
        _ensure_tick_available(snapshot)
        step = _resolve_step(snapshot, backtest_config.execution)
    except SnapshotValidationError as exc:
        return _refused_report(
            source_invalid_reason=source_invalid_reason,
            source_refusal_code=source_refusal_code,
            source_run_refused=source_run_refused,
            evaluation_invalid_reason=str(exc),
            evaluation_refusal_code=type(exc).__name__,
            backtest_result=backtest_result,
            equity_curve=(),
            actual_fees=_sum_fees(entries),
            trade_count=0,
            open_position_qty=backtest_result.final_position_qty,
        )

    ask_fee = snapshot.fee_rates.ask
    krw_min_total_ask = snapshot.minimums.krw_min_total_ask
    slippage_bps = backtest_config.execution.slippage_bps_per_side
    max_notional = backtest_config.execution.max_notional_krw.value
    liq_kwargs: dict[str, object] = {
        "snapshot": snapshot,
        "step": step,
        "ask_fee": ask_fee,
        "krw_min_total_ask": krw_min_total_ask,
        "slippage_bps": slippage_bps,
        "max_notional_krw": max_notional,
    }

    # ---- Ledger validation (regardless of source validity) ----
    closed_pairs, trailing_buy, ledger_error = _validate_and_pair_ledger(entries)
    if ledger_error is not None:
        return _refused_report(
            source_invalid_reason=source_invalid_reason,
            source_refusal_code=source_refusal_code,
            source_run_refused=source_run_refused,
            evaluation_invalid_reason=f"ledger consistency: {ledger_error}",
            evaluation_refusal_code="LedgerConsistencyError",
            backtest_result=backtest_result,
            equity_curve=(),
            actual_fees=_sum_fees(entries),
            trade_count=len(closed_pairs),
            open_position_qty=backtest_result.final_position_qty,
        )

    # ---- Equity-curve reconstruction (raises if any liquidation breaches cap) ----
    try:
        full_curve = _reconstruct_equity_curve(
            dataset,
            entries,
            backtest_config.starting_cash_krw,
            snapshot=snapshot,
            step=step,
            ask_fee=ask_fee,
            krw_min_total_ask=krw_min_total_ask,
            slippage_bps=slippage_bps,
            max_notional_krw=max_notional,
        )
    except NotionalCapExceededError as exc:
        return _refused_report(
            source_invalid_reason=source_invalid_reason,
            source_refusal_code=source_refusal_code,
            source_run_refused=source_run_refused,
            evaluation_invalid_reason=str(exc),
            evaluation_refusal_code="NotionalCapExceededError",
            backtest_result=backtest_result,
            equity_curve=(),
            actual_fees=_sum_fees(entries),
            trade_count=len(closed_pairs),
            open_position_qty=backtest_result.final_position_qty,
        )

    processed_first = dataset.candles[0].open_time_utc if dataset.candles else None
    processed_last = dataset.candles[-1].open_time_utc if dataset.candles else None

    # ---- Warm-up sufficiency ----
    lookback = backtest_config.strategy.lookback_candles
    if len(dataset.candles) < lookback + 1:
        return _refused_report(
            source_invalid_reason=source_invalid_reason,
            source_refusal_code=source_refusal_code,
            source_run_refused=source_run_refused,
            evaluation_invalid_reason=(
                f"insufficient candles after warm-up: need at least "
                f"{lookback + 1}, got {len(dataset.candles)}"
            ),
            evaluation_refusal_code="InsufficientCandlesError",
            backtest_result=backtest_result,
            equity_curve=tuple(full_curve),
            actual_fees=_sum_fees(entries),
            trade_count=len(closed_pairs),
            open_position_qty=backtest_result.final_position_qty,
            processed_first=processed_first,
            processed_last=processed_last,
        )

    # Post-warm-up window: from candle index (lookback - 1) — the first
    # candle at which the strategy has warmed up — through the last.
    evaluation_curve = tuple(full_curve[lookback - 1 :])

    by_open: dict[datetime, Candle] = {c.open_time_utc: c for c in dataset.candles}
    total_actual_fees = _sum_fees(entries)
    total_modeled_slippage = _compute_modeled_slippage_krw(entries, by_open)

    # ---- Source invalidity: headline metrics forced None ----
    if source_run_refused:
        return PerformanceReport(
            source_invalid_reason=source_invalid_reason,
            source_refusal_code=source_refusal_code,
            evaluation_invalid_reason=None,
            evaluation_refusal_code=None,
            benchmark_invalid_reason="source_run_refused",
            benchmark_refusal_code="SourceRunRefused",
            processed_first_open_utc=processed_first,
            processed_last_open_utc=processed_last,
            evaluation_first_open_utc=evaluation_curve[0].candle_open_time_utc
            if evaluation_curve
            else None,
            evaluation_last_open_utc=evaluation_curve[-1].candle_open_time_utc
            if evaluation_curve
            else None,
            equity_curve=evaluation_curve,
            entries=entries,
            total_actual_fees_krw=Money(total_actual_fees),
            total_modeled_slippage_krw=Money(total_modeled_slippage),
            estimated_final_liquidation_fee_krw=(
                evaluation_curve[-1].estimated_liquidation_fee_krw
                if evaluation_curve
                else _ZERO_MONEY
            ),
            trade_count=len(closed_pairs),
            open_position_qty=backtest_result.final_position_qty,
            pending_intent_count=1 if backtest_result.pending_intent else 0,
            refused_run_count=1,
            source_run_refused=True,
            used_provisional_fee_model=backtest_result.used_provisional_fee_model,
            starting_equity_krw=None,
            ending_mark_to_market_equity_krw=None,
            ending_net_liquidation_equity_krw=None,
            strategy_net_return=None,
            gross_before_fees_after_slippage_return=None,
            max_drawdown_fraction=None,
            annualized_volatility=None,
            annualized_sharpe=None,
            closed_trade_win_rate=None,
            average_holding_period_hours=None,
            turnover=None,
            benchmark_net_return=None,
            strategy_minus_benchmark_net_return=None,
        )

    # ---- Valid path: compute headline metrics ----
    starting_equity = backtest_config.starting_cash_krw
    ending_point = evaluation_curve[-1]
    ending_mtm = ending_point.mark_to_market_equity_krw
    ending_net_liq = ending_point.net_liquidation_equity_krw
    estimated_final_liq_fee = ending_point.estimated_liquidation_fee_krw

    strategy_net_return = (
        ending_net_liq.value / starting_equity.value - _ONE
    )
    fee_addback_end = (
        ending_net_liq.value
        + total_actual_fees
        + estimated_final_liq_fee.value
    )
    gross_before_fees_after_slippage_return = (
        fee_addback_end / starting_equity.value - _ONE
    )

    max_dd = _max_drawdown_fraction(evaluation_curve)
    ann_vol, sharpe = _annualized_vol_and_sharpe(
        evaluation_curve,
        periods_per_year=PERIODS_PER_YEAR,
        risk_free_rate=RISK_FREE_RATE,
    )
    win_rate, avg_hold = _closed_trade_stats(closed_pairs)
    turnover = _turnover(entries, starting_equity.value)

    # ---- Aligned benchmark ----
    benchmark_return, bench_reason, bench_code = _run_benchmark(
        dataset,
        snapshot,
        backtest_config,
        first_executable_idx=lookback,
        liq_kwargs=liq_kwargs,
    )
    strategy_minus_benchmark = (
        strategy_net_return - benchmark_return
        if benchmark_return is not None
        else None
    )

    return PerformanceReport(
        source_invalid_reason=source_invalid_reason,
        source_refusal_code=source_refusal_code,
        evaluation_invalid_reason=None,
        evaluation_refusal_code=None,
        benchmark_invalid_reason=bench_reason,
        benchmark_refusal_code=bench_code,
        processed_first_open_utc=processed_first,
        processed_last_open_utc=processed_last,
        evaluation_first_open_utc=evaluation_curve[0].candle_open_time_utc,
        evaluation_last_open_utc=evaluation_curve[-1].candle_open_time_utc,
        equity_curve=evaluation_curve,
        entries=entries,
        total_actual_fees_krw=Money(total_actual_fees),
        total_modeled_slippage_krw=Money(total_modeled_slippage),
        estimated_final_liquidation_fee_krw=estimated_final_liq_fee,
        trade_count=len(closed_pairs),
        open_position_qty=backtest_result.final_position_qty,
        pending_intent_count=1 if backtest_result.pending_intent else 0,
        refused_run_count=0,
        source_run_refused=False,
        used_provisional_fee_model=backtest_result.used_provisional_fee_model,
        starting_equity_krw=starting_equity,
        ending_mark_to_market_equity_krw=ending_mtm,
        ending_net_liquidation_equity_krw=ending_net_liq,
        strategy_net_return=strategy_net_return,
        gross_before_fees_after_slippage_return=gross_before_fees_after_slippage_return,
        max_drawdown_fraction=max_dd,
        annualized_volatility=ann_vol,
        annualized_sharpe=sharpe,
        closed_trade_win_rate=win_rate,
        average_holding_period_hours=avg_hold,
        turnover=turnover,
        benchmark_net_return=benchmark_return,
        strategy_minus_benchmark_net_return=strategy_minus_benchmark,
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _sum_fees(entries: tuple[LedgerEntry, ...]) -> Decimal:
    return sum((e.fee_krw.value for e in entries), _ZERO)


def _refused_report(
    *,
    source_invalid_reason: str | None,
    source_refusal_code: str | None,
    source_run_refused: bool,
    evaluation_invalid_reason: str,
    evaluation_refusal_code: str,
    backtest_result: BacktestResult,
    equity_curve: tuple[EquityPoint, ...],
    actual_fees: Decimal,
    trade_count: int,
    open_position_qty: Qty,
    processed_first: datetime | None = None,
    processed_last: datetime | None = None,
) -> PerformanceReport:
    """Build a fail-closed report with all headline metrics None."""
    return PerformanceReport(
        source_invalid_reason=source_invalid_reason,
        source_refusal_code=source_refusal_code,
        evaluation_invalid_reason=evaluation_invalid_reason,
        evaluation_refusal_code=evaluation_refusal_code,
        benchmark_invalid_reason="evaluation_refused",
        benchmark_refusal_code="EvaluationRefused",
        processed_first_open_utc=processed_first,
        processed_last_open_utc=processed_last,
        evaluation_first_open_utc=None,
        evaluation_last_open_utc=None,
        equity_curve=equity_curve,
        entries=backtest_result.entries,
        total_actual_fees_krw=Money(actual_fees),
        total_modeled_slippage_krw=_ZERO_MONEY,
        estimated_final_liquidation_fee_krw=_ZERO_MONEY,
        trade_count=trade_count,
        open_position_qty=open_position_qty,
        pending_intent_count=1 if backtest_result.pending_intent else 0,
        refused_run_count=1 if source_run_refused else 0,
        source_run_refused=source_run_refused,
        used_provisional_fee_model=backtest_result.used_provisional_fee_model,
        starting_equity_krw=None,
        ending_mark_to_market_equity_krw=None,
        ending_net_liquidation_equity_krw=None,
        strategy_net_return=None,
        gross_before_fees_after_slippage_return=None,
        max_drawdown_fraction=None,
        annualized_volatility=None,
        annualized_sharpe=None,
        closed_trade_win_rate=None,
        average_holding_period_hours=None,
        turnover=None,
        benchmark_net_return=None,
        strategy_minus_benchmark_net_return=None,
    )


__all__ = [
    "PERIODS_PER_YEAR",
    "RISK_FREE_RATE",
    "EquityPoint",
    "PerformanceReport",
    "evaluate_backtest",
]
