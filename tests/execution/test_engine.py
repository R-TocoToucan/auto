"""Focused hand-calculated tests for the marketable-order execution core.

Every value below is hand-verifiable:

* fill candle open        = 100_000_000 KRW / BTC
* slippage                = 50 bps per side (0.50%)
* tick                    = 1000 KRW
* step                    = 0.001 BTC
* bid_fee / ask_fee       = 0.0025
* min_total_bid           = 5000 KRW
* min_total_ask           = 0.001 BTC
* config.max_notional_krw = 100_000_000 KRW (unless a test overrides it)

Buy math (unless a test overrides):
    adjusted_price      = 100_000_000 * (1 + 50/10000) = 100_500_000
    fill_price          = ceil(100_500_000, 1000)      = 100_500_000
    desired_qty         = 10_050_000 / 100_500_000     = 0.1
    filled_qty          = floor(0.1, 0.001)            = 0.1
    order_notional_krw  = 100_500_000 * 0.1            = 10_050_000
    fee_krw             = 10_050_000 * 0.0025          = 25_125
    total_cash_debit    = 10_050_000 + 25_125          = 10_075_125

Sell math (unless a test overrides):
    adjusted_price      = 100_000_000 * (1 - 50/10000) =  99_500_000
    fill_price          = floor(99_500_000, 1000)      =  99_500_000
    filled_qty          = floor(0.1, 0.001)            = 0.1
    gross_proceeds_krw  = 99_500_000 * 0.1             =  9_950_000
    fee_krw             = 9_950_000 * 0.0025           =     24_875
    net_proceeds_krw    = 9_950_000 - 24_875           =  9_925_125
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from bithumb_bot.bithumb_spec.snapshot import FeeRates, Minimums, SnapshotV1
from bithumb_bot.core.money import Money, Qty
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
    ExecutionConfig,
    LedgerEntry,
    LedgerState,
    OrderIntent,
    execute_intent,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance


UNIT = 240
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _candle(open_time_utc: datetime, price: str = "100000000") -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=open_time_utc,
        open=price,
        high=price,
        low=price,
        close=price,
        volume="1",
        quote_volume=price,
    )


def _dataset(candles: list[Candle]) -> CandleDataset:
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
        missing_intervals_utc=[],
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
    tick: str | None = "1000",
    step: str | None = "0.001",
    min_bid: str | None = "5000",
    min_ask: str | None = "0.001",
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
        minimums=Minimums(
            krw_min_total_bid=min_bid,
            krw_min_total_ask=min_ask,
        ),
        price_tick_rules=price_tick_rules,
        quantity_step_rules=quantity_step_rules,
        supported_order_types=["price", "market", "limit"],
        verification_status={
            "general_fee_rate": "confirmed_read_only",
            "market_buy_fee_reservation": buy_status,
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
        },
        source_fixture_hashes=["0" * 64],
    )


def _config(
    *,
    max_notional_krw: str = "100000000",
    slippage_bps: str = "50",
    allow_provisional: bool = True,
) -> ExecutionConfig:
    return ExecutionConfig(
        slippage_bps_per_side=Decimal(slippage_bps),
        max_notional_krw=Money(Decimal(max_notional_krw)),
        allow_provisional_fee_model=allow_provisional,
    )


def _initial_state(cash: str = "20000000", position: str = "0") -> LedgerState:
    return LedgerState(
        cash_krw=Money(Decimal(cash)), position_qty=Qty(Decimal(position))
    )


# ---------------------------------------------------------------------------
# Intent structural rules
# ---------------------------------------------------------------------------


class TestOrderIntentStructure:
    def test_signal_ts_is_close_boundary(self) -> None:
        candle = _candle(T0)
        intent = OrderIntent.buy_from_signal(candle, Money(Decimal("10050000")))
        assert intent.signal_ts_utc == T0 + timedelta(minutes=UNIT)
        assert intent.source_open_time_utc == T0

    def test_buy_requires_notional(self) -> None:
        with pytest.raises(ValueError, match="requested_notional_krw"):
            OrderIntent(
                side="buy",
                source_open_time_utc=T0,
                unit_minutes=UNIT,
                signal_ts_utc=T0 + timedelta(minutes=UNIT),
                requested_notional_krw=None,
                requested_qty=None,
            )

    def test_sell_requires_qty(self) -> None:
        with pytest.raises(ValueError, match="requested_qty"):
            OrderIntent(
                side="sell",
                source_open_time_utc=T0,
                unit_minutes=UNIT,
                signal_ts_utc=T0 + timedelta(minutes=UNIT),
                requested_notional_krw=None,
                requested_qty=None,
            )

    def test_mismatched_signal_ts_rejected(self) -> None:
        with pytest.raises(ValueError, match="signal_ts_utc"):
            OrderIntent(
                side="buy",
                source_open_time_utc=T0,
                unit_minutes=UNIT,
                signal_ts_utc=T0,  # wrong — must be T0 + UNIT
                requested_notional_krw=Money(Decimal("1")),
                requested_qty=None,
            )


# ---------------------------------------------------------------------------
# Temporal separation
# ---------------------------------------------------------------------------


class TestTemporalSeparation:
    def test_no_same_candle_fill(self) -> None:
        # Dataset holds ONLY candle t. Signal fires at t+unit; there's no
        # candle at or after t+unit → NoNextCandleError. Same-candle fill
        # is impossible by construction.
        candle_t = _candle(T0)
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        with pytest.raises(NoNextCandleError):
            execute_intent(
                _initial_state(),
                intent,
                _dataset([candle_t]),
                _snapshot(),
                _config(),
            )

    def test_fill_uses_next_candle_open(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        _new_state, entry = execute_intent(
            _initial_state(),
            intent,
            _dataset([candle_t, candle_t1]),
            _snapshot(),
            _config(),
        )
        assert entry.fill_ts_utc == candle_t1.open_time_utc
        assert entry.fill_ts_utc != intent.source_open_time_utc

    def test_missing_next_uses_first_available_later_no_fabrication(self) -> None:
        # Dataset has t and t+2*unit; t+1 is missing. Signal from t must
        # fill at t+2, using its REAL open price — nothing forward-filled.
        candle_t = _candle(T0, price="100000000")
        candle_t2 = _candle(T0 + timedelta(minutes=2 * UNIT), price="90000000")
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("9045000")))
        _new_state, entry = execute_intent(
            _initial_state(),
            intent,
            _dataset([candle_t, candle_t2]),
            _snapshot(),
            _config(),
        )
        assert entry.fill_ts_utc == candle_t2.open_time_utc
        # Fill price is derived from candle_t2's open (90M), not t (100M).
        # 90_000_000 * 1.005 = 90_450_000; ceil to tick 1000 = 90_450_000.
        assert entry.fill_price.value == Decimal("90450000")


# ---------------------------------------------------------------------------
# Buy path — hand-checked ledger
# ---------------------------------------------------------------------------


class TestBuyPath:
    def _buy_entry(self, **kwargs: object) -> LedgerEntry:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(
            candle_t, Money(Decimal("10050000"))
        )
        _new_state, entry = execute_intent(
            _initial_state(),
            intent,
            _dataset([candle_t, candle_t1]),
            _snapshot(),
            _config(),
        )
        return entry

    def test_fill_price_hand_checked(self) -> None:
        assert self._buy_entry().fill_price.value == Decimal("100500000")

    def test_filled_qty_hand_checked(self) -> None:
        assert self._buy_entry().filled_qty.value == Decimal("0.1")

    def test_order_notional_hand_checked(self) -> None:
        assert self._buy_entry().order_notional_krw.value == Decimal("10050000")

    def test_fee_hand_checked(self) -> None:
        assert self._buy_entry().fee_krw.value == Decimal("25125.0000")

    def test_total_cash_debit_hand_checked(self) -> None:
        assert (
            self._buy_entry().total_cash_debit_krw.value == Decimal("10075125.0000")
        )

    def test_net_acquired_coin_equals_filled_qty(self) -> None:
        entry = self._buy_entry()
        assert entry.net_acquired_coin.value == entry.filled_qty.value

    def test_requested_and_order_notional_are_distinct(self) -> None:
        # Same in this hand-checked case, but the fields are separate
        # concepts and both are populated on the entry.
        e = self._buy_entry()
        assert e.requested_notional_krw.value == Decimal("10050000")
        assert e.order_notional_krw.value == Decimal("10050000")

    def test_sell_fields_zero_on_buy(self) -> None:
        e = self._buy_entry()
        assert e.gross_proceeds_krw.value == Decimal("0")
        assert e.net_proceeds_krw.value == Decimal("0")
        assert e.requested_qty.value == Decimal("0")

    def test_cash_after_hand_checked(self) -> None:
        # 20_000_000 - 10_075_125 = 9_924_875
        assert self._buy_entry().cash_after_krw.value == Decimal("9924875.0000")

    def test_position_after_hand_checked(self) -> None:
        assert self._buy_entry().position_after_qty.value == Decimal("0.1")

    def test_buy_never_overspends_cash(self) -> None:
        # total_cash_debit = 10_075_125; cash below → refuse.
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(
            candle_t, Money(Decimal("10050000"))
        )
        state = LedgerState(
            cash_krw=Money(Decimal("10000000")),  # < 10_075_125
            position_qty=Qty(Decimal("0")),
        )
        with pytest.raises(InsufficientCashError):
            execute_intent(
                state, intent, _dataset([candle_t, candle_t1]),
                _snapshot(), _config(),
            )

    def test_below_min_bid_fails(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        # Request tiny notional; step floor sends order_notional under min.
        # Use a huge step so filled_qty > 0 but well below min_bid=5000.
        # notional=6000, fill_price=100_500_000, step=0.00001 → desired
        # qty = 6000/100500000 = 5.97e-5 → floor to 0.00005 → filled_qty=5e-5
        # → order_notional = 100_500_000 * 0.00005 = 5025 < 5000? no, > 5000.
        # Let's set min_bid = 6000 instead.
        snap = _snapshot(step="0.00001", min_bid="6000")
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("5000")))
        # desired qty = 5000/100500000 = ~4.97e-5 → floor(0.00001)=0.00004
        # order_notional = 100_500_000 * 0.00004 = 4020 < min_bid=6000
        with pytest.raises(BelowMinimumOrderError):
            execute_intent(
                _initial_state(),
                intent,
                _dataset([candle_t, candle_t1]),
                snap,
                _config(),
            )

    def test_notional_cap_exceeded_buy(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(
            candle_t, Money(Decimal("10050000"))
        )
        # order_notional = 10_050_000 > cap=1_000_000 → refuse.
        with pytest.raises(NotionalCapExceededError):
            execute_intent(
                _initial_state(cash="100000000"),
                intent,
                _dataset([candle_t, candle_t1]),
                _snapshot(),
                _config(max_notional_krw="1000000"),
            )

    def test_step_flooring_conservative_reduces_order(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        # requested_notional yields desired_qty = 0.10999...; floored to 0.109.
        intent = OrderIntent.buy_from_signal(
            candle_t, Money(Decimal("11054500"))  # 0.10999... at fill_price
        )
        _new_state, entry = execute_intent(
            _initial_state(),
            intent,
            _dataset([candle_t, candle_t1]),
            _snapshot(),
            _config(),
        )
        # desired = 11_054_500 / 100_500_000 = 0.109995...
        # floor(0.001) = 0.109
        assert entry.filled_qty.value == Decimal("0.109")
        # order_notional = 100_500_000 * 0.109 = 10_954_500 < requested
        assert entry.order_notional_krw.value == Decimal("10954500")
        assert entry.requested_notional_krw.value == Decimal("11054500")

    def test_tick_snap_up_conservative_for_buy(self) -> None:
        # Choose slippage that lands off-grid, verify ceil.
        candle_t = _candle(T0, price="100000000")
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT), price="100000000")
        # 100_000_000 * 1.0015 = 100_150_000 (on grid tick 1000). Use 1 bp.
        # 100_000_000 * 1.0001 = 100_010_000 (on grid). Try slippage 0.5 bps:
        # not integer bps. Use slippage that hits off-grid: 51 bps →
        # adjusted 100_510_000 (still on grid).
        # Use tick=10_000 with 50 bps: adjusted=100_500_000 → ceil→100_500_000.
        # Off-grid requires a fractional bps. Use 33 bps: adjusted=100_330_000
        # still on grid.
        # Trick: use price 100_000_001 → adjusted=100_500_001.005 → ceil=100_501_000.
        candle_t = _candle(T0, price="100000001")
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT), price="100000001")
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        _new_state, entry = execute_intent(
            _initial_state(),
            intent,
            _dataset([candle_t, candle_t1]),
            _snapshot(),
            _config(),
        )
        # 100000001 * 1.005 = 100500001.005 → ceil(tick=1000) = 100_501_000
        assert entry.fill_price.value == Decimal("100501000")


# ---------------------------------------------------------------------------
# Sell path — hand-checked ledger
# ---------------------------------------------------------------------------


class TestSellPath:
    def _sell_entry(self) -> LedgerEntry:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.sell_from_signal(candle_t, Qty(Decimal("0.1")))
        # Position starts with 0.1 BTC (from a prior buy simulation).
        state = LedgerState(
            cash_krw=Money(Decimal("9924875")), position_qty=Qty(Decimal("0.1"))
        )
        _new_state, entry = execute_intent(
            state,
            intent,
            _dataset([candle_t, candle_t1]),
            _snapshot(),
            _config(),
        )
        return entry

    def test_fill_price_snapped_down(self) -> None:
        assert self._sell_entry().fill_price.value == Decimal("99500000")

    def test_gross_proceeds_hand_checked(self) -> None:
        assert self._sell_entry().gross_proceeds_krw.value == Decimal("9950000.000")

    def test_fee_hand_checked(self) -> None:
        assert self._sell_entry().fee_krw.value == Decimal("24875.0000000")

    def test_net_proceeds_hand_checked(self) -> None:
        assert self._sell_entry().net_proceeds_krw.value == Decimal("9925125.0000000")

    def test_buy_fields_zero_on_sell(self) -> None:
        e = self._sell_entry()
        assert e.requested_notional_krw.value == Decimal("0")
        assert e.total_cash_debit_krw.value == Decimal("0")
        assert e.net_acquired_coin.value == Decimal("0")

    def test_position_after_hand_checked(self) -> None:
        assert self._sell_entry().position_after_qty.value == Decimal("0.0")

    def test_sell_never_exceeds_position(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.sell_from_signal(candle_t, Qty(Decimal("0.2")))
        state = LedgerState(
            cash_krw=Money(Decimal("0")), position_qty=Qty(Decimal("0.1"))
        )
        with pytest.raises(InsufficientPositionError):
            execute_intent(
                state, intent, _dataset([candle_t, candle_t1]),
                _snapshot(), _config(),
            )

    def test_below_min_ask_fails(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.sell_from_signal(candle_t, Qty(Decimal("0.0005")))
        state = LedgerState(
            cash_krw=Money(Decimal("0")), position_qty=Qty(Decimal("0.001"))
        )
        # filled_qty = floor(0.0005, 0.001) = 0 → BelowMinimumOrderError
        # (zero-after-floor path)
        with pytest.raises(BelowMinimumOrderError):
            execute_intent(
                state, intent, _dataset([candle_t, candle_t1]),
                _snapshot(), _config(),
            )

    def test_below_min_ask_via_min_check(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        # step=0.0001, min_ask=0.001; request 0.0005 → filled=0.0005 < min.
        snap = _snapshot(step="0.0001", min_ask="0.001")
        intent = OrderIntent.sell_from_signal(candle_t, Qty(Decimal("0.0005")))
        state = LedgerState(
            cash_krw=Money(Decimal("0")), position_qty=Qty(Decimal("0.001"))
        )
        with pytest.raises(BelowMinimumOrderError):
            execute_intent(
                state, intent, _dataset([candle_t, candle_t1]),
                snap, _config(),
            )

    def test_notional_cap_exceeded_sell(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.sell_from_signal(candle_t, Qty(Decimal("0.1")))
        state = LedgerState(
            cash_krw=Money(Decimal("0")), position_qty=Qty(Decimal("0.1"))
        )
        # gross_proceeds = 9_950_000; cap = 1_000_000 → refuse.
        with pytest.raises(NotionalCapExceededError):
            execute_intent(
                state, intent, _dataset([candle_t, candle_t1]),
                _snapshot(), _config(max_notional_krw="1000000"),
            )


# ---------------------------------------------------------------------------
# Snapshot verification-status gating
# ---------------------------------------------------------------------------


class TestFeeModelVerification:
    def _run_buy(self, snap: SnapshotV1, config: ExecutionConfig) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        execute_intent(
            _initial_state(), intent, _dataset([candle_t, candle_t1]), snap, config,
        )

    def test_provisional_without_opt_in_refused(self) -> None:
        with pytest.raises(UnverifiedFeeModelError, match="provisional"):
            self._run_buy(
                _snapshot(buy_status="provisional_documented"),
                _config(allow_provisional=False),
            )

    def test_provisional_with_opt_in_proceeds(self) -> None:
        # Should not raise.
        self._run_buy(
            _snapshot(buy_status="provisional_documented"),
            _config(allow_provisional=True),
        )

    def test_confirmed_does_not_require_opt_in(self) -> None:
        # Should not raise even with allow_provisional=False.
        self._run_buy(
            _snapshot(buy_status="confirmed_read_only"),
            _config(allow_provisional=False),
        )

    def test_unresolved_hard_refused_even_with_opt_in(self) -> None:
        with pytest.raises(UnverifiedFeeModelError):
            self._run_buy(
                _snapshot(buy_status="unresolved_until_M6B"),
                _config(allow_provisional=True),
            )


# ---------------------------------------------------------------------------
# Snapshot tick/step gating
# ---------------------------------------------------------------------------


class TestSnapshotGating:
    def test_missing_tick_refused(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        with pytest.raises(SnapshotValidationError, match="tick"):
            execute_intent(
                _initial_state(), intent, _dataset([candle_t, candle_t1]),
                _snapshot(tick=None), _config(),
            )

    def test_missing_step_refused(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        intent = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        with pytest.raises(SnapshotValidationError, match="step"):
            execute_intent(
                _initial_state(), intent, _dataset([candle_t, candle_t1]),
                _snapshot(step=None), _config(),
            )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterministicReplay:
    def test_identical_inputs_produce_identical_ledger(self) -> None:
        candle_t = _candle(T0)
        candle_t1 = _candle(T0 + timedelta(minutes=UNIT))
        candle_t2 = _candle(T0 + timedelta(minutes=2 * UNIT))
        candle_t3 = _candle(T0 + timedelta(minutes=3 * UNIT))
        dataset = _dataset([candle_t, candle_t1, candle_t2, candle_t3])
        snap = _snapshot()
        config = _config()
        # Buy on t, sell on t+2.
        intent_buy = OrderIntent.buy_from_signal(candle_t, Money(Decimal("10050000")))
        intent_sell = OrderIntent.sell_from_signal(candle_t2, Qty(Decimal("0.1")))

        def _run() -> LedgerState:
            state = _initial_state()
            state, _ = execute_intent(state, intent_buy, dataset, snap, config)
            state, _ = execute_intent(state, intent_sell, dataset, snap, config)
            return state

        run_a = _run()
        run_b = _run()
        assert run_a.cash_krw == run_b.cash_krw
        assert run_a.position_qty == run_b.position_qty
        assert run_a.entries == run_b.entries
