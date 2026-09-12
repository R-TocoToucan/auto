"""Adversarial tests for the D-erv D1 warmup-isolation fix.

Covers must_have truths #1 (paper-start portfolio is unambiguously
fresh), #2 (the first fills.jsonl entry, when one exists, is always a
BUY), and #3 (a warmup slice's transition-emitting SHAPE produces zero
forward portfolio effect — demonstrated via a swap-equivalence
fixture).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session
from bithumb_bot.paper.state import load_state, read_jsonl

from .conftest import STEP, flat, make_backtest_config, make_candle, make_dataset, make_snapshot


class TestWarmupIsolationSwapEquivalent:
    """must_have truth #3.

    Two datasets sharing the SAME single forward candle:

    * ``A`` — warmup ends with a spike at the last warmup candle
      (index 1199) that DOES emit a real ``LONG`` transition from
      :func:`~bithumb_bot.strategy.generate_signals`.
    * ``B`` — warmup is flat at the spike's price the whole way
      through (never emits any signal at all).

    The shared forward candle's extreme drop is calibrated so that,
    for A, the SAME first-forward iteration that
    ``_forward_only_signals`` walks consumes BOTH the warmup-sourced
    ``LONG@1199`` signal AND a genuine ``CASH`` reversal fired by
    ``generate_signals`` at the forward candle itself (source_open_time
    == the forward candle's own open) — netting true_target back to
    ``CASH`` before any transition is ever re-emitted. For B, no
    signal ever fires, so true_target is ``CASH`` throughout too. Both
    scenarios therefore reduce to an EMPTY forward-only stream — proving
    the warmup slice's transition-emitting shape (whether it fires a
    signal at all) has zero effect on the forward-visible ledger, not
    merely that its *value* happens to match after the fact.
    """

    def test_swap_equivalent_warmups_yield_identical_forward_ledger(
        self,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        p = "100000000"
        spike = "10000000000"

        warmup_a = [flat(i, p) for i in range(WARMUP_CANDLE_COUNT - 1)]
        warmup_a.append(make_candle(WARMUP_CANDLE_COUNT - 1, p, spike, p, spike))
        forward_candle = make_candle(WARMUP_CANDLE_COUNT, spike, spike, "1", "1")
        candles_a = [*warmup_a, forward_candle]

        warmup_b = [flat(i, spike) for i in range(WARMUP_CANDLE_COUNT)]
        candles_b = [*warmup_b, forward_candle]

        dataset_a = make_dataset(candles_a)
        dataset_b = make_dataset(candles_b)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = forward_candle.open_time_utc + STEP

        state_dir_a = tmp_path_factory.mktemp("swap_a")
        state_dir_b = tmp_path_factory.mktemp("swap_b")

        result_a = run_paper_session(dataset_a, snapshot, config, state_dir_a, now_utc=now)
        result_b = run_paper_session(dataset_b, snapshot, config, state_dir_b, now_utc=now)

        assert result_a.invalid_reason is None
        assert result_b.invalid_reason is None
        assert result_a.forward_entries == result_b.forward_entries

        assert result_a.backtest_result is not None
        assert result_b.backtest_result is not None
        assert (
            result_a.backtest_result.final_cash_krw
            == result_b.backtest_result.final_cash_krw
        )
        assert (
            result_a.backtest_result.final_position_qty
            == result_b.backtest_result.final_position_qty
        )


class TestPaperStartInvariants:
    """must_have truth #1."""

    def test_all_flat_dataset_yields_clean_paper_start_fields(
        self,
        tmp_path: Path,
        flat_warmup_and_forward_dataset: tuple[object, object, object],
    ) -> None:
        dataset, snapshot, config = flat_warmup_and_forward_dataset
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert result.forward_entries == ()

        state = load_state(tmp_path)
        assert state is not None
        assert state.paper_start_cash_krw == format(config.starting_cash_krw.value, "f")
        assert state.paper_start_position_qty == "0"
        assert state.paper_start_realized_pnl_krw == "0"
        assert state.paper_start_cumulative_fees_krw == "0"
        assert state.final_cash_krw == state.paper_start_cash_krw
        assert state.final_position_qty == "0"


class TestFirstFillIsBuy:
    """must_have truth #2."""

    def test_forward_long_then_cash_writes_buy_then_sell_to_fills_jsonl(
        self, tmp_path: Path
    ) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT, "100000000", "200000000", "100000000", "200000000"
            )
        )
        candles.append(flat(WARMUP_CANDLE_COUNT + 1, "200000000"))
        candles.append(
            make_candle(WARMUP_CANDLE_COUNT + 2, "200000000", "200000000", "1", "1")
        )
        candles.append(flat(WARMUP_CANDLE_COUNT + 3, "1"))
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        fills = read_jsonl(tmp_path / "fills.jsonl")
        assert len(fills) == 2
        assert fills[0]["side"] == "buy"
        assert fills[1]["side"] == "sell"

    def test_warmup_inherited_long_never_writes_a_leading_sell(
        self, tmp_path: Path
    ) -> None:
        # A warmup-only spike (would-be LONG signal at the very last
        # warmup candle) followed by a genuine forward CASH reversal.
        # fills.jsonl's FIRST recorded line must be the synthesized
        # forward buy, never a bare sell of a phantom warmup position.
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT - 1)]
        candles.append(
            make_candle(
                WARMUP_CANDLE_COUNT - 1,
                "100000000",
                "200000000",
                "100000000",
                "200000000",
            )
        )
        candles.append(flat(WARMUP_CANDLE_COUNT, "200000000"))
        candles.append(
            make_candle(WARMUP_CANDLE_COUNT + 1, "200000000", "200000000", "1", "1")
        )
        candles.append(flat(WARMUP_CANDLE_COUNT + 2, "1"))
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        fills = read_jsonl(tmp_path / "fills.jsonl")
        assert len(fills) >= 1
        assert fills[0]["side"] == "buy"
