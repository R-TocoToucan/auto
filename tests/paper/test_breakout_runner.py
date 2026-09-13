"""Focused tests for the shadow breakout paper runner (post-7ca7f78 fix).

Covers:

* Raw prior-high exit boundary — exit threshold is
  ``max(prior_120_high_at_entry, prior_60_low)``, with the RAW
  unbuffered prior high.
* Next-candle execution — buy/sell fill_ts_utc == source_open + STEP.
* Open-LONG resume parity: splitting the dataset and resuming while
  LONG produces the exact same fills / cash / position as a single-
  shot run over the concatenated dataset.
* Final-candle signal fills on the next appended candle — a signal
  emitted on the last candle of run 1 fires as a fill on the first
  new candle of run 2.
* Stopped-out-lockout resume parity — a run split across the stop-out
  candle produces the same terminal state as a single-shot run.
* Mutated-prefix refusal — mutating a previously processed candle on
  resume raises ProcessedPrefixMutatedError and leaves every on-disk
  audit file byte-unchanged.
* Per-candle equity count and replay — equity.jsonl grows by exactly
  the number of new forward candles per invocation.
* Separate ledgers vs the SMA paper runner.
* Input contract / incomplete / insufficient refusals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.bithumb_spec.snapshot import FeeRates, SnapshotV1
from bithumb_bot.core.money import Money
from bithumb_bot.errors import ProcessedPrefixMutatedError
from bithumb_bot.execution.config import ExecutionConfig
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import CandleDataset, DatasetProvenance
from bithumb_bot.paper.breakout_runner import (
    WARMUP_CANDLE_COUNT,
    BreakoutInputContractMismatchError,
    BreakoutPaperConfig,
    BreakoutResumeDivergenceError,
    run_breakout_paper_session,
)

from .conftest import make_snapshot

UNIT = 240
T0 = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=UNIT)


def _candle(
    idx: int, *, open_: str, high: str, low: str, close: str
) -> Candle:
    return Candle(
        market="KRW-BTC",
        unit_minutes=UNIT,
        open_time_utc=T0 + idx * STEP,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume="1",
        quote_volume=close,
    )


def _flat(idx: int, price: str) -> Candle:
    return _candle(idx, open_=price, high=price, low=price, close=price)


def _dataset(candles: list[Candle]) -> CandleDataset:
    end = candles[-1].open_time_utc + STEP
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=UNIT,
        requested_start_utc=candles[0].open_time_utc.isoformat(),
        requested_end_utc=end.isoformat(),
        fetched_at_utc="2026-01-01T00:00:00+00:00",
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


def _config(
    *,
    cash: str = "20000000",
    max_notional_krw: str = "100000000",
) -> BreakoutPaperConfig:
    execution = ExecutionConfig(
        slippage_bps_per_side=Decimal("25"),
        max_notional_krw=Money(Decimal(max_notional_krw)),
        allow_provisional_fee_model=False,
        simulation_quantity_quantum=Decimal("0.00000001"),
    )
    return BreakoutPaperConfig(
        starting_cash_krw=Money(Decimal(cash)),
        max_notional_krw=Money(Decimal(max_notional_krw)),
        execution=execution,
    )


def _entry_series() -> list[Candle]:
    """120 flat warmup candles + a breakout candle at index 120.

    prior_120_high = 100_000_000; upper (50 bps) = 100_500_000.
    Breakout close = 100_600_000 strictly above upper → LONG entry.
    entry_breakout_level = raw prior_120_high = 100_000_000.
    """
    return [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)] + [
        _candle(
            WARMUP_CANDLE_COUNT,
            open_="100000000",
            high="100600000",
            low="100000000",
            close="100600000",
        )
    ]


def _snapshot_dir(state_dir: Path) -> dict[str, bytes | None]:
    names = (
        "state.json",
        "state.json.sha256",
        "fills.jsonl",
        "signals.jsonl",
        "equity.jsonl",
        "candle_fingerprints.jsonl",
    )
    return {
        name: (
            (state_dir / name).read_bytes()
            if (state_dir / name).is_file()
            else None
        )
        for name in names
    }


class TestRawPriorHighExitBoundary:
    def test_exit_at_prior_120_high_not_buffered_boundary(
        self, tmp_path: Path
    ) -> None:
        # entry_breakout_level should be RAW 100_000_000 (not 100_500_000).
        # Exit threshold = max(100M, prior_60_low=100M) = 100M.
        # A drop-candle close of 99_999_999 (one unit below RAW level)
        # exits; the OLD buffered rule would have kept LONG at 100_499_999.
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        candles.append(
            _candle(
                WARMUP_CANDLE_COUNT + 2,
                open_="100600000",
                high="100600000",
                low="99999999",
                close="99999999",
            )
        )
        candles.append(_flat(WARMUP_CANDLE_COUNT + 3, "99999999"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()

        result = run_breakout_paper_session(
            dataset, snapshot, config, tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert result.invalid_reason is None, result.invalid_reason
        assert len(result.forward_entries) == 2
        buy, sell = result.forward_entries
        assert buy.side == "buy"
        assert sell.side == "sell"
        assert buy.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 1].open_time_utc
        assert sell.fill_ts_utc == candles[WARMUP_CANDLE_COUNT + 3].open_time_utc

    def test_close_equal_to_raw_prior_high_retains_long(
        self, tmp_path: Path
    ) -> None:
        # Close exactly at the RAW prior_120_high — retain LONG.
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        candles.append(
            _candle(
                WARMUP_CANDLE_COUNT + 2,
                open_="100600000",
                high="100600000",
                low="100000000",
                close="100000000",
            )
        )
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()

        result = run_breakout_paper_session(
            dataset, snapshot, config, tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert result.invalid_reason is None
        # Only the BUY fills; equality with RAW prior high retains LONG.
        assert len(result.forward_entries) == 1
        assert result.forward_entries[0].side == "buy"


class TestNextCandleExecution:
    def test_buy_never_fills_on_source_candle(self, tmp_path: Path) -> None:
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        config = _config()

        result = run_breakout_paper_session(
            dataset, snapshot, config, tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert len(result.forward_entries) == 1
        buy = result.forward_entries[0]
        assert buy.source_open_time_utc == candles[WARMUP_CANDLE_COUNT].open_time_utc
        assert buy.fill_ts_utc == buy.source_open_time_utc + STEP


class TestOpenLongResumeParity:
    def test_split_run_matches_one_shot_across_long_state(
        self, tmp_path: Path
    ) -> None:
        # Build a dataset where the strategy is LONG mid-way. Split
        # between the fill candle and the exit-signal candle so the
        # split happens while LONG. Resume must reproduce the identical
        # fills / cash / position as a one-shot run.
        full_candles = _entry_series()
        # candle 121: BUY fills here.
        full_candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        # candle 122: still holding LONG, close above SMA-of-highs.
        full_candles.append(_flat(WARMUP_CANDLE_COUNT + 2, "100600000"))
        # candle 123: strategy CASH signal (close < RAW prior_120_high).
        full_candles.append(
            _candle(
                WARMUP_CANDLE_COUNT + 3,
                open_="100600000",
                high="100600000",
                low="99999999",
                close="99999999",
            )
        )
        # candle 124: SELL fills here.
        full_candles.append(_flat(WARMUP_CANDLE_COUNT + 4, "99999999"))

        # One-shot run.
        one_shot_dir = tmp_path / "one_shot"
        one_shot_dir.mkdir()
        one_shot = run_breakout_paper_session(
            _dataset(full_candles), make_snapshot(), _config(), one_shot_dir,
            now_utc=full_candles[-1].open_time_utc + STEP,
        )
        assert one_shot.invalid_reason is None

        # Split-and-resume: split AFTER the BUY fill (index 121), while LONG.
        split_dir = tmp_path / "split"
        split_dir.mkdir()
        first_slice = full_candles[: WARMUP_CANDLE_COUNT + 2]  # includes fill
        r1 = run_breakout_paper_session(
            _dataset(first_slice), make_snapshot(), _config(), split_dir,
            now_utc=first_slice[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        # After the BUY fills, position is > 0 (LONG mid-resume).
        assert r1.final_position_qty.value > 0
        assert len(r1.forward_entries) == 1

        r2 = run_breakout_paper_session(
            _dataset(full_candles), make_snapshot(), _config(), split_dir,
            now_utc=full_candles[-1].open_time_utc + STEP,
        )
        assert r2.invalid_reason is None

        # Parity: same total fills, cash, position.
        assert len(r2.forward_entries) == len(one_shot.forward_entries) == 2
        assert r2.final_cash_krw == one_shot.final_cash_krw
        assert r2.final_position_qty == one_shot.final_position_qty
        # Fills logs equal.
        assert (
            (split_dir / "fills.jsonl").read_bytes()
            == (one_shot_dir / "fills.jsonl").read_bytes()
        )
        # Equity logs equal.
        assert (
            (split_dir / "equity.jsonl").read_bytes()
            == (one_shot_dir / "equity.jsonl").read_bytes()
        )


class TestFinalCandleSignalFillsOnNextAppend:
    def test_signal_on_last_candle_fires_on_next_run_first_candle(
        self, tmp_path: Path
    ) -> None:
        # Run 1: warmup + entry candle. Signal emits (LONG); no fill
        # (no next candle yet).
        first_slice = _entry_series()
        r1 = run_breakout_paper_session(
            _dataset(first_slice), make_snapshot(), _config(), tmp_path,
            now_utc=first_slice[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        assert r1.new_signals_this_invocation == 1
        assert r1.new_fills_this_invocation == 0
        assert r1.final_position_qty.value == 0

        # Run 2: append one more candle. That candle is the fill.
        appended = first_slice + [_flat(WARMUP_CANDLE_COUNT + 1, "100600000")]
        r2 = run_breakout_paper_session(
            _dataset(appended), make_snapshot(), _config(), tmp_path,
            now_utc=appended[-1].open_time_utc + STEP,
        )
        assert r2.invalid_reason is None
        assert r2.new_fills_this_invocation == 1
        buy = r2.forward_entries[0]
        assert buy.side == "buy"
        # Fill happens on the newly appended candle, not the original last.
        assert buy.fill_ts_utc == appended[-1].open_time_utc


class TestStoppedOutLockoutResumeParity:
    def test_split_across_stop_out_matches_one_shot(
        self, tmp_path: Path
    ) -> None:
        # Build a dataset that: enters LONG, then gaps way below the
        # 10% protective stop level (triggers stop-out and arms the
        # lockout), then recovers and re-enters — but the lockout must
        # persist until a CASH transition clears it. The parity check
        # is: split before the stop-out vs. one-shot must yield the
        # exact same terminal state after the recovery candles.
        candles = _entry_series()
        # 121: BUY fills. Fill price ≈ 100.6M * 1.0025 = 100_851_500;
        # protective stop = ~90_766_350.
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        # 122: STOP-OUT candle. Low well below the stop level.
        candles.append(
            _candle(
                WARMUP_CANDLE_COUNT + 2,
                open_="100600000",
                high="100600000",
                low="50000000",
                close="50000000",
            )
        )
        # 123..: recovery — flat candles keep close < RAW prior high so
        # no new LONG entry until the strategy first returns to CASH.
        # The strategy is already in CASH after stop-out (because we
        # arm lockout). To clear lockout, close must drop such that
        # signal emits CASH. But strategy tracks its own state which
        # is LONG from the entry signal — it only emits CASH when
        # close < max(entry_level, prior_60_low). At index 123, close
        # is 50M << 100M → CASH signal emits → clears lockout.
        candles.append(_flat(WARMUP_CANDLE_COUNT + 3, "50000000"))
        candles.append(_flat(WARMUP_CANDLE_COUNT + 4, "50000000"))
        dataset_full = _dataset(candles)

        # One-shot.
        one_shot_dir = tmp_path / "one_shot"
        one_shot_dir.mkdir()
        one_shot = run_breakout_paper_session(
            dataset_full, make_snapshot(), _config(), one_shot_dir,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert one_shot.invalid_reason is None
        # BUY at 121 + stop-out sell at 122 = 2 entries.
        assert len(one_shot.forward_entries) == 2

        # Split-and-resume: split AFTER the BUY fill (index 121).
        split_dir = tmp_path / "split"
        split_dir.mkdir()
        first_slice = candles[: WARMUP_CANDLE_COUNT + 2]
        r1 = run_breakout_paper_session(
            _dataset(first_slice), make_snapshot(), _config(), split_dir,
            now_utc=first_slice[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        assert len(r1.forward_entries) == 1  # BUY only, no stop yet.
        assert r1.final_position_qty.value > 0
        assert r1.stopped_out_lockout is False

        r2 = run_breakout_paper_session(
            dataset_full, make_snapshot(), _config(), split_dir,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r2.invalid_reason is None

        assert len(r2.forward_entries) == len(one_shot.forward_entries)
        assert r2.final_cash_krw == one_shot.final_cash_krw
        assert r2.final_position_qty == one_shot.final_position_qty
        assert r2.stopped_out_lockout == one_shot.stopped_out_lockout
        assert (
            (split_dir / "fills.jsonl").read_bytes()
            == (one_shot_dir / "fills.jsonl").read_bytes()
        )


class TestMutatedPrefixRefusal:
    def test_mutated_candle_on_resume_refuses_without_mutation(
        self, tmp_path: Path
    ) -> None:
        # Run 1: process 121 candles (warmup + entry).
        first_slice = _entry_series()
        r1 = run_breakout_paper_session(
            _dataset(first_slice), make_snapshot(), _config(), tmp_path,
            now_utc=first_slice[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None

        # Capture on-disk state.
        before = _snapshot_dir(tmp_path)
        assert before["candle_fingerprints.jsonl"] is not None

        # Mutate the entry candle's HIGH — this changes its fingerprint.
        mutated_candles = list(first_slice)
        original = mutated_candles[WARMUP_CANDLE_COUNT]
        # Bump high by 1.
        mutated_candles[WARMUP_CANDLE_COUNT] = _candle(
            WARMUP_CANDLE_COUNT,
            open_=str(original.open.value),
            high=str(original.high.value + Decimal("1")),
            low=str(original.low.value),
            close=str(original.close.value),
        )
        # Append a new candle so resume tries to advance.
        mutated_candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))

        with pytest.raises(ProcessedPrefixMutatedError):
            run_breakout_paper_session(
                _dataset(mutated_candles), make_snapshot(), _config(),
                tmp_path, now_utc=mutated_candles[-1].open_time_utc + STEP,
            )

        # Every persisted file byte-unchanged.
        after = _snapshot_dir(tmp_path)
        assert after == before


class TestPerCandleEquityCountAndReplay:
    def test_one_equity_point_per_forward_candle(
        self, tmp_path: Path
    ) -> None:
        # 5 forward candles → equity.jsonl has 5 lines.
        candles = _entry_series() + [
            _flat(WARMUP_CANDLE_COUNT + k, "100600000") for k in range(1, 5)
        ]
        r = run_breakout_paper_session(
            _dataset(candles), make_snapshot(), _config(), tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r.invalid_reason is None
        lines = (tmp_path / "equity.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        assert r.new_equity_points_this_invocation == 5
        # 5 forward candles including entry + 4 flat.
        assert r.forward_candle_count == 5

    def test_resume_appends_only_new_equity_points_and_prefix_matches(
        self, tmp_path: Path
    ) -> None:
        # Run 1: 3 forward candles.
        candles_a = _entry_series() + [
            _flat(WARMUP_CANDLE_COUNT + 1, "100600000"),
            _flat(WARMUP_CANDLE_COUNT + 2, "100600000"),
        ]
        r1 = run_breakout_paper_session(
            _dataset(candles_a), make_snapshot(), _config(), tmp_path,
            now_utc=candles_a[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        equity_before = (
            (tmp_path / "equity.jsonl").read_text(encoding="utf-8").splitlines()
        )
        assert len(equity_before) == 3

        # Run 2: append 2 more.
        candles_b = candles_a + [
            _flat(WARMUP_CANDLE_COUNT + 3, "100600000"),
            _flat(WARMUP_CANDLE_COUNT + 4, "100600000"),
        ]
        r2 = run_breakout_paper_session(
            _dataset(candles_b), make_snapshot(), _config(), tmp_path,
            now_utc=candles_b[-1].open_time_utc + STEP,
        )
        assert r2.invalid_reason is None
        equity_after = (
            (tmp_path / "equity.jsonl").read_text(encoding="utf-8").splitlines()
        )
        assert len(equity_after) == 5
        assert r2.new_equity_points_this_invocation == 2
        # Prefix byte-equal.
        assert equity_after[:3] == equity_before


class TestSeparateLedgers:
    def test_breakout_state_dir_does_not_touch_sma_state_dir(
        self, tmp_path: Path
    ) -> None:
        sma_state = tmp_path / "sma_state"
        breakout_state = tmp_path / "breakout_state"
        sma_state.mkdir()
        (sma_state / "sma_sentinel").write_bytes(b"sma-owned")

        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        result = run_breakout_paper_session(
            _dataset(candles), make_snapshot(), _config(), breakout_state,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert result.invalid_reason is None
        assert (breakout_state / "state.json").is_file()
        assert (breakout_state / "fills.jsonl").is_file()
        assert (sma_state / "sma_sentinel").read_bytes() == b"sma-owned"
        assert not (sma_state / "state.json").exists()


class TestResumeDivergence:
    def test_resume_refuses_starting_cash_change_without_mutation(
        self, tmp_path: Path
    ) -> None:
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(_flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        r1 = run_breakout_paper_session(
            dataset, snapshot, _config(cash="20000000"),
            tmp_path, now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        before = _snapshot_dir(tmp_path)

        with pytest.raises(BreakoutResumeDivergenceError):
            run_breakout_paper_session(
                dataset, snapshot, _config(cash="30000000"),
                tmp_path, now_utc=candles[-1].open_time_utc + STEP,
            )
        after = _snapshot_dir(tmp_path)
        assert after == before


class TestInputContract:
    def test_wrong_market_refuses(self, tmp_path: Path) -> None:
        candles = [
            Candle(
                market="KRW-ETH",
                unit_minutes=UNIT,
                open_time_utc=T0 + i * STEP,
                open="100000000",
                high="100000000",
                low="100000000",
                close="100000000",
                volume="1",
                quote_volume="100000000",
            )
            for i in range(WARMUP_CANDLE_COUNT + 1)
        ]
        end = candles[-1].open_time_utc + STEP
        dataset = CandleDataset(
            schema_version=1,
            venue="bithumb",
            market="KRW-ETH",
            unit_minutes=UNIT,
            requested_start_utc=candles[0].open_time_utc.isoformat(),
            requested_end_utc=end.isoformat(),
            fetched_at_utc="2026-01-01T00:00:00+00:00",
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
        with pytest.raises(BreakoutInputContractMismatchError):
            run_breakout_paper_session(
                dataset, make_snapshot(), _config(),
                tmp_path, now_utc=candles[-1].open_time_utc + STEP,
            )

    def test_incomplete_final_candle_refuses(self, tmp_path: Path) -> None:
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        candles.append(_flat(WARMUP_CANDLE_COUNT, "100000000"))
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        now = candles[-1].open_time_utc  # not yet closed

        result = run_breakout_paper_session(
            dataset, snapshot, _config(), tmp_path, now_utc=now
        )
        assert result.refusal_code == "BreakoutIncompleteCandleError"
        assert not (tmp_path / "state.json").exists()

    def test_insufficient_forward_candles_refuses(self, tmp_path: Path) -> None:
        candles = [_flat(i, "100000000") for i in range(WARMUP_CANDLE_COUNT)]
        dataset = _dataset(candles)
        snapshot = make_snapshot()
        now = candles[-1].open_time_utc + STEP

        result = run_breakout_paper_session(
            dataset, snapshot, _config(), tmp_path, now_utc=now
        )
        assert result.refusal_code == "BreakoutInsufficientForwardCandlesError"
        assert not (tmp_path / "state.json").exists()


# ---------------------------------------------------------------------------
# b3ce934-followup safety patch — input-fingerprint drift refusals
# ---------------------------------------------------------------------------


def _replace_candle_close(candle: Candle, new_close: str) -> Candle:
    return Candle(
        market=candle.market,
        unit_minutes=candle.unit_minutes,
        open_time_utc=candle.open_time_utc,
        open=str(candle.open.value),
        high=str(candle.high.value),
        low=str(candle.low.value),
        close=new_close,
        volume=str(candle.volume.value),
        quote_volume=str(candle.quote_volume.value),
    )


def _first_run_and_snapshot_dir(
    tmp_path: Path,
) -> tuple[list[Candle], SnapshotV1, BreakoutPaperConfig, dict[str, bytes | None]]:
    """Do a fresh first run + return the on-disk snapshot to compare
    against after a drift attempt."""
    candles = _entry_series()
    dataset = _dataset(candles)
    snapshot = make_snapshot()
    config = _config()
    r1 = run_breakout_paper_session(
        dataset, snapshot, config, tmp_path,
        now_utc=candles[-1].open_time_utc + STEP,
    )
    assert r1.invalid_reason is None
    return candles, snapshot, config, _snapshot_dir(tmp_path)


class TestWarmupFingerprintDrift:
    def test_mutated_warmup_candle_refuses_without_file_mutation(
        self, tmp_path: Path
    ) -> None:
        candles, snapshot, config, before = _first_run_and_snapshot_dir(
            tmp_path
        )

        # Mutate an in-warmup candle's close value. This changes the
        # warmup fingerprint but leaves every forward-candle
        # fingerprint identical, so the ONLY failing check is the
        # warmup drift refusal.
        mutated = list(candles)
        mutated[10] = _replace_candle_close(mutated[10], "100000001")

        with pytest.raises(BreakoutResumeDivergenceError, match="warmup_sha256"):
            run_breakout_paper_session(
                _dataset(mutated), snapshot, config, tmp_path,
                now_utc=mutated[-1].open_time_utc + STEP,
            )

        assert _snapshot_dir(tmp_path) == before


class TestConfigFingerprintDrift:
    @pytest.mark.parametrize(
        "field,new_kwargs",
        [
            ("slippage", {"slippage_bps_per_side": Decimal("40")}),
            (
                "max_notional",
                {"max_notional_krw": Money(Decimal("50000000"))},
            ),
            (
                "allow_provisional",
                {"allow_provisional_fee_model": True},
            ),
            (
                "quantum",
                {"simulation_quantity_quantum": Decimal("0.00001000")},
            ),
        ],
    )
    def test_execution_config_field_change_refuses_without_mutation(
        self, tmp_path: Path, field: str, new_kwargs: dict[str, object]
    ) -> None:
        candles, snapshot, config, before = _first_run_and_snapshot_dir(
            tmp_path
        )

        drifted_execution = ExecutionConfig(
            slippage_bps_per_side=(
                new_kwargs.get(
                    "slippage_bps_per_side",
                    config.execution.slippage_bps_per_side,
                )  # type: ignore[arg-type]
            ),
            max_notional_krw=(
                new_kwargs.get(
                    "max_notional_krw", config.execution.max_notional_krw
                )  # type: ignore[arg-type]
            ),
            allow_provisional_fee_model=(
                new_kwargs.get(
                    "allow_provisional_fee_model",
                    config.execution.allow_provisional_fee_model,
                )  # type: ignore[arg-type]
            ),
            simulation_quantity_quantum=(
                new_kwargs.get(
                    "simulation_quantity_quantum",
                    config.execution.simulation_quantity_quantum,
                )  # type: ignore[arg-type]
            ),
        )
        drifted = BreakoutPaperConfig(
            starting_cash_krw=config.starting_cash_krw,
            max_notional_krw=config.max_notional_krw,
            execution=drifted_execution,
        )

        with pytest.raises(BreakoutResumeDivergenceError, match="config_sha256"):
            run_breakout_paper_session(
                _dataset(candles), snapshot, drifted, tmp_path,
                now_utc=candles[-1].open_time_utc + STEP,
            )

        assert _snapshot_dir(tmp_path) == before


class TestSnapshotFingerprintDrift:
    def test_snapshot_fee_change_refuses_without_file_mutation(
        self, tmp_path: Path
    ) -> None:
        candles, snapshot, config, before = _first_run_and_snapshot_dir(
            tmp_path
        )

        # Rebuild the snapshot with a different (still-valid) fee rate.
        drifted_snapshot = snapshot.model_copy(
            update={"fee_rates": FeeRates(bid="0.0026", ask="0.0025")}
        )

        with pytest.raises(BreakoutResumeDivergenceError, match="snapshot_sha256"):
            run_breakout_paper_session(
                _dataset(candles), drifted_snapshot, config, tmp_path,
                now_utc=candles[-1].open_time_utc + STEP,
            )

        assert _snapshot_dir(tmp_path) == before


class TestUnchangedResumeMatchesOneShot:
    """Regression guard: fingerprint tightening must not break the
    everyday "resume with identical inputs" flow."""

    def test_flat_resume_appends_only_new_suffix_and_matches_one_shot(
        self, tmp_path: Path
    ) -> None:
        candles_a = _entry_series() + [
            _flat(WARMUP_CANDLE_COUNT + 1, "100600000"),
            _flat(WARMUP_CANDLE_COUNT + 2, "100600000"),
        ]
        candles_b = candles_a + [
            _flat(WARMUP_CANDLE_COUNT + 3, "100600000"),
            _flat(WARMUP_CANDLE_COUNT + 4, "100600000"),
        ]

        # One-shot over the full extended dataset.
        one_shot_dir = tmp_path / "one_shot"
        one_shot_dir.mkdir()
        one_shot = run_breakout_paper_session(
            _dataset(candles_b), make_snapshot(), _config(), one_shot_dir,
            now_utc=candles_b[-1].open_time_utc + STEP,
        )
        assert one_shot.invalid_reason is None

        # Two-shot: first the short dataset, then the extended one.
        split_dir = tmp_path / "split"
        split_dir.mkdir()
        r1 = run_breakout_paper_session(
            _dataset(candles_a), make_snapshot(), _config(), split_dir,
            now_utc=candles_a[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        r2 = run_breakout_paper_session(
            _dataset(candles_b), make_snapshot(), _config(), split_dir,
            now_utc=candles_b[-1].open_time_utc + STEP,
        )
        assert r2.invalid_reason is None

        # Terminal state and audit trails byte-equal.
        assert r2.final_cash_krw == one_shot.final_cash_krw
        assert r2.final_position_qty == one_shot.final_position_qty
        assert (
            (split_dir / "fills.jsonl").read_bytes()
            == (one_shot_dir / "fills.jsonl").read_bytes()
        )
        assert (
            (split_dir / "signals.jsonl").read_bytes()
            == (one_shot_dir / "signals.jsonl").read_bytes()
        )
        assert (
            (split_dir / "equity.jsonl").read_bytes()
            == (one_shot_dir / "equity.jsonl").read_bytes()
        )
        assert (
            (split_dir / "candle_fingerprints.jsonl").read_bytes()
            == (one_shot_dir / "candle_fingerprints.jsonl").read_bytes()
        )


class TestMalformedJsonlBecomesPrefixMutatedError:
    """A hand-corrupted JSONL line surfaces as
    ProcessedPrefixMutatedError, not a bare ValueError leaking to the
    caller."""

    def test_malformed_fills_jsonl_line_raises_prefix_mutated(
        self, tmp_path: Path
    ) -> None:
        # First run to create the audit trail.
        candles = _entry_series()
        candles.append(_flat(WARMUP_CANDLE_COUNT + 1, "100600000"))
        r1 = run_breakout_paper_session(
            _dataset(candles), make_snapshot(), _config(), tmp_path,
            now_utc=candles[-1].open_time_utc + STEP,
        )
        assert r1.invalid_reason is None
        fills_path = tmp_path / "fills.jsonl"
        assert fills_path.is_file()

        # Corrupt fills.jsonl with a non-JSON line.
        fills_path.write_bytes(b"{not json at all\n")

        candles.append(_flat(WARMUP_CANDLE_COUNT + 2, "100600000"))
        with pytest.raises(ProcessedPrefixMutatedError):
            run_breakout_paper_session(
                _dataset(candles), make_snapshot(), _config(), tmp_path,
                now_utc=candles[-1].open_time_utc + STEP,
            )
