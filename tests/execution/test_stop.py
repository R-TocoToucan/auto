"""Hand-calculated tests for the protective-stop simulator.

Anchor values (matched to tests/execution/test_engine.py):

* entry-fill candle open       = 100_000_000 KRW / BTC
* slippage                     = 50 bps per side
* tick                         = 1000 KRW
* step                         = 0.001 BTC
* bid_fee / ask_fee            = 0.0025
* config.max_notional_krw      = 100_000_000 KRW
* entry.fill_price after buy   = 100_500_000  (100_000_000 * 1.005, on grid)
* entry.filled_qty             = 0.1
* cash after entry             = 20_000_000 - 10_075_125 = 9_924_875
* stop_price                   = 95_000_000

Intrabar (base = stop_price = 95_000_000):
    fill_price          = floor(95_000_000 * 0.995, 1000) = 94_525_000
    gross_proceeds_krw  = 94_525_000 * 0.1                =  9_452_500
    fee_krw             = 9_452_500 * 0.0025              =     23_631.25
    net_proceeds_krw    = 9_452_500 - 23_631.25           =  9_428_868.75
    cash_after          = 9_924_875 + 9_428_868.75        = 19_353_743.75

Gap (base = trigger candle open = 94_000_000):
    fill_price          = floor(94_000_000 * 0.995, 1000) = 93_530_000
    gross_proceeds_krw  = 93_530_000 * 0.1                =  9_353_000
    fee_krw             = 9_353_000 * 0.0025              =     23_382.50
    net_proceeds_krw    = 9_353_000 - 23_382.50           =  9_329_617.50
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    InsufficientPositionError,
    MissingIntervalInStopWindowError,
    NoNextCandleError,
    SnapshotValidationError,
    UnverifiedFeeModelError,
)
from bithumb_bot.execution import (
    ExecutionConfig,
    LedgerEntry,
    LedgerState,
    OrderIntent,
    ProtectiveStop,
    evaluate_protective_stop,
    execute_intent,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance


UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=UNIT)
T2 = T0 + timedelta(minutes=2 * UNIT)
T3 = T0 + timedelta(minutes=3 * UNIT)


def _candle(
    open_time: datetime,
    *,
    o: str = "100000000",
    h: str | None = None,
    lo: str | None = None,
    c: str | None = None,
) -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=open_time,
        open=o,
        high=h if h is not None else o,
        low=lo if lo is not None else o,
        close=c if c is not None else o,
        volume="1",
        quote_volume=o,
    )


def _dataset(
    candles: list[Candle],
    *,
    missing: list[datetime] | None = None,
) -> CandleDataset:
    end = candles[-1].open_time_utc + timedelta(minutes=UNIT)
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=UNIT,
        requested_start_utc=candles[0].open_time_utc.isoformat(),
        requested_end_utc=end.isoformat(),
        fetched_at_utc="2026-09-09T00:00:00+00:00",
        candles=candles,
        missing_intervals_utc=[dt.isoformat() for dt in (missing or [])],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=[],
            effective_end_utc=end.isoformat(),
        ),
    )


def _snapshot(
    *,
    buy_status: Literal[
        "confirmed_read_only",
        "provisional_documented",
        "unresolved_until_M6B",
        "contradicted",
    ] = "provisional_documented",
    sell_status: Literal[
        "confirmed_read_only",
        "provisional_documented",
        "unresolved_until_M6B",
        "contradicted",
    ] = "confirmed_read_only",
    tick: str | None = "1000",
    step: str | None = "0.001",
) -> SnapshotV1:
    price_tick_rules: dict[str, Decimal] = {}
    if tick is not None:
        price_tick_rules["default_tick"] = Decimal(tick)
    quantity_step_rules: dict[str, Decimal] = {}
    if step is not None:
        quantity_step_rules["default_step"] = Decimal(step)
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        retrieved_at_utc="2026-09-09T00:00:00Z",
        source_endpoints=["/v1/orders/chance"],
        fee_rates=FeeRates(bid="0.0025", ask="0.0025"),
        # Both minimums are KRW-denominated notionals. Bithumb's
        # `min_total` on the ask side is a KRW minimum notional (not a
        # coin quantity); the engine compares gross_proceeds_krw
        # against it — see the KRW-vs-KRW fix in
        # bithumb_bot.execution.engine._build_sell_entry.
        minimums=Minimums(krw_min_total_bid="5000", krw_min_total_ask="5000"),
        price_tick_rules=price_tick_rules,
        quantity_step_rules=quantity_step_rules,
        supported_order_types=["price", "market", "limit"],
        verification_status={
            "general_fee_rate": sell_status,
            "market_buy_fee_reservation": buy_status,
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
        },
        source_fixture_hashes=["0" * 64],
    )


def _config() -> ExecutionConfig:
    return ExecutionConfig(
        slippage_bps_per_side=Decimal("50"),
        max_notional_krw=Money(Decimal("100000000")),
        allow_provisional_fee_model=True,
    )


def _entry_at_t1(
    entry_candle: Candle | None = None,
) -> tuple[LedgerState, LedgerEntry, Candle]:
    """Run the standard buy at T0's signal; return the post-entry state.

    ``entry_candle`` optionally overrides the entry-fill candle (used
    by same-candle intrabar tests to give the entry candle a low that
    breaches the stop).
    """
    candle_signal = _candle(T0)
    candle_fill = entry_candle if entry_candle is not None else _candle(T1)
    dataset = _dataset([candle_signal, candle_fill])
    snap = _snapshot()
    config = _config()
    intent = OrderIntent.buy_from_signal(candle_signal, Money(Decimal("10050000")))
    initial = LedgerState(
        cash_krw=Money(Decimal("20000000")), position_qty=Qty(Decimal("0"))
    )
    state, entry = execute_intent(initial, intent, dataset, snap, config)
    return state, entry, candle_fill


# ---------------------------------------------------------------------------
# ProtectiveStop construction validation
# ---------------------------------------------------------------------------


class TestProtectiveStopValidation:
    def test_stop_price_below_entry_required(self) -> None:
        _state, entry, _ = _entry_at_t1()
        # entry.fill_price = 100_500_000; stop equal → refused.
        with pytest.raises(ValueError, match="stop_price"):
            ProtectiveStop.from_entry(
                entry, stop_price=Money(Decimal("100500000"))
            )

    def test_stop_price_above_entry_refused(self) -> None:
        _state, entry, _ = _entry_at_t1()
        with pytest.raises(ValueError, match="stop_price"):
            ProtectiveStop.from_entry(
                entry, stop_price=Money(Decimal("101000000"))
            )

    def test_positive_qty_required(self) -> None:
        _state, entry, _ = _entry_at_t1()
        with pytest.raises(ValueError, match="qty"):
            ProtectiveStop.from_entry(
                entry, stop_price=Money(Decimal("95000000")), qty=Qty(Decimal("0"))
            )

    def test_activated_at_utc_equals_entry_fill_ts(self) -> None:
        _state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        assert stop.activated_at_utc == entry.fill_ts_utc

    def test_qty_defaults_to_entry_filled_qty(self) -> None:
        _state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        assert stop.qty == entry.filled_qty

    def test_from_entry_refuses_sell_entry(self) -> None:
        # Fake a sell entry — from_entry should refuse.
        _state, buy_entry, _ = _entry_at_t1()
        sell_like = LedgerEntry(
            side="sell",
            source_open_time_utc=buy_entry.source_open_time_utc,
            signal_ts_utc=buy_entry.signal_ts_utc,
            fill_ts_utc=buy_entry.fill_ts_utc,
            fill_price=buy_entry.fill_price,
            requested_notional_krw=Money(Decimal("0")),
            requested_qty=Qty(Decimal("0.1")),
            order_notional_krw=Money(Decimal("0")),
            filled_qty=Qty(Decimal("0.1")),
            net_acquired_coin=Qty(Decimal("0")),
            gross_proceeds_krw=Money(Decimal("0")),
            net_proceeds_krw=Money(Decimal("0")),
            fee_krw=Money(Decimal("0")),
            total_cash_debit_krw=Money(Decimal("0")),
            cash_before_krw=Money(Decimal("0")),
            cash_after_krw=Money(Decimal("0")),
            position_before_qty=Qty(Decimal("0")),
            position_after_qty=Qty(Decimal("0")),
        )
        with pytest.raises(ValueError, match="buy"):
            ProtectiveStop.from_entry(
                sell_like, stop_price=Money(Decimal("95000000"))
            )


# ---------------------------------------------------------------------------
# No-trigger paths
# ---------------------------------------------------------------------------


class TestNoTrigger:
    def test_all_candles_above_stop_no_trigger(self) -> None:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        # Entry-fill candle (T1) already has low=100M > stop=95M via
        # _candle default. Add one more later candle also above stop.
        candle_fill = _candle(T1)  # matches the one used in entry
        candle_next = _candle(
            T2, o="101000000", h="102000000", lo="99000000", c="100500000"
        )
        dataset = _dataset([candle_fill, candle_next])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is False
        assert result.fill_entry is None
        assert result.new_state == state
        assert result.remaining_position_qty == state.position_qty


# ---------------------------------------------------------------------------
# Ordinary intrabar crossing on a later candle
# ---------------------------------------------------------------------------


class TestOrdinaryIntrabarCrossing:
    def _run(self) -> tuple[LedgerState, LedgerEntry]:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)  # entry candle, no trigger
        candle_next = _candle(
            T2, o="98000000", h="99000000", lo="94000000", c="94500000"
        )
        dataset = _dataset([candle_fill, candle_next])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is True
        assert result.exit_reason == "intrabar_stop_crossed"
        assert result.trigger_candle_open_time_utc == T2
        assert result.trigger_price == Money(Decimal("95000000"))
        assert result.fill_entry is not None
        return result.new_state, result.fill_entry

    def test_fill_price_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.fill_price.value == Decimal("94525000")

    def test_gross_proceeds_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.gross_proceeds_krw.value == Decimal("9452500.000")

    def test_fee_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.fee_krw.value == Decimal("23631.2500000")

    def test_net_proceeds_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.net_proceeds_krw.value == Decimal("9428868.7500000")

    def test_ledger_execution_reason_and_timing(self) -> None:
        _new_state, entry = self._run()
        assert entry.execution_reason == "protective_stop_intrabar"
        assert entry.timing_semantics == "within_candle_unknown"
        assert entry.trigger_price == Money(Decimal("95000000"))

    def test_position_fully_exited(self) -> None:
        new_state, entry = self._run()
        assert entry.position_after_qty.value == Decimal("0.000")
        assert new_state.position_qty.value == Decimal("0.000")


# ---------------------------------------------------------------------------
# Gap below stop
# ---------------------------------------------------------------------------


class TestGapBelowStop:
    def _run(self) -> tuple[LedgerState, LedgerEntry]:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)
        candle_gap = _candle(
            T2, o="94000000", h="95000000", lo="93000000", c="93500000"
        )
        dataset = _dataset([candle_fill, candle_gap])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is True
        assert result.exit_reason == "gap_below_stop"
        assert result.trigger_price == Money(Decimal("94000000"))
        assert result.fill_entry is not None
        return result.new_state, result.fill_entry

    def test_fill_price_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.fill_price.value == Decimal("93530000")

    def test_gross_proceeds_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.gross_proceeds_krw.value == Decimal("9353000.000")

    def test_fee_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.fee_krw.value == Decimal("23382.5000000")

    def test_net_proceeds_hand_checked(self) -> None:
        _new_state, entry = self._run()
        assert entry.net_proceeds_krw.value == Decimal("9329617.5000000")

    def test_ledger_execution_reason_and_timing(self) -> None:
        _new_state, entry = self._run()
        assert entry.execution_reason == "protective_stop_gap"
        assert entry.timing_semantics == "open_boundary"
        assert entry.trigger_price == Money(Decimal("94000000"))


# ---------------------------------------------------------------------------
# Same-candle (entry-fill candle) intrabar crossing
# ---------------------------------------------------------------------------


class TestActivationCandleGapNeverClassified:
    """Regression: on the ACTIVATION candle, ``candle.open <= stop_price``
    must NOT be classified as ``protective_stop_gap`` — the stop did not
    exist before that candle opened, so there was no level for the price
    to gap through.
    """

    def test_activation_candle_open_below_stop_still_intrabar(self) -> None:
        # entry candle open = 95_000_000 (BELOW the eventual stop);
        # adverse buy fill = 95_000_000 * 1.005 = 95_475_000 (ABOVE stop);
        # stop = 95_100_000 (below entry.fill_price, valid protective);
        # candle low = 94_000_000 (BELOW stop) → intrabar, NOT gap.
        entry_candle = _candle(
            T1, o="95000000", h="96000000", lo="94000000", c="95500000"
        )
        state, entry, _ = _entry_at_t1(entry_candle=entry_candle)
        assert entry.fill_price.value == Decimal("95475000")  # sanity: fill > stop
        assert entry_candle.open.value < Decimal("95100000")  # sanity: open < stop
        stop = ProtectiveStop.from_entry(
            entry, stop_price=Money(Decimal("95100000"))
        )
        assert stop.activated_at_utc == T1
        dataset = _dataset([entry_candle])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is True
        assert result.exit_reason == "intrabar_stop_crossed"
        assert result.trigger_price == Money(Decimal("95100000"))
        assert result.fill_entry is not None
        assert result.fill_entry.execution_reason == "protective_stop_intrabar"
        assert result.fill_entry.timing_semantics == "within_candle_unknown"

    def test_later_candle_open_below_stop_still_gap(self) -> None:
        # Regression control: on a NON-activation candle, open <= stop
        # remains a gap classification. Same numeric setup as the
        # ordinary gap test to keep the contrast one-parameter.
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(
            entry, stop_price=Money(Decimal("95000000"))
        )
        candle_fill = _candle(T1)  # activation candle, no trigger
        candle_gap = _candle(
            T2, o="94000000", h="95000000", lo="93000000", c="93500000"
        )
        dataset = _dataset([candle_fill, candle_gap])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is True
        assert result.exit_reason == "gap_below_stop"
        assert result.trigger_price == Money(Decimal("94000000"))


class TestSameCandleIntrabarAfterEntry:
    def test_entry_candle_low_breaches_stop_triggers_intrabar(self) -> None:
        # Entry-fill candle open=100M (base for buy), low=94M breaches
        # stop=95M. Because activated_at_utc == entry.fill_ts_utc, THIS
        # candle is eligible. Result: conservative "entry filled at
        # open, then stop crossed intrabar" model.
        entry_candle = _candle(
            T1, o="100000000", h="100000000", lo="94000000", c="94500000"
        )
        state, entry, _ = _entry_at_t1(entry_candle=entry_candle)
        assert entry.fill_price.value == Decimal("100500000")  # sanity
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        assert stop.activated_at_utc == T1
        dataset = _dataset([entry_candle])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is True
        assert result.exit_reason == "intrabar_stop_crossed"
        assert result.trigger_candle_open_time_utc == T1
        assert result.trigger_price == Money(Decimal("95000000"))
        assert result.fill_entry is not None
        assert result.fill_entry.execution_reason == "protective_stop_intrabar"
        assert result.fill_entry.timing_semantics == "within_candle_unknown"
        # Cash after entry (9_924_875) + net_proceeds 9_428_868.75.
        assert result.new_state.cash_krw.value == Decimal("19353743.7500000")
        assert result.new_state.position_qty.value == Decimal("0.000")


# ---------------------------------------------------------------------------
# Missing-interval fail-closed
# ---------------------------------------------------------------------------


class TestMissingIntervalFailsClosed:
    def test_reported_missing_activation_slot_refused(self) -> None:
        # Dataset lists T1 in missing_intervals_utc. Since activated_at=T1,
        # the very first expected slot is missing → refuse.
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        # Give the dataset SOMETHING at or after T1 so we don't hit
        # NoNextCandleError first. Put a candle at T2 that would gap.
        candle_t2 = _candle(
            T2, o="94000000", h="95000000", lo="93000000", c="93500000"
        )
        dataset = _dataset([candle_t2], missing=[T1])
        with pytest.raises(
            MissingIntervalInStopWindowError, match="listed in"
        ):
            evaluate_protective_stop(state, stop, dataset, _snapshot(), _config())

    def test_unreported_gap_refused(self) -> None:
        # activated_at=T1. Dataset has T1 and T3 (T2 absent and not
        # listed). Slot walk hits T2 → unreported gap → refuse.
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)
        candle_t3 = _candle(T3, o="94000000", lo="93000000", h="95000000", c="93500000")
        dataset = _dataset([candle_fill, candle_t3])
        with pytest.raises(MissingIntervalInStopWindowError, match="unreported"):
            evaluate_protective_stop(state, stop, dataset, _snapshot(), _config())


# ---------------------------------------------------------------------------
# Position and dataset boundary refusals
# ---------------------------------------------------------------------------


class TestBoundaryRefusals:
    def test_stop_qty_exceeds_position_refused(self) -> None:
        state, entry, _ = _entry_at_t1()
        # position=0.1; ask for stop qty 0.2.
        stop = ProtectiveStop.from_entry(
            entry, stop_price=Money(Decimal("95000000")), qty=Qty(Decimal("0.2"))
        )
        candle_fill = _candle(T1)
        dataset = _dataset([candle_fill])
        with pytest.raises(InsufficientPositionError):
            evaluate_protective_stop(state, stop, dataset, _snapshot(), _config())

    def test_no_later_candle_refused(self) -> None:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        # Dataset has ONLY the signal candle (T0). No candle at or
        # after activated_at=T1 → NoNextCandleError.
        dataset = _dataset([_candle(T0)])
        with pytest.raises(NoNextCandleError):
            evaluate_protective_stop(state, stop, dataset, _snapshot(), _config())


# ---------------------------------------------------------------------------
# Partial protected qty
# ---------------------------------------------------------------------------


class TestPartialProtectedQty:
    def test_partial_qty_leaves_residual_position(self) -> None:
        state, entry, _ = _entry_at_t1()
        # Protect only half the position (0.05 of 0.1). The remainder
        # stays open.
        stop = ProtectiveStop.from_entry(
            entry,
            stop_price=Money(Decimal("95000000")),
            qty=Qty(Decimal("0.05")),
        )
        candle_fill = _candle(T1)
        candle_next = _candle(
            T2, o="94000000", h="95000000", lo="93000000", c="93500000"
        )
        dataset = _dataset([candle_fill, candle_next])
        result = evaluate_protective_stop(
            state, stop, dataset, _snapshot(), _config()
        )
        assert result.triggered is True
        assert result.exit_reason == "gap_below_stop"
        assert result.remaining_position_qty.value == Decimal("0.050")
        assert result.new_state.position_qty.value == Decimal("0.050")
        assert result.fill_entry is not None
        assert result.fill_entry.filled_qty.value == Decimal("0.050")


# ---------------------------------------------------------------------------
# Snapshot verification-status gating (via the shared engine helpers)
# ---------------------------------------------------------------------------


class TestSnapshotGating:
    def test_unverified_sell_fee_status_refused(self) -> None:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)
        dataset = _dataset([candle_fill])
        snap = _snapshot(sell_status="unresolved_until_M6B")
        with pytest.raises(UnverifiedFeeModelError):
            evaluate_protective_stop(state, stop, dataset, snap, _config())

    def test_missing_tick_refused(self) -> None:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)
        dataset = _dataset([candle_fill])
        with pytest.raises(SnapshotValidationError, match="tick"):
            evaluate_protective_stop(
                state, stop, dataset, _snapshot(tick=None), _config()
            )

    def test_missing_step_refused(self) -> None:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)
        dataset = _dataset([candle_fill])
        with pytest.raises(SnapshotValidationError, match="step"):
            evaluate_protective_stop(
                state, stop, dataset, _snapshot(step=None), _config()
            )


# ---------------------------------------------------------------------------
# Ordinary orders' execution_reason preserved (regression guard)
# ---------------------------------------------------------------------------


class TestOrdinaryOrdersRegression:
    def test_ordinary_buy_defaults_to_strategy_signal(self) -> None:
        _state, entry, _ = _entry_at_t1()
        assert entry.execution_reason == "strategy_signal"
        assert entry.timing_semantics == "open_boundary"
        assert entry.trigger_price is None

    def test_ordinary_sell_via_execute_intent_defaults(self) -> None:
        state, entry, _ = _entry_at_t1()
        candle_fill = _candle(T1)
        candle_next = _candle(T2)
        dataset = _dataset([candle_fill, candle_next])
        intent = OrderIntent.sell_from_signal(candle_fill, Qty(Decimal("0.1")))
        _new_state, sell_entry = execute_intent(
            state, intent, dataset, _snapshot(), _config()
        )
        assert sell_entry.execution_reason == "strategy_signal"
        assert sell_entry.timing_semantics == "open_boundary"
        assert sell_entry.trigger_price is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterministicReplay:
    def test_two_runs_produce_identical_evaluation(self) -> None:
        state, entry, _ = _entry_at_t1()
        stop = ProtectiveStop.from_entry(entry, stop_price=Money(Decimal("95000000")))
        candle_fill = _candle(T1)
        candle_next = _candle(
            T2, o="98000000", h="99000000", lo="94000000", c="94500000"
        )
        dataset = _dataset([candle_fill, candle_next])
        snap = _snapshot()
        config = _config()
        a = evaluate_protective_stop(state, stop, dataset, snap, config)
        b = evaluate_protective_stop(state, stop, dataset, snap, config)
        assert a.triggered == b.triggered
        assert a.exit_reason == b.exit_reason
        assert a.trigger_candle_open_time_utc == b.trigger_candle_open_time_utc
        assert a.trigger_price == b.trigger_price
        assert a.fill_entry == b.fill_entry
        assert a.new_state == b.new_state
        assert a.remaining_position_qty == b.remaining_position_qty
