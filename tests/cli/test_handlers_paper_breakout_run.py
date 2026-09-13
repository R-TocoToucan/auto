"""Focused CLI-level tests for ``bt paper breakout-run``.

Covers:

* Golden run — argparse tree accepts the five required flags, the
  handler writes a report + sidecar, and the report carries every
  required pinning field.
* Overwrite guard — a second run with the same ``--out`` refuses.
* Help — the argparse help text names every required flag.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bithumb_bot.bithumb_spec.sanitize import sanitize_orders_chance
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import build_snapshot, write_snapshot_with_sidecar
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import (
    CandleDataset,
    DatasetProvenance,
    write_dataset_with_sidecar,
)
from bithumb_bot.paper.breakout_runner import WARMUP_CANDLE_COUNT

UNIT = 240
_STEP = timedelta(minutes=UNIT)
OBSERVED_JSON = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
    / "observed_krw_btc.json"
)


@pytest.fixture()
def _env(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
    for key in (
        "BITHUMB_TRADE_ACCESS_KEY",
        "BITHUMB_TRADE_SECRET_KEY",
        "BITHUMB_ACCOUNT_READ_ACCESS_KEY",
        "BITHUMB_ACCOUNT_READ_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _write_snapshot(tmp_path: Path) -> Path:
    raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
    sanitized = sanitize_orders_chance(raw)
    parsed = OrdersChanceResponse.model_validate(sanitized)
    snapshot = build_snapshot(
        parsed, market="KRW-BTC", fixture_paths=[OBSERVED_JSON]
    )
    target = tmp_path / "snap.json"
    write_snapshot_with_sidecar(snapshot, target)
    return target


def _write_dataset(tmp_path: Path) -> Path:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    candles: list[Candle] = []
    for i in range(WARMUP_CANDLE_COUNT):
        candles.append(
            Candle(
                market="KRW-BTC",
                unit_minutes=UNIT,
                open_time_utc=start + i * _STEP,
                open="100000000",
                high="100000000",
                low="100000000",
                close="100000000",
                volume="1",
                quote_volume="100000000",
            )
        )
    # Breakout candle: close just above upper (100_500_000). Low kept
    # at 100_000_000 so the fill candle's protective stop (10% below
    # ~101.1M fill = ~90.99M) is never in play.
    candles.append(
        Candle(
            market="KRW-BTC",
            unit_minutes=UNIT,
            open_time_utc=start + WARMUP_CANDLE_COUNT * _STEP,
            open="100000000",
            high="100600000",
            low="100000000",
            close="100600000",
            volume="1",
            quote_volume="100600000",
        )
    )
    # Fill candle for the buy.
    candles.append(
        Candle(
            market="KRW-BTC",
            unit_minutes=UNIT,
            open_time_utc=start + (WARMUP_CANDLE_COUNT + 1) * _STEP,
            open="100600000",
            high="100600000",
            low="100600000",
            close="100600000",
            volume="1",
            quote_volume="100600000",
        )
    )
    end = candles[-1].open_time_utc + _STEP
    dataset = CandleDataset(
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
    target = tmp_path / "dataset.json"
    write_dataset_with_sidecar(dataset, target)
    return target


def _run(
    *,
    dataset: Path,
    snapshot: Path,
    state_dir: Path,
    out: Path,
    starting_cash: str = "10000000",
    max_notional: str = "100000000",
) -> int:
    return main(
        [
            "paper",
            "breakout-run",
            "--dataset",
            str(dataset),
            "--snapshot",
            str(snapshot),
            "--state-dir",
            str(state_dir),
            "--out",
            str(out),
            "--starting-cash-krw",
            starting_cash,
            "--max-notional-krw",
            max_notional,
        ]
    )


class TestBreakoutRunGoldenPath:
    def test_report_pins_required_fields(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc = _run(
            dataset=dataset, snapshot=snapshot, state_dir=state_dir, out=out
        )
        assert rc == 0, capsys.readouterr()
        assert out.is_file()
        assert out.with_name(out.name + ".sha256").is_file()

        parsed = json.loads(out.read_text(encoding="utf-8"))
        # Every pinning field the requirement asked for.
        assert parsed["run_purpose"] == "engineering_smoke"
        assert parsed["selection_eligible"] is False
        assert parsed["holdout_eligible"] is False
        assert parsed["strategy"]["entry_buffer_bps"] == "50"
        assert parsed["strategy"]["entry_lookback_candles"] == 120
        assert parsed["strategy"]["exit_lookback_candles"] == 60
        # And the shadow identity.
        assert parsed["mode"] == "paper_breakout_shadow"

        # State-dir artifacts written to their own directory.
        assert (state_dir / "state.json").is_file()
        assert (state_dir / "fills.jsonl").is_file()
        assert (state_dir / "signals.jsonl").is_file()
        assert (state_dir / "equity.jsonl").is_file()


class TestBreakoutRunOverwriteGuard:
    def test_second_run_with_same_out_refuses(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc1 = _run(
            dataset=dataset, snapshot=snapshot, state_dir=state_dir, out=out
        )
        assert rc1 == 0, capsys.readouterr()

        rc2 = _run(
            dataset=dataset, snapshot=snapshot, state_dir=state_dir, out=out
        )
        assert rc2 != 0
        err = capsys.readouterr().err
        assert "refuse to overwrite" in err or "already" in err.lower()


class TestBreakoutRunHelp:
    def test_help_names_all_required_flags(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["paper", "breakout-run", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        for flag in (
            "--dataset",
            "--snapshot",
            "--state-dir",
            "--out",
            "--starting-cash-krw",
            "--max-notional-krw",
        ):
            assert flag in out
