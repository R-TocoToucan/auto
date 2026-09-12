"""Parity between `run_backtest` and `run_paper_session` — hard-requirement #6.

Demonstrates the reuse contract is real, not aspirational: given the
SAME `(dataset, snapshot, config)`, the paper runner's `forward_entries`
must be byte-identical to the backtest engine's own entries filtered
to the forward window, and the terminal cash/position/pending-intent/
stop/lockout state must agree exactly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bithumb_bot.backtest.config import BacktestConfig
from bithumb_bot.backtest.runner import run_backtest
from bithumb_bot.core.money import Money
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session

from .conftest import (
    STEP,
    make_candle,
    make_dataset,
    make_snapshot,
    make_strategy_config,
    warmup_candles,
)


class TestParityWithBacktestEngineDeterministic:
    def test_forward_entries_match_filtered_backtest_entries(
        self,
        tmp_path: Path,
        paper_fixture: tuple[object, object, object],
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP

        expected = run_backtest(dataset, snapshot, config)
        actual = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert actual.invalid_reason is None
        assert expected.invalid_reason is None
        assert actual.paper_start_ts_utc is not None

        expected_forward = tuple(
            e
            for e in expected.entries
            if e.source_open_time_utc >= actual.paper_start_ts_utc
        )
        assert actual.forward_entries == expected_forward

        assert actual.backtest_result is not None
        assert actual.backtest_result.final_cash_krw == expected.final_cash_krw
        assert (
            actual.backtest_result.final_position_qty == expected.final_position_qty
        )
        assert actual.backtest_result.pending_intent == expected.pending_intent
        assert (
            actual.backtest_result.active_protective_stop
            == expected.active_protective_stop
        )
        assert (
            actual.backtest_result.stopped_out_lockout
            == expected.stopped_out_lockout
        )


# ---------------------------------------------------------------------------
# Property-based demonstration: for ANY small forward tail appended to the
# fixed 1200-candle warmup, run_paper_session's forward_entries are exactly
# run_backtest's own entries filtered to the forward window. This is the
# operational proof that "reuse" is real — a divergent reimplementation
# would eventually disagree with the shared engine on some random tail.
# ---------------------------------------------------------------------------


@st.composite
def _ohlc_tail_candle(draw: st.DrawFn, idx: int) -> object:
    a = draw(st.integers(min_value=90, max_value=110))
    b = draw(st.integers(min_value=90, max_value=110))
    pad_low = draw(st.integers(min_value=0, max_value=5))
    pad_high = draw(st.integers(min_value=0, max_value=5))
    low = min(a, b) - pad_low
    high = max(a, b) + pad_high
    return make_candle(idx, str(a), str(high), str(low), str(b))


@st.composite
def _tail(draw: st.DrawFn) -> list[object]:
    n = draw(st.integers(min_value=1, max_value=10))
    return [draw(_ohlc_tail_candle(WARMUP_CANDLE_COUNT + i)) for i in range(n)]


@given(tail=_tail())
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_parity_property_random_forward_tail(
    tail: list[object], tmp_path_factory: pytest.TempPathFactory
) -> None:
    candles = warmup_candles(price="100") + tail
    dataset = make_dataset(candles)
    snapshot = make_snapshot()

    config = BacktestConfig(
        starting_cash_krw=Money(Decimal("20000000")),
        target_sleeve_fraction=Decimal("1.0"),
        protective_stop_fraction=Decimal("0.10"),
        strategy=make_strategy_config(),
        execution=ExecutionConfig(
            slippage_bps_per_side=Decimal("50"),
            max_notional_krw=Money(Decimal("100000000")),
            allow_provisional_fee_model=False,
        ),
    )
    now = dataset.candles[-1].open_time_utc + STEP
    state_dir = tmp_path_factory.mktemp("parity")

    expected = run_backtest(dataset, snapshot, config)
    actual = run_paper_session(dataset, snapshot, config, state_dir, now_utc=now)

    if expected.invalid_reason is not None:
        # Both paths share the SAME run_backtest call under the hood;
        # a domain refusal on the full dataset must surface identically.
        assert actual.refusal_code == expected.refusal_code
        return

    assert actual.invalid_reason is None
    assert actual.paper_start_ts_utc is not None
    expected_forward = tuple(
        e
        for e in expected.entries
        if e.source_open_time_utc >= actual.paper_start_ts_utc
    )
    assert actual.forward_entries == expected_forward
