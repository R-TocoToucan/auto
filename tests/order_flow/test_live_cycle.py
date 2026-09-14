"""Focused tests for the one-cycle live-trading coordinator.

Every test uses `MockBroker` (dry-run path) plus synthetic candle
datasets. The live-broker (LiveBithumbBroker) is exercised separately
in tests/broker/test_live_broker.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bithumb_bot.bithumb_spec.snapshot import (
    FeeRates,
    Minimums,
    SnapshotV1,
)
from bithumb_bot.broker.mock import MockBroker
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.order_flow.live_cycle import (
    CycleInputs,
    CycleRefused,
    run_one_cycle,
)
from bithumb_bot.strategy.breakout import (
    BREAKOUT_UNIT_MINUTES,
    ENTRY_LOOKBACK_CANDLES,
)

_UNIT = BREAKOUT_UNIT_MINUTES
_STEP = timedelta(minutes=_UNIT)
_BASE = datetime(2026, 3, 15, 0, 0, tzinfo=UTC)


def _make_candle(
    open_time: datetime, close_value: Decimal, high: Decimal | None = None
) -> Candle:
    high_val = high if high is not None else max(close_value, Decimal("100000000"))
    return Candle(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        open_time_utc=open_time,
        open=Money(close_value),
        high=Money(high_val),
        low=Money(close_value - Decimal("100000")),
        close=Money(close_value),
        volume=Qty(Decimal("1")),
        quote_volume=Money(close_value),
    )


def _flat_dataset(n: int, close_value: Decimal = Decimal("100000000")) -> CandleDataset:
    candles = [
        _make_candle(_BASE + i * _STEP, close_value) for i in range(n)
    ]
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=_UNIT,
        requested_start_utc=_BASE.isoformat(),
        requested_end_utc=(_BASE + n * _STEP).isoformat(),
        fetched_at_utc=(_BASE + n * _STEP).isoformat(),
        candles=candles,
        missing_intervals_utc=[],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=["2026-03-15T09:00:00"],
            effective_end_utc=(_BASE + n * _STEP).isoformat(),
        ),
    )


def _breakout_dataset() -> CandleDataset:
    """121 candles: 120 flat + 1 that breaks above the flat by more than 50 bps."""
    n_flat = ENTRY_LOOKBACK_CANDLES
    flat_close = Decimal("100000000")
    candles = [_make_candle(_BASE + i * _STEP, flat_close) for i in range(n_flat)]
    # Breakout candle: close > flat * (10000 + 50) / 10000 = flat * 1.005
    breakout_close = Decimal("100600000")  # > 100_500_000
    candles.append(
        _make_candle(
            _BASE + n_flat * _STEP,
            breakout_close,
            high=breakout_close,
        )
    )
    end = _BASE + (n_flat + 1) * _STEP
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=_UNIT,
        requested_start_utc=_BASE.isoformat(),
        requested_end_utc=end.isoformat(),
        fetched_at_utc=end.isoformat(),
        candles=candles,
        missing_intervals_utc=[],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=["2026-03-15T09:00:00"],
            effective_end_utc=end.isoformat(),
        ),
    )


def _snapshot() -> SnapshotV1:
    return SnapshotV1(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        retrieved_at_utc="2026-03-15T00:00:00Z",
        source_endpoints=["/v1/orders/chance"],
        fee_rates=FeeRates(
            bid=Decimal("0.0025"),
            ask=Decimal("0.0025"),
        ),
        minimums=Minimums(
            krw_min_total_bid=Decimal("5000"),
        ),
        price_tick_rules={"default_tick": Decimal("1000")},
        quantity_step_rules={},
        supported_order_types=["limit", "price", "market"],
        verification_status={
            "general_fee_rate": "confirmed_read_only",
            "market_buy_fee_reservation": "provisional_documented",
            "rounding_rejection_behavior": "unresolved_until_M6B",
            "live_order_acceptance": "unresolved_until_M6B",
        },
        source_fixture_hashes=[],
    )


def _cycle_inputs(
    *,
    tmp_path: Path,
    dataset: CandleDataset,
    now_utc: datetime,
    broker: MockBroker | None = None,
    starting_cash_krw: Money | None = None,
    balances_fetcher: Any = None,
) -> CycleInputs:
    return CycleInputs(
        dataset=dataset,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=Money(Decimal("10000000")),
        broker=broker or MockBroker(store_root=tmp_path / "mock"),
        now_utc=now_utc,
        starting_cash_krw=starting_cash_krw,
        balances_fetcher=balances_fetcher,
    )


# ---------------------------------------------------------------------------
# HALT + basic NOOP paths
# ---------------------------------------------------------------------------


def test_halt_file_forces_halted_and_submits_nothing(tmp_path: Path) -> None:
    (tmp_path / "HALT").write_text("stop")
    ds = _flat_dataset(5)
    result = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path, dataset=ds, now_utc=_BASE + 6 * _STEP
        )
    )
    assert result.status == "HALTED"
    assert result.submitted_order is None


def test_no_completed_candles_returns_noop(tmp_path: Path) -> None:
    ds = _flat_dataset(1)
    result = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=_BASE + timedelta(minutes=10),  # before first close
        )
    )
    assert result.status == "NOOP"
    assert result.submitted_order is None


def test_incomplete_final_candle_is_not_evaluated(tmp_path: Path) -> None:
    """Completed = open_time + unit <= now. In-progress candles are ignored."""
    ds = _breakout_dataset()
    last_open = ds.candles[-1].open_time_utc
    result = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            # Set now so the breakout candle is IN PROGRESS.
            now_utc=last_open + timedelta(minutes=60),
        )
    )
    assert result.status == "NOOP"


# ---------------------------------------------------------------------------
# Breakout signal → dispatch (dry-run, no balances fetcher)
# ---------------------------------------------------------------------------


def test_breakout_signal_dispatches_one_order(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    last_open = ds.candles[-1].open_time_utc
    # Fully completed breakout candle → signal fires.
    now = last_open + _STEP
    inputs = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds,
        now_utc=now,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    result = run_one_cycle(inputs)
    assert result.status == "SUBMITTED"
    assert result.submitted_order is not None
    assert result.submitted_order.intent.side == "buy"


def test_second_cycle_does_not_duplicate_order(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    last_open = ds.candles[-1].open_time_utc
    now = last_open + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")
    first = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    assert first.submitted_order is not None
    # Second cycle should see the accepted order and report OPEN.
    second = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=30),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    assert second.status == "SUBMITTED"  # accepted state maps to SUBMITTED
    assert second.submitted_order is None  # nothing new was submitted
    files = sorted((tmp_path / "mock" / "orders").glob("*.json"))
    assert len(files) == 1


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_dataset_wrong_market_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bad = ds.model_copy(update={"market": "KRW-ETH"})
    with pytest.raises(CycleRefused):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path, dataset=bad, now_utc=_BASE + 6 * _STEP
            )
        )


def test_dataset_wrong_unit_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bad = ds.model_copy(update={"unit_minutes": 60})
    with pytest.raises(CycleRefused):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path, dataset=bad, now_utc=_BASE + 6 * _STEP
            )
        )


def test_naive_now_utc_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    naive = datetime(2026, 3, 16, 0, 0)  # no tz
    with pytest.raises(CycleRefused):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path, dataset=ds, now_utc=naive  # type: ignore[arg-type]
            )
        )


def test_max_notional_zero_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    inputs = CycleInputs(
        dataset=ds,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=Money(Decimal("0")),
        broker=MockBroker(store_root=tmp_path / "mock"),
        now_utc=_BASE + 6 * _STEP,
    )
    with pytest.raises(CycleRefused):
        run_one_cycle(inputs)


# ---------------------------------------------------------------------------
# Unmanaged open order refusal
# ---------------------------------------------------------------------------


def test_unmanaged_open_venue_order_refuses(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    last_open = ds.candles[-1].open_time_utc
    now = last_open + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")
    inputs = CycleInputs(
        dataset=ds,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=Money(Decimal("1000000")),
        broker=broker,
        now_utc=now,
        starting_cash_krw=Money(Decimal("1000000")),
        venue_open_lister=lambda: [
            {"client_order_id": "unmanaged-xyz", "state": "wait"}
        ],
    )
    with pytest.raises(CycleRefused) as exc_info:
        run_one_cycle(inputs)
    assert "unmanaged" in str(exc_info.value).lower()


def test_unmanaged_watch_order_refuses(tmp_path: Path) -> None:
    """Bithumb ``state=watch`` (stop-limit trigger waiting) is also
    an open order — an unmanaged watch row must refuse."""
    ds = _breakout_dataset()
    last_open = ds.candles[-1].open_time_utc
    now = last_open + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")
    inputs = CycleInputs(
        dataset=ds,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=Money(Decimal("1000000")),
        broker=broker,
        now_utc=now,
        starting_cash_krw=Money(Decimal("1000000")),
        venue_open_lister=lambda: [
            {"client_order_id": "unmanaged-watch", "state": "watch"}
        ],
    )
    with pytest.raises(CycleRefused):
        run_one_cycle(inputs)


def test_first_startup_with_btc_and_no_managed_state_refuses_without_adopt(
    tmp_path: Path,
) -> None:
    """No managed history + nonzero venue BTC + no --adopt flag → refuse."""
    from bithumb_bot.broker.live import VenueBalances

    ds = _flat_dataset(5)

    def balances() -> VenueBalances:
        return VenueBalances(
            cash_krw=Money(Decimal("1000000")),
            coin_qty=Qty(Decimal("0.01")),
        )

    broker = MockBroker(store_root=tmp_path / "mock")
    inputs = CycleInputs(
        dataset=ds,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=Money(Decimal("1000000")),
        broker=broker,
        now_utc=_BASE + 6 * _STEP,
        balances_fetcher=balances,
    )
    with pytest.raises(CycleRefused) as exc_info:
        run_one_cycle(inputs)
    assert "adopt-existing-btc" in str(exc_info.value).lower()


def test_first_startup_adopt_existing_btc_allows_run(tmp_path: Path) -> None:
    from bithumb_bot.broker.live import VenueBalances

    ds = _flat_dataset(5)

    def balances() -> VenueBalances:
        return VenueBalances(
            cash_krw=Money(Decimal("1000000")),
            coin_qty=Qty(Decimal("0.01")),
            avg_buy_price=Money(Decimal("100000000")),
        )

    broker = MockBroker(store_root=tmp_path / "mock")
    inputs = CycleInputs(
        dataset=ds,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=Money(Decimal("1000000")),
        broker=broker,
        now_utc=_BASE + 6 * _STEP,
        balances_fetcher=balances,
        adopt_existing_btc=True,
    )
    result = run_one_cycle(inputs)
    assert result.status == "NOOP"

    # Adoption persisted the position + active stop + adoption flag.
    from bithumb_bot.order_flow.live_state import load_state

    persisted = load_state(tmp_path)
    assert persisted is not None
    assert persisted.adopted_existing_btc is True
    assert persisted.strategy_state == "LONG"
    assert persisted.bot_owned_position_qty.value == Decimal("0.01")
    assert persisted.bot_owned_cost_basis_krw.value == Decimal("1000000")
    assert persisted.active_stop is not None
    assert persisted.active_stop.stop_price.value == Decimal("90000000")
    assert persisted.active_stop.qty.value == Decimal("0.01")


def test_halt_reconciles_but_never_submits(tmp_path: Path) -> None:
    """HALT must still surface an accurate persisted view of the venue —
    reconcile FIRST, then report HALTED and submit nothing."""
    (tmp_path / "HALT").write_text("stop")
    ds = _breakout_dataset()
    last_open = ds.candles[-1].open_time_utc
    now = last_open + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")
    result = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    assert result.status == "HALTED"
    assert result.submitted_order is None
    # Nothing in the MockBroker order store — no new submission occurred.
    files = list((tmp_path / "mock" / "orders").glob("*.json"))
    assert files == []


# ---------------------------------------------------------------------------
# Cycle log
# ---------------------------------------------------------------------------


def test_cycle_events_jsonl_is_appended(tmp_path: Path) -> None:
    ds = _flat_dataset(1)
    inputs = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds,
        now_utc=_BASE + timedelta(minutes=10),
    )
    run_one_cycle(inputs)
    log = tmp_path / "cycle_events.jsonl"
    assert log.is_file()
    lines = log.read_text().splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# Existing breakout constants are unchanged
# ---------------------------------------------------------------------------


def test_breakout_constants_are_byte_for_byte_unchanged() -> None:
    from bithumb_bot.strategy import breakout as strat

    assert strat.ENTRY_LOOKBACK_CANDLES == 120
    assert strat.EXIT_LOOKBACK_CANDLES == 60
    assert strat.ENTRY_BUFFER_BPS == Decimal("50")
    assert strat.BREAKOUT_MARKET == "KRW-BTC"
    assert strat.BREAKOUT_UNIT_MINUTES == 240
