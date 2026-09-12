"""D-no0 D3 regression: dataset/config market and unit_minutes
mismatch is rejected upfront with PaperInputContractMismatchError
BEFORE any state-file mutation. Includes parity with run_backtest
(which raises ValueError on the same mismatch, per its own
pre-flight)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bithumb_bot.backtest.runner import run_backtest
from bithumb_bot.errors import PaperInputContractMismatchError
from bithumb_bot.market_data.dataset import CandleDataset
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT, run_paper_session

from .conftest import (
    STEP,
    flat,
    make_backtest_config_with_strategy,
    make_dataset,
    make_snapshot,
    warmup_candles,
)


def _fixture_dataset() -> CandleDataset:
    """A minimal, structurally-valid ``market="KRW-BTC"``,
    ``unit_minutes=240`` dataset. The D3 mismatch checks fire before any
    other pre-flight, so the exact shape beyond that doesn't matter."""
    candles = warmup_candles()
    candles.append(flat(WARMUP_CANDLE_COUNT, "100000000"))
    return make_dataset(candles)


class TestPaperInputContractRejection:
    def test_market_mismatch_raises_before_state_write(self, tmp_path: Path) -> None:
        dataset = _fixture_dataset()
        snapshot = make_snapshot()
        config = make_backtest_config_with_strategy(market="KRW-ETH")
        now = dataset.candles[-1].open_time_utc + STEP

        with pytest.raises(
            PaperInputContractMismatchError,
            match=re.escape(
                "dataset.market='KRW-BTC' != config.strategy.market='KRW-ETH'"
            ),
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert list(tmp_path.iterdir()) == []

    def test_unit_minutes_mismatch_raises_before_state_write(
        self, tmp_path: Path
    ) -> None:
        dataset = _fixture_dataset()
        snapshot = make_snapshot()
        config = make_backtest_config_with_strategy(unit_minutes=60)
        now = dataset.candles[-1].open_time_utc + STEP

        with pytest.raises(
            PaperInputContractMismatchError,
            match=re.escape(
                "dataset.unit_minutes=240 != config.strategy.unit_minutes=60"
            ),
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)

        assert list(tmp_path.iterdir()) == []


class TestRunBacktestParity:
    @pytest.mark.parametrize(
        ("mismatch_field", "dataset_value", "config_value"),
        [
            ("market", "KRW-BTC", "KRW-ETH"),
            ("unit_minutes", 240, 60),
        ],
    )
    def test_paper_and_backtest_both_refuse_market_and_unit_mismatch(
        self,
        tmp_path: Path,
        mismatch_field: str,
        dataset_value: object,
        config_value: object,
    ) -> None:
        dataset = _fixture_dataset()
        snapshot = make_snapshot()
        if mismatch_field == "market":
            assert dataset.market == dataset_value
            config = make_backtest_config_with_strategy(market=str(config_value))
        else:
            assert dataset.unit_minutes == dataset_value
            config = make_backtest_config_with_strategy(unit_minutes=int(config_value))  # type: ignore[arg-type]
        now = dataset.candles[-1].open_time_utc + STEP

        # (a) run_backtest refuses with a bare ValueError naming the
        # mismatched field.
        with pytest.raises(ValueError, match=re.escape(mismatch_field)):
            run_backtest(dataset, snapshot, config)

        # (b) run_paper_session refuses with the dedicated
        # PaperInputContractMismatchError, also naming the mismatched
        # field — proving the paper runner mirrors run_backtest's own
        # contract boundary.
        with pytest.raises(
            PaperInputContractMismatchError, match=re.escape(mismatch_field)
        ):
            run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
