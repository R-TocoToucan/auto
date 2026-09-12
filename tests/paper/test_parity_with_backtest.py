"""Determinism of `run_paper_session` — hard-requirement regression guard.

The parity contract this file ORIGINALLY expressed (paper runner's
``forward_entries`` byte-equal to the shared backtest engine's own
entries, filtered to the forward window) is INVALID after the D-erv D1
warmup-isolation fix: the paper runner deliberately no longer replays
a warmup-sourced fill into the forward ledger, so the two engines'
outputs are EXPECTED to diverge whenever the warmup slice would have
emitted a transition (see ``paper/runner.py``'s module docstring).

What DOES still hold, and is guarded here instead, is DETERMINISM: two
independent invocations of :func:`run_paper_session` against the
identical ``(dataset, snapshot, config)`` must produce byte-identical
on-disk artifacts and an identical ``PaperSessionResult.forward_entries``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bithumb_bot.backtest.config import BacktestConfig
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

_ARTIFACT_NAMES = (
    "state.json",
    "fills.jsonl",
    "signals.jsonl",
    "candle_fingerprints.jsonl",
)


def _artifact_bytes(state_dir: Path) -> dict[str, bytes]:
    return {
        name: (
            (state_dir / name).read_bytes() if (state_dir / name).is_file() else b""
        )
        for name in _ARTIFACT_NAMES
    }


class TestPaperSessionDeterminism:
    def test_two_invocations_over_identical_inputs_are_byte_identical(
        self,
        tmp_path: Path,
        paper_fixture: tuple[object, object, object],
    ) -> None:
        dataset, snapshot, config = paper_fixture
        now = dataset.candles[-1].open_time_utc + STEP

        first = run_paper_session(dataset, snapshot, config, tmp_path, now_utc=now)
        assert first.invalid_reason is None
        artifacts_1 = _artifact_bytes(tmp_path)

        # Re-run against a FRESH state_dir with the SAME inputs — this
        # is NOT a resume (different dir), so both invocations
        # independently exercise the full pre-flight + forward loop
        # from scratch.
        other_dir = tmp_path.parent / (tmp_path.name + "_replay")
        other_dir.mkdir()
        second = run_paper_session(dataset, snapshot, config, other_dir, now_utc=now)
        assert second.invalid_reason is None
        artifacts_2 = _artifact_bytes(other_dir)

        assert artifacts_1 == artifacts_2
        assert first.forward_entries == second.forward_entries


# ---------------------------------------------------------------------------
# Property-based demonstration: for ANY small forward tail appended to the
# fixed 1200-candle warmup, two independent run_paper_session invocations
# over the SAME inputs produce identical forward_entries and identical
# on-disk artifacts.
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
def test_determinism_property_random_forward_tail(
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
    dir_1 = tmp_path_factory.mktemp("determinism_1")
    dir_2 = tmp_path_factory.mktemp("determinism_2")

    first = run_paper_session(dataset, snapshot, config, dir_1, now_utc=now)
    second = run_paper_session(dataset, snapshot, config, dir_2, now_utc=now)

    if first.invalid_reason is not None:
        assert second.refusal_code == first.refusal_code
        return

    assert second.invalid_reason is None
    assert first.forward_entries == second.forward_entries
    assert _artifact_bytes(dir_1) == _artifact_bytes(dir_2)
