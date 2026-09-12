"""Adversarial tests for the D1 warmup-isolation fix (D-erv, tightened
D-no0 via the reduced-dataset approach).

Covers must_have truths #1 (paper-start portfolio is unambiguously
fresh), #2 (the first fills.jsonl entry, when one exists, is always a
BUY), and #3 (the forward-visible ledger at ``paper_start_ts_utc`` is
computed from a REDUCED dataset whose SMA starts fresh — the strategy's
internal ``current_state`` is CASH at that point by construction, never
by re-derivation against a full-history signal stream).
"""

from __future__ import annotations

from pathlib import Path

from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session
from bithumb_bot.paper.state import load_state, read_jsonl

from .conftest import STEP, flat, make_backtest_config, make_candle, make_dataset, make_snapshot


class TestForwardOnlyStartsFromCash:
    """must_have truth #3 (D-no0 revision).

    The erv-era swap-equivalence property ("warmup shape never affects
    the forward ledger") was itself an artifact of the deleted
    full-history re-derivation mechanism. Under the correct
    reduced-dataset fix (see ``paper/runner.py``'s module docstring),
    the warmup SLICE is part of the SMA the reduced dataset computes at
    ``paper_start_ts_utc`` — so warmup shape legitimately changes the
    forward-visible ledger. These two fixtures share the IDENTICAL
    forward candles; only their warmup shape differs, and the divergent
    (not equal) outcome below is the exact expected behaviour, computed
    by hand from the reduced-dataset SMA rule.
    """

    def test_warmup_ending_with_spike_transitions_at_forward_from_fresh_cash(
        self, tmp_path: Path
    ) -> None:
        # Warmup: 1199 flat candles at 100M, then a spike to 200M at the
        # LAST warmup candle (index 1199). The reduced dataset
        # (candles[1:]) computes its SMA over [100M]*1198 + [200M, 200M]:
        # running_sum = 100M*1198 + 200M + 200M = 120_200_000_000;
        # scaled_close = 200M * 1200 * 10000 is far larger than
        # running_sum * 10075 — CASH -> LONG fires exactly at the
        # forward candle (paper_start).
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
        candles.append(flat(WARMUP_CANDLE_COUNT + 1, "200000000"))
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert len(result.forward_entries) == 1
        buy = result.forward_entries[0]
        assert buy.side == "buy"
        assert buy.source_open_time_utc == candles[WARMUP_CANDLE_COUNT].open_time_utc
        assert buy.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 1].open_time_utc

    def test_warmup_flat_at_terminal_price_stays_flat(self, tmp_path: Path) -> None:
        # SAME forward candles as above, but the warmup is flat at 200M
        # the WHOLE WAY THROUGH — no warmup transition could possibly
        # have fired (close == running SMA exactly at every warmup
        # index). The reduced dataset's SMA is 200M exactly (every close
        # in the window is 200M): scaled_close = 200M*1200*10000 ==
        # running_sum*10000 < running_sum*10075 — CASH is retained, no
        # signal ever fires.
        candles = [flat(i, "200000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(flat(WARMUP_CANDLE_COUNT, "200000000"))
        candles.append(flat(WARMUP_CANDLE_COUNT + 1, "200000000"))
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.invalid_reason is None
        assert result.forward_entries == ()


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
        # The reduced dataset's own SMA computation at paper_start
        # produces a fresh LONG transition from the CASH baseline, so
        # fills.jsonl's FIRST recorded line must be that forward buy,
        # never a bare sell of a phantom warmup position.
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
