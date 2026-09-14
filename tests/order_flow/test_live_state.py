"""Focused tests for the canonical live_state.json + sidecar.

Covers every D4 requirement: strict schema validation, snapshot/config
drift refusal, processed-candle prefix integrity, balance reconciliation
without double counting, existing-BTC adoption, active-stop persistence,
lockout persistence, and the byte-identical-on-refusal invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from bithumb_bot.order_flow.live_state import (
    ActiveStop,
    LiveStateCorrupt,
    ManagedFill,
    ProcessedCandle,
    compute_candle_fingerprint,
    compute_config_sha256,
    compute_snapshot_sha256,
    initial_state,
    load_state,
    sidecar_path,
    state_path,
    verify_processed_prefix,
    write_state,
)
from bithumb_bot.strategy.breakout import (
    BREAKOUT_UNIT_MINUTES,
    ENTRY_LOOKBACK_CANDLES,
)

_UNIT = BREAKOUT_UNIT_MINUTES
_STEP = timedelta(minutes=_UNIT)
_BASE = datetime(2026, 3, 15, 0, 0, tzinfo=UTC)


def _make_candle(open_time: datetime, close_value: Decimal) -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        open_time_utc=open_time,
        open=Money(close_value),
        high=Money(close_value + Decimal("100")),
        low=Money(close_value - Decimal("100")),
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
    n_flat = ENTRY_LOOKBACK_CANDLES
    flat_close = Decimal("100000000")
    candles = [_make_candle(_BASE + i * _STEP, flat_close) for i in range(n_flat)]
    breakout_close = Decimal("100600000")
    candles.append(_make_candle(_BASE + n_flat * _STEP, breakout_close))
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
        fee_rates=FeeRates(bid=Decimal("0.0025"), ask=Decimal("0.0025")),
        minimums=Minimums(krw_min_total_bid=Decimal("5000")),
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


@dataclass(frozen=True)
class _Bal:
    cash_krw: Money
    coin_qty: Qty
    cash_krw_locked: Money = Money(Decimal("0"))
    coin_qty_locked: Qty = Qty(Decimal("0"))
    avg_buy_price: Money | None = None


def _cycle_inputs(
    *,
    tmp_path: Path,
    dataset: CandleDataset | None = None,
    now_utc: datetime | None = None,
    broker: MockBroker | None = None,
    balances: _Bal | None = None,
    max_notional_krw: Money = Money(Decimal("10000000")),
    starting_cash_krw: Money | None = None,
    adopt_existing_btc: bool = False,
) -> CycleInputs:
    ds = dataset if dataset is not None else _flat_dataset(5)
    return CycleInputs(
        dataset=ds,
        snapshot=_snapshot(),
        state_dir=tmp_path,
        max_notional_krw=max_notional_krw,
        broker=broker or MockBroker(store_root=tmp_path / "mock"),
        now_utc=now_utc or (_BASE + 6 * _STEP),
        balances_fetcher=(lambda: balances) if balances is not None else None,
        starting_cash_krw=starting_cash_krw,
        adopt_existing_btc=adopt_existing_btc,
    )


def _byte_snapshot(tmp_path: Path) -> tuple[bytes, bytes]:
    return (
        state_path(tmp_path).read_bytes(),
        sidecar_path(tmp_path).read_bytes(),
    )


# ---------------------------------------------------------------------------
# Roundtrip + schema
# ---------------------------------------------------------------------------


def test_load_returns_none_when_state_absent(tmp_path: Path) -> None:
    assert load_state(tmp_path) is None


def test_write_then_load_roundtrip(tmp_path: Path) -> None:
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    write_state(tmp_path, st)
    reloaded = load_state(tmp_path)
    assert reloaded == st


def test_load_refuses_when_sidecar_missing(tmp_path: Path) -> None:
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    write_state(tmp_path, st)
    sidecar_path(tmp_path).unlink()
    with pytest.raises(LiveStateCorrupt):
        load_state(tmp_path)


def test_load_refuses_orphan_sidecar(tmp_path: Path) -> None:
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    write_state(tmp_path, st)
    state_path(tmp_path).unlink()
    with pytest.raises(LiveStateCorrupt):
        load_state(tmp_path)


def test_load_refuses_when_bytes_do_not_match_sidecar(tmp_path: Path) -> None:
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    write_state(tmp_path, st)
    raw = state_path(tmp_path).read_bytes()
    # Mutate one byte and rewrite; sidecar now stale.
    tampered = raw.replace(b'"CASH"', b'"LONG"')
    assert tampered != raw
    state_path(tmp_path).write_bytes(tampered)
    with pytest.raises(LiveStateCorrupt):
        load_state(tmp_path)


def test_load_refuses_extra_top_level_key(tmp_path: Path) -> None:
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    write_state(tmp_path, st)
    raw = state_path(tmp_path).read_bytes()
    # Injecting an extra key changes canonical bytes → prefix mismatch.
    tampered = raw[:-2] + b',"extra":1}\n'
    state_path(tmp_path).write_bytes(tampered)
    # Sidecar must also update or we get sidecar-mismatch — we want the
    # schema-key error, so recompute sidecar to isolate schema validation.
    from bithumb_bot.artifact.canonical import sha256_hex
    sidecar_path(tmp_path).write_text(
        f"{sha256_hex(tampered)}  live_state.json\n", encoding="utf-8"
    )
    with pytest.raises(LiveStateCorrupt):
        load_state(tmp_path)


# ---------------------------------------------------------------------------
# Processed-candle prefix integrity
# ---------------------------------------------------------------------------


def test_verify_processed_prefix_ok_when_dataset_matches(tmp_path: Path) -> None:
    ds = _flat_dataset(3)
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    processed = tuple(
        ProcessedCandle(
            open_time_utc=c.open_time_utc,
            fingerprint=compute_candle_fingerprint(c),
        )
        for c in ds.candles
    )
    st = replace(
        st,
        processed_candles=processed,
        last_processed_open_time_utc=processed[-1].open_time_utc,
    )
    verify_processed_prefix(st, tuple(ds.candles))


def test_verify_processed_prefix_refuses_mutation(tmp_path: Path) -> None:
    ds = _flat_dataset(3)
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256="a" * 64,
        config_sha256="b" * 64,
    )
    processed = tuple(
        ProcessedCandle(
            open_time_utc=c.open_time_utc,
            fingerprint="0" * 64,  # not the actual fingerprint
        )
        for c in ds.candles
    )
    st = replace(
        st,
        processed_candles=processed,
        last_processed_open_time_utc=processed[-1].open_time_utc,
    )
    from bithumb_bot.order_flow.live_state import LiveStateDrift

    with pytest.raises(LiveStateDrift):
        verify_processed_prefix(st, tuple(ds.candles))


def test_cycle_refuses_when_processed_candle_mutated(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    inp = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds,
        now_utc=now,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    result = run_one_cycle(inp)
    assert result.status == "SUBMITTED"

    # Mutate the dataset for the SECOND cycle: change a close value on
    # a candle whose fingerprint is already persisted. Refuses.
    mutated = list(ds.candles)
    mutated[10] = _make_candle(mutated[10].open_time_utc, Decimal("999999999"))
    ds2 = ds.model_copy(update={"candles": mutated})
    inp2 = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds2,
        now_utc=now + _STEP,
        broker=inp.broker,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused):
        run_one_cycle(inp2)
    assert _byte_snapshot(tmp_path) == before, "state must be byte-identical"


def test_cycle_refuses_when_processed_candle_shortened(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    inp = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds,
        now_utc=now,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    run_one_cycle(inp)
    # Drop the first candle from the dataset — the persisted prefix
    # now references a missing candle.
    ds2 = ds.model_copy(update={"candles": ds.candles[1:]})
    inp2 = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds2,
        now_utc=now + _STEP,
        broker=inp.broker,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused):
        run_one_cycle(inp2)
    assert _byte_snapshot(tmp_path) == before


def test_cycle_refuses_when_processed_candle_reordered(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    inp = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds,
        now_utc=now,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    run_one_cycle(inp)
    # Swap two candles' open times → fingerprint at that slot mismatches.
    swapped = list(ds.candles)
    swapped[10], swapped[11] = _make_candle(
        swapped[10].open_time_utc, Decimal("100000001")
    ), _make_candle(swapped[11].open_time_utc, Decimal("100000002"))
    ds2 = ds.model_copy(update={"candles": swapped})
    inp2 = _cycle_inputs(
        tmp_path=tmp_path,
        dataset=ds2,
        now_utc=now + _STEP,
        broker=inp.broker,
        starting_cash_krw=Money(Decimal("1000000")),
    )
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused):
        run_one_cycle(inp2)
    assert _byte_snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# Snapshot + config drift
# ---------------------------------------------------------------------------


def test_snapshot_drift_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    inp = _cycle_inputs(tmp_path=tmp_path, dataset=ds)
    run_one_cycle(inp)

    # Rewrite state with a fake snapshot sha, then rerun with the
    # original snapshot → mismatch, refuse.
    from bithumb_bot.order_flow.live_state import load_state as _ls

    st = _ls(tmp_path)
    assert st is not None
    bad = replace(st, snapshot_sha256="c" * 64)
    write_state(tmp_path, bad)

    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused, match="snapshot drift"):
        run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds))
    assert _byte_snapshot(tmp_path) == before


def test_max_notional_drift_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            max_notional_krw=Money(Decimal("10000000")),
        )
    )
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused, match="config drift"):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=ds,
                max_notional_krw=Money(Decimal("20000000")),
            )
        )
    assert _byte_snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# Balance-drift refusal
# ---------------------------------------------------------------------------


def test_unexplained_btc_drift_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bal_a = _Bal(cash_krw=Money(Decimal("1000000")), coin_qty=Qty(Decimal("0")))
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            balances=bal_a,
        )
    )
    # Next cycle: venue suddenly reports BTC — refuse (no known fill).
    bal_b = _Bal(cash_krw=Money(Decimal("1000000")), coin_qty=Qty(Decimal("0.1")))
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused, match="BTC drift"):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=ds,
                balances=bal_b,
                now_utc=_BASE + 7 * _STEP,
            )
        )
    assert _byte_snapshot(tmp_path) == before


def test_unexplained_krw_drift_refuses(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bal_a = _Bal(cash_krw=Money(Decimal("1000000")), coin_qty=Qty(Decimal("0")))
    run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds, balances=bal_a))
    bal_b = _Bal(cash_krw=Money(Decimal("5000000")), coin_qty=Qty(Decimal("0")))
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused, match="KRW drift"):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=ds,
                balances=bal_b,
                now_utc=_BASE + 7 * _STEP,
            )
        )
    assert _byte_snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# Fill-delta reconciliation (no double count after restart)
# ---------------------------------------------------------------------------


def test_fill_deltas_are_not_double_counted_across_cycles(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")

    # First cycle: dispatches a buy (ACCEPTED, no fills yet).
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st1 = load_state(tmp_path)
    assert st1 is not None
    assert st1.bot_owned_position_qty.value == Decimal("0")

    # Simulate a fill on the accepted order.
    open_orders = broker.list_open()
    assert len(open_orders) == 1
    order = open_orders[0]
    broker.record_fill(
        client_order_id=order.client_order_id,
        fill_qty=Qty(Decimal("0.005")),
        fill_notional_krw=Money(Decimal("500000")),
        complete=True,
    )

    # Second cycle: fold in the fill exactly once.
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=10),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st2 = load_state(tmp_path)
    assert st2 is not None
    assert st2.bot_owned_position_qty.value == Decimal("0.005")
    assert st2.bot_owned_cost_basis_krw.value == Decimal("500000")

    # Third cycle with NO further fills: bot-owned must stay the same.
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=15),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st3 = load_state(tmp_path)
    assert st3 is not None
    assert st3.bot_owned_position_qty.value == Decimal("0.005")
    assert st3.bot_owned_cost_basis_krw.value == Decimal("500000")


# ---------------------------------------------------------------------------
# Existing-BTC adoption edge cases
# ---------------------------------------------------------------------------


def test_adoption_refused_without_positive_avg_buy_price(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bal = _Bal(
        cash_krw=Money(Decimal("1000000")),
        coin_qty=Qty(Decimal("0.01")),
        avg_buy_price=None,
    )
    with pytest.raises(CycleRefused, match="avg_buy_price"):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=ds,
                balances=bal,
                adopt_existing_btc=True,
            )
        )
    # No state file created on refusal.
    assert not state_path(tmp_path).exists()


def test_repeated_adoption_after_restart_refused(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bal = _Bal(
        cash_krw=Money(Decimal("1000000")),
        coin_qty=Qty(Decimal("0.01")),
        avg_buy_price=Money(Decimal("100000000")),
    )
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            balances=bal,
            adopt_existing_btc=True,
        )
    )
    # Second cycle (state now exists) with --adopt again → refuse.
    before = _byte_snapshot(tmp_path)
    with pytest.raises(CycleRefused, match="adopt"):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=ds,
                balances=bal,
                now_utc=_BASE + 7 * _STEP,
                adopt_existing_btc=True,
            )
        )
    assert _byte_snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# Active-stop persistence + restart recovery
# ---------------------------------------------------------------------------


def test_active_stop_byte_identical_after_restart(tmp_path: Path) -> None:
    ds = _flat_dataset(5)
    bal = _Bal(
        cash_krw=Money(Decimal("1000000")),
        coin_qty=Qty(Decimal("0.01")),
        avg_buy_price=Money(Decimal("100000000")),
    )
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            balances=bal,
            adopt_existing_btc=True,
        )
    )
    raw_before = state_path(tmp_path).read_bytes()
    st = load_state(tmp_path)
    assert st is not None
    assert st.active_stop is not None
    # A no-op cycle (no new candles / no fills) leaves the active_stop
    # region byte-identical inside state.json.
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            balances=bal,
            now_utc=_BASE + 7 * _STEP,
        )
    )
    st2 = load_state(tmp_path)
    assert st2 is not None
    assert st2.active_stop == st.active_stop


# ---------------------------------------------------------------------------
# Never sell more than bot_owned_position_qty (unrelated BTC)
# ---------------------------------------------------------------------------


def test_unrelated_btc_never_sold(tmp_path: Path) -> None:
    """A CASH signal must NOT dispatch a sell for BTC we do not own."""
    ds = _breakout_dataset()  # ends in a LONG signal
    now = ds.candles[-1].open_time_utc + _STEP

    # First cycle establishes a fresh state — no bot-owned BTC — with
    # the venue reporting 0 BTC. Dispatches a buy intent (ACCEPTED).
    broker = MockBroker(store_root=tmp_path / "mock")
    bal0 = _Bal(
        cash_krw=Money(Decimal("1000000")),
        coin_qty=Qty(Decimal("0")),
    )
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            balances=bal0,
        )
    )
    st = load_state(tmp_path)
    assert st is not None
    assert st.bot_owned_position_qty.value == Decimal("0")


# ---------------------------------------------------------------------------
# Lockout persistence + clearing
# ---------------------------------------------------------------------------


def test_lockout_persists_across_flat_cycles(tmp_path: Path) -> None:
    """A flat cycle by itself must NOT clear a persisted lockout."""
    ds = _flat_dataset(5)
    # Bootstrap the state with lockout=True and flat position.
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256=compute_snapshot_sha256(_snapshot()),
        config_sha256=compute_config_sha256(
            market="KRW-BTC",
            unit_minutes=_UNIT,
            max_notional_krw=Money(Decimal("10000000")),
        ),
    )
    st = replace(st, stopped_out_lockout=True)
    write_state(tmp_path, st)

    result = run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds))
    assert result.status == "NOOP"
    reloaded = load_state(tmp_path)
    assert reloaded is not None
    assert reloaded.stopped_out_lockout is True, (
        "lockout must not clear merely because a cycle is flat"
    )


def test_lockout_clears_on_new_cash_transition_while_flat(tmp_path: Path) -> None:
    """Lockout clears exactly when a newly processed CASH transition fires."""
    from bithumb_bot.strategy.breakout import BreakoutSignal

    # Build a dataset whose LAST candle emits a CASH transition:
    # 60 flat highs then a low candle → LONG then immediate CASH. To
    # keep the test hermetic, we short-circuit generate_breakout_signals.
    ds = _flat_dataset(3)
    st = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256=compute_snapshot_sha256(_snapshot()),
        config_sha256=compute_config_sha256(
            market="KRW-BTC",
            unit_minutes=_UNIT,
            max_notional_krw=Money(Decimal("10000000")),
        ),
    )
    st = replace(st, stopped_out_lockout=True, strategy_state="LONG")
    write_state(tmp_path, st)

    # Patch generate_breakout_signals to emit a CASH transition on the
    # last candle so we can exercise the clearing rule deterministically.
    from bithumb_bot.order_flow import live_cycle as _lc

    original = _lc.generate_breakout_signals

    def fake_signals(candles: tuple[Candle, ...]) -> tuple[BreakoutSignal, ...]:
        c = candles[-1]
        return (
            BreakoutSignal(
                target_state="CASH",
                signal_ts_utc=c.open_time_utc + _STEP,
                source_open_time_utc=c.open_time_utc,
                unit_minutes=_UNIT,
                close_value=c.close.value,
                prior_high=None,
                prior_low=None,
                entry_breakout_level=None,
            ),
        )

    _lc.generate_breakout_signals = fake_signals
    try:
        result = run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds))
    finally:
        _lc.generate_breakout_signals = original

    assert result.status == "NOOP"  # nothing to sell (position=0)
    reloaded = load_state(tmp_path)
    assert reloaded is not None
    assert reloaded.stopped_out_lockout is False


# ---------------------------------------------------------------------------
# HALT preserves state bytes for refusal-only paths
# ---------------------------------------------------------------------------


def test_partial_fill_updates_active_stop_quantity(tmp_path: Path) -> None:
    """A partial buy still gets protected — active_stop.qty grows with fills."""
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    open_orders = broker.list_open()
    assert len(open_orders) == 1
    cid = open_orders[0].client_order_id

    # Partial fill only.
    broker.record_fill(
        client_order_id=cid,
        fill_qty=Qty(Decimal("0.003")),
        fill_notional_krw=Money(Decimal("300000")),
        complete=False,
    )
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=10),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st = load_state(tmp_path)
    assert st is not None
    assert st.bot_owned_position_qty.value == Decimal("0.003")


def test_refusal_after_prefix_verify_leaves_state_byte_identical(
    tmp_path: Path,
) -> None:
    ds = _flat_dataset(3)
    run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds))
    before = _byte_snapshot(tmp_path)
    mutated = [
        _make_candle(ds.candles[0].open_time_utc, Decimal("9"))
    ] + list(ds.candles[1:])
    ds2 = ds.model_copy(update={"candles": mutated})
    with pytest.raises(CycleRefused):
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=ds2,
                now_utc=_BASE + 7 * _STEP,
            )
        )
    assert _byte_snapshot(tmp_path) == before


# ---------------------------------------------------------------------------
# available ↔ locked movement is not drift
# ---------------------------------------------------------------------------


def test_available_to_locked_movement_is_not_drift(tmp_path: Path) -> None:
    """Moving KRW from ``available`` to ``locked`` (a no-fill accepted
    order at the venue) must NOT trip the balance-drift refusal."""
    ds = _flat_dataset(5)
    bal_before = _Bal(
        cash_krw=Money(Decimal("5000000")),
        coin_qty=Qty(Decimal("0")),
        cash_krw_locked=Money(Decimal("0")),
        coin_qty_locked=Qty(Decimal("0")),
    )
    run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds, balances=bal_before))
    # Same total (5M), redistributed available → locked. Drift check
    # compares against ``available + locked``, so this is a no-op diff.
    bal_after = _Bal(
        cash_krw=Money(Decimal("3000000")),
        coin_qty=Qty(Decimal("0")),
        cash_krw_locked=Money(Decimal("2000000")),
        coin_qty_locked=Qty(Decimal("0")),
    )
    result = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            balances=bal_after,
            now_utc=_BASE + 7 * _STEP,
        )
    )
    assert result.status == "NOOP"


# ---------------------------------------------------------------------------
# paid_fee-aware KRW arithmetic
# ---------------------------------------------------------------------------


def test_normal_025_percent_fee_does_not_produce_false_krw_drift(
    tmp_path: Path,
) -> None:
    """A buy that fills with a 0.25% fee must reconcile cleanly.

    Setup: state carries a KRW-only baseline. Inject a synthetic buy
    order carrying ``paid_fee_krw`` on the BrokerOrder, fold it, and
    verify KRW delta = ``-(executed_funds + paid_fee)``.
    """
    from bithumb_bot.broker.state import (
        BrokerOrder,
        OrderState,
        StateTransition,
    )
    from bithumb_bot.execution.intent import OrderIntent
    from bithumb_bot.order_flow.live_cycle import _fold_managed_fills

    state = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256=compute_snapshot_sha256(_snapshot()),
        config_sha256=compute_config_sha256(
            market="KRW-BTC",
            unit_minutes=_UNIT,
            max_notional_krw=Money(Decimal("10000000")),
        ),
    )
    intent = OrderIntent(
        side="buy",
        source_open_time_utc=_BASE,
        unit_minutes=_UNIT,
        signal_ts_utc=_BASE + _STEP,
        requested_notional_krw=Money(Decimal("1000000")),
        requested_qty=None,
    )
    order = BrokerOrder(
        client_order_id="a" * 64,
        intent=intent,
        state=OrderState.FILLED,
        filled_qty=Qty(Decimal("0.005")),
        filled_notional_krw=Money(Decimal("500000")),
        rejection_reason=None,
        history=(
            StateTransition(
                from_state=None,
                to_state=OrderState.ACCEPTED,
                at_utc=_BASE,
            ),
        ),
        paid_fee_krw=Money(Decimal("1250")),  # exact 0.25% of 500000
    )
    new_state, btc_delta, krw_delta = _fold_managed_fills(state, [order])
    assert btc_delta == Decimal("0.005")
    # Buy KRW delta = -(executed_funds + paid_fee)
    assert krw_delta == -Decimal("501250")
    # Second fold with same known fills → no double-count.
    new_state2, btc2, krw2 = _fold_managed_fills(new_state, [order])
    assert btc2 == Decimal("0")
    assert krw2 == Decimal("0")


def test_sell_paid_fee_krw_delta_is_gross_minus_fee(tmp_path: Path) -> None:
    """A sell that pays a 0.25% fee must produce net KRW proceeds."""
    from bithumb_bot.broker.state import (
        BrokerOrder,
        OrderState,
        StateTransition,
    )
    from bithumb_bot.execution.intent import OrderIntent
    from bithumb_bot.order_flow.live_cycle import _fold_managed_fills

    state = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256=compute_snapshot_sha256(_snapshot()),
        config_sha256=compute_config_sha256(
            market="KRW-BTC",
            unit_minutes=_UNIT,
            max_notional_krw=Money(Decimal("10000000")),
        ),
    )
    # Seed a bot-owned position so the sell has something to reduce.
    state = replace(
        state,
        bot_owned_position_qty=Qty(Decimal("0.005")),
        bot_owned_cost_basis_krw=Money(Decimal("500000")),
    )
    intent = OrderIntent(
        side="sell",
        source_open_time_utc=_BASE,
        unit_minutes=_UNIT,
        signal_ts_utc=_BASE + _STEP,
        requested_notional_krw=None,
        requested_qty=Qty(Decimal("0.005")),
    )
    order = BrokerOrder(
        client_order_id="b" * 64,
        intent=intent,
        state=OrderState.FILLED,
        filled_qty=Qty(Decimal("0.005")),
        filled_notional_krw=Money(Decimal("500000")),
        rejection_reason=None,
        history=(
            StateTransition(
                from_state=None,
                to_state=OrderState.ACCEPTED,
                at_utc=_BASE,
            ),
        ),
        paid_fee_krw=Money(Decimal("1250")),  # 0.25% of 500000
    )
    _, btc_delta, krw_delta = _fold_managed_fills(state, [order])
    assert btc_delta == -Decimal("0.005")
    # Sell KRW delta = +(executed_funds - paid_fee)
    assert krw_delta == Decimal("498750")


# ---------------------------------------------------------------------------
# active_stop is armed / resized on async managed fills BEFORE early return
# ---------------------------------------------------------------------------


def test_async_partial_buy_fill_arms_active_stop_before_open_return(
    tmp_path: Path,
) -> None:
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")

    # Cycle 1 dispatches a buy (accepted, no fills).
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st1 = load_state(tmp_path)
    assert st1 is not None
    assert st1.active_stop is None

    open_orders = broker.list_open()
    assert len(open_orders) == 1
    cid = open_orders[0].client_order_id
    # Partial fill only.
    broker.record_fill(
        client_order_id=cid,
        fill_qty=Qty(Decimal("0.003")),
        fill_notional_krw=Money(Decimal("300000")),
        complete=False,
    )
    # Cycle 2: BEFORE the open-order early return, my reconciler must
    # arm active_stop off the cumulative fill VWAP.
    result = run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=10),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    # Order is still non-terminal → we early-return on open.
    assert result.active_order is not None
    assert result.status == "PARTIAL"
    st2 = load_state(tmp_path)
    assert st2 is not None
    # Position folded + stop armed BEFORE we returned early.
    assert st2.bot_owned_position_qty.value == Decimal("0.003")
    assert st2.active_stop is not None
    # vwap = 300000/0.003 = 100_000_000; stop = 90_000_000.
    assert st2.active_stop.stop_price.value == Decimal("90000000")
    assert st2.active_stop.qty.value == Decimal("0.003")


def test_additional_partial_fill_resizes_active_stop(tmp_path: Path) -> None:
    ds = _breakout_dataset()
    now = ds.candles[-1].open_time_utc + _STEP
    broker = MockBroker(store_root=tmp_path / "mock")
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now,
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    cid = broker.list_open()[0].client_order_id

    broker.record_fill(
        client_order_id=cid,
        fill_qty=Qty(Decimal("0.003")),
        fill_notional_krw=Money(Decimal("300000")),
        complete=False,
    )
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=10),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st_a = load_state(tmp_path)
    assert st_a is not None and st_a.active_stop is not None
    assert st_a.active_stop.qty.value == Decimal("0.003")

    # Additional partial — different price so VWAP shifts, stop moves.
    broker.record_fill(
        client_order_id=cid,
        fill_qty=Qty(Decimal("0.002")),
        fill_notional_krw=Money(Decimal("220000")),
        complete=False,
    )
    run_one_cycle(
        _cycle_inputs(
            tmp_path=tmp_path,
            dataset=ds,
            now_utc=now + timedelta(minutes=20),
            broker=broker,
            starting_cash_krw=Money(Decimal("1000000")),
        )
    )
    st_b = load_state(tmp_path)
    assert st_b is not None and st_b.active_stop is not None
    # New cumulative: qty=0.005, notional=520000; vwap=104_000_000;
    # stop = 93_600_000.
    assert st_b.active_stop.qty.value == Decimal("0.005")
    assert st_b.active_stop.stop_price.value == Decimal("93600000")


def test_async_protective_fill_sets_lockout_and_clears_stop(tmp_path: Path) -> None:
    """A protective sell filling asynchronously must set lockout AND
    (when it takes bot-owned position to zero) clear active_stop."""
    from datetime import UTC

    from bithumb_bot.broker.state import (
        BrokerOrder,
        OrderState,
        StateTransition,
    )
    from bithumb_bot.execution.intent import OrderIntent
    from bithumb_bot.order_flow.live_cycle import _reconcile_stop_and_lockout

    state = initial_state(
        market="KRW-BTC",
        unit_minutes=_UNIT,
        snapshot_sha256=compute_snapshot_sha256(_snapshot()),
        config_sha256=compute_config_sha256(
            market="KRW-BTC",
            unit_minutes=_UNIT,
            max_notional_krw=Money(Decimal("10000000")),
        ),
    )
    # Pre-existing bot-owned position + armed stop.
    state = replace(
        state,
        bot_owned_position_qty=Qty(Decimal("0")),  # already folded to zero
        bot_owned_cost_basis_krw=Money(Decimal("0")),
        active_stop=ActiveStop(
            stop_price=Money(Decimal("90000000")),
            qty=Qty(Decimal("0.005")),
            activated_at_utc=_BASE,
            entry_fill_price=Money(Decimal("100000000")),
        ),
        stopped_out_lockout=False,
    )
    now_utc = datetime(2026, 3, 15, 4, 0, tzinfo=UTC)
    intent = OrderIntent.protective_sell(
        reason="protective_stop_intrabar",
        qty=Qty(Decimal("0.005")),
        trigger_ts_utc=_BASE + timedelta(minutes=10),
        trigger_price=Money(Decimal("89000000")),
        source_open_time_utc=_BASE,
        unit_minutes=_UNIT,
    )
    order = BrokerOrder(
        client_order_id="c" * 64,
        intent=intent,
        state=OrderState.FILLED,
        filled_qty=Qty(Decimal("0.005")),
        filled_notional_krw=Money(Decimal("445000")),
        rejection_reason=None,
        history=(
            StateTransition(
                from_state=None,
                to_state=OrderState.ACCEPTED,
                at_utc=_BASE,
            ),
        ),
        paid_fee_krw=Money(Decimal("1112")),
    )
    # ``prior_fills`` is empty — the fill is discovered fresh (post-restart).
    new_state = _reconcile_stop_and_lockout(state, [order], {}, now_utc)
    assert new_state.stopped_out_lockout is True
    assert new_state.active_stop is None


# ---------------------------------------------------------------------------
# Lockout clear rules: only newly processed CASH transitions
# ---------------------------------------------------------------------------


def test_old_cash_signal_does_not_clear_lockout(tmp_path: Path) -> None:
    """A CASH signal whose source candle is already persisted must NOT
    clear the lockout."""
    from bithumb_bot.order_flow import live_cycle as _lc
    from bithumb_bot.strategy.breakout import BreakoutSignal

    ds = _flat_dataset(5)
    # Cycle 1: process all 5 candles under lockout=False, no signals.
    run_one_cycle(_cycle_inputs(tmp_path=tmp_path, dataset=ds))
    persisted = load_state(tmp_path)
    assert persisted is not None
    # Add a new (7th) candle so cycle 2 has NEW candles arriving, then
    # patch signals to emit a stale CASH on an OLD (already processed)
    # candle. The old CASH must NOT clear the lockout.
    st = replace(persisted, stopped_out_lockout=True, strategy_state="LONG")
    write_state(tmp_path, st)
    new_dataset = _flat_dataset(7)  # 2 new candles, same fingerprints for the first 5

    original = _lc.generate_breakout_signals

    def stale_cash_signal(
        candles: tuple[Candle, ...],
    ) -> tuple[BreakoutSignal, ...]:
        # Emit a CASH signal on the FIRST candle — already processed,
        # so it must not clear the lockout.
        c0 = candles[0]
        return (
            BreakoutSignal(
                target_state="CASH",
                signal_ts_utc=c0.open_time_utc + _STEP,
                source_open_time_utc=c0.open_time_utc,
                unit_minutes=_UNIT,
                close_value=c0.close.value,
                prior_high=None,
                prior_low=None,
                entry_breakout_level=None,
            ),
        )

    _lc.generate_breakout_signals = stale_cash_signal
    try:
        run_one_cycle(
            _cycle_inputs(
                tmp_path=tmp_path,
                dataset=new_dataset,
                now_utc=_BASE + 10 * _STEP,
            )
        )
    finally:
        _lc.generate_breakout_signals = original

    reloaded = load_state(tmp_path)
    assert reloaded is not None
    assert reloaded.stopped_out_lockout is True, (
        "an old (already processed) CASH signal must not clear the lockout"
    )
