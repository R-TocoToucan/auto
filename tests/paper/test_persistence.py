"""Restart-safety and fail-closed-fault tests for the paper runner.

Covers hard-requirement item #4 (restart produces identical state and
no duplicate fills) and item #5 (dataset faults fail closed: gap,
duplicate, incomplete).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session
from bithumb_bot.paper.state import load_state

from .conftest import STEP, T0, flat, make_backtest_config, make_dataset, make_snapshot


class TestRestartProducesIdenticalStateAndNoDuplicateFills:
    def test_second_run_is_a_no_op_third_run_only_appends(
        self, tmp_path: Path, paper_fixture: tuple[CandleDataset, object, object]
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP

        r1 = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        assert r1.invalid_reason is None
        assert r1.resumed is False
        assert r1.new_fills_this_invocation == 1

        state_bytes_1 = (tmp_path / "state.json").read_bytes()
        fills_bytes_1 = (tmp_path / "fills.jsonl").read_bytes()
        signals_path = tmp_path / "signals.jsonl"
        signals_bytes_1 = signals_path.read_bytes() if signals_path.is_file() else b""
        fingerprints_path = tmp_path / "candle_fingerprints.jsonl"
        assert fingerprints_path.is_file()
        fingerprints_bytes_1 = fingerprints_path.read_bytes()
        fingerprints_lines_1 = fingerprints_bytes_1.decode("utf-8").splitlines()
        assert len(fingerprints_lines_1) == r1.forward_candle_count

        # ---- second run: identical dataset -> pure no-op --------------
        r2 = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        assert r2.invalid_reason is None
        assert r2.resumed is True
        assert r2.new_fills_this_invocation == 0

        assert (tmp_path / "state.json").read_bytes() == state_bytes_1
        assert (tmp_path / "fills.jsonl").read_bytes() == fills_bytes_1
        assert (
            signals_path.read_bytes() if signals_path.is_file() else b""
        ) == signals_bytes_1
        assert fingerprints_path.read_bytes() == fingerprints_bytes_1

        # ---- third run: append-extend the dataset by 5 flat candles --
        extended_candles = list(dataset.candles) + [
            flat(len(dataset.candles) + i, "200000000") for i in range(5)
        ]
        extended_dataset = make_dataset(extended_candles)
        now3 = extended_dataset.candles[-1].open_time_utc + STEP

        r3 = run_paper_session(extended_dataset, snapshot, config, tmp_path, now_utc=now3)
        assert r3.invalid_reason is None
        assert r3.resumed is True

        state3 = load_state(tmp_path)
        assert state3 is not None
        assert state3.forward_candle_count == r1.forward_candle_count + 5

        fills_bytes_3 = (tmp_path / "fills.jsonl").read_bytes()
        # No new signal fires on flat continuation candles -> the fills
        # ledger is unchanged (still exactly the prior bytes).
        assert fills_bytes_3 == fills_bytes_1

        # candle_fingerprints.jsonl grows by exactly the 5 newly appended
        # forward candles; the prior prefix stays byte-identical (D2).
        fingerprints_lines_3 = (
            fingerprints_path.read_text(encoding="utf-8").splitlines()
        )
        assert len(fingerprints_lines_3) == len(fingerprints_lines_1) + 5
        assert fingerprints_lines_3[: len(fingerprints_lines_1)] == fingerprints_lines_1


class TestDatasetFaultsFailClosed:
    def test_internal_gap_refuses_and_writes_nothing(self, tmp_path: Path) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))
        # Skip the slot at WARMUP_CANDLE_COUNT + 1 entirely — the next
        # candle's open_time_utc jumps by 2 * STEP instead of 1 * STEP.
        gap_open = T0 + STEP * (WARMUP_CANDLE_COUNT + 2)
        candles.append(
            Candle(
                market="KRW-BTC",
                unit_minutes=240,
                open_time_utc=gap_open,
                open="100000000",
                high="100000000",
                low="100000000",
                close="100000000",
                volume="1",
                quote_volume="100000000",
            )
        )
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()
        now = dataset.candles[-1].open_time_utc + STEP

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.refusal_code == "InternalCandleGapError"
        assert not (tmp_path / "state.json").exists()
        assert not (tmp_path / "fills.jsonl").exists()

    def test_duplicate_open_time_rejected_at_dataset_construction(self) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        # Two candles sharing the SAME open_time_utc -> CandleDataset's
        # own ascending-strict validator raises BEFORE run_paper_session
        # is ever reached.
        candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))
        candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))  # duplicate
        with pytest.raises(ValidationError, match="strictly ascending"):
            make_dataset(candles)

    def test_incomplete_final_candle_refuses_and_state_dir_stays_empty(
        self, tmp_path: Path
    ) -> None:
        candles = [flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset = make_dataset(candles)
        snapshot = make_snapshot()
        config = make_backtest_config()

        # Close boundary of the final candle has not passed yet.
        now = dataset.candles[-1].open_time_utc

        result = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert result.refusal_code == "IncompleteCandleError"
        assert not (tmp_path / "state.json").exists()
