"""Offline end-to-end tests for `bt paper run`.

Covers hard-requirement item #7 (CLI end-to-end: golden run, overwrite
guard, missing-argument refusals, zero-credential run) and item #8
(the persisted report AND `state.json` both pin `hysteresis_bps ==
"75"` — string, byte-equal).
"""

from __future__ import annotations

import json
import os
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
from bithumb_bot.paper.runner import WARMUP_CANDLE_COUNT

OBSERVED_JSON = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
    / "observed_krw_btc.json"
)
UNIT = 240
_STEP = timedelta(minutes=UNIT)


@pytest.fixture()
def _env(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
    monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", raising=False)
    return tmp_path


def _write_snapshot(tmp_path: Path) -> Path:
    raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
    sanitized = sanitize_orders_chance(raw)
    parsed = OrdersChanceResponse.model_validate(sanitized)
    snapshot = build_snapshot(parsed, market="KRW-BTC", fixture_paths=[OBSERVED_JSON])
    target = tmp_path / "snap.json"
    write_snapshot_with_sidecar(snapshot, target)
    return target


def _write_dataset(tmp_path: Path, *, name: str = "dataset.json") -> Path:
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
    # LONG-triggering candle at paper_start, then one more forward
    # candle so the resulting BUY intent has a next candle to fill.
    candles.append(
        Candle(
            market="KRW-BTC",
            unit_minutes=UNIT,
            open_time_utc=start + WARMUP_CANDLE_COUNT * _STEP,
            open="100000000",
            high="200000000",
            low="100000000",
            close="200000000",
            volume="1",
            quote_volume="100000000",
        )
    )
    candles.append(
        Candle(
            market="KRW-BTC",
            unit_minutes=UNIT,
            open_time_utc=start + (WARMUP_CANDLE_COUNT + 1) * _STEP,
            open="200000000",
            high="200000000",
            low="200000000",
            close="200000000",
            volume="1",
            quote_volume="100000000",
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
    target = tmp_path / name
    write_dataset_with_sidecar(dataset, target)
    return target


def _write_config(
    tmp_path: Path,
    *,
    hysteresis_bps: str = "75",
    starting_cash_krw: str = "100000",
    max_notional_krw: str = "100000",
    name: str = "config.toml",
) -> Path:
    body = f"""\
[backtest]
starting_cash_krw = "{starting_cash_krw}"
target_sleeve_fraction = "1.0"
protective_stop_fraction = "0.10"

[strategy]
rule_id = "price_over_sma"
ma_type = "SMA"
lookback_candles = {WARMUP_CANDLE_COUNT}
warmup_candles = {WARMUP_CANDLE_COUNT}
unit_minutes = 240
market = "KRW-BTC"
hysteresis_bps = "{hysteresis_bps}"

[execution]
slippage_bps_per_side = "50"
max_notional_krw = "{max_notional_krw}"
allow_provisional_fee_model = true
simulation_quantity_quantum = "0.00000001"
"""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _run(
    *, dataset: Path, snapshot: Path, config: Path, state_dir: Path, out: Path
) -> int:
    return main(
        [
            "paper",
            "run",
            "--dataset",
            str(dataset),
            "--snapshot",
            str(snapshot),
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
            "--out",
            str(out),
        ]
    )


class TestPaperRunGoldenPath:
    def test_golden_run_report_and_state_pin_hysteresis_75(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out,
        )
        assert rc == 0, capsys.readouterr()
        assert out.is_file()
        assert out.with_name(out.name + ".sha256").is_file()

        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed["run_purpose"] == "engineering_smoke"
        assert parsed["selection_eligible"] is False
        assert parsed["holdout_eligible"] is False
        assert parsed["mode"] == "paper"
        # Hard-requirement #8: hysteresis_bps pinned at "75" (string,
        # byte-equal) in BOTH the report and state.json.
        assert parsed["strategy"]["hysteresis_bps"] == "75"

        assert (state_dir / "state.json").is_file()
        assert (state_dir / "state.json.sha256").is_file()
        assert (state_dir / "fills.jsonl").is_file()
        state_parsed = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        assert state_parsed["hysteresis_bps"] == "75"

        paper_block = parsed["paper"]
        assert paper_block["warmup_candle_count"] == WARMUP_CANDLE_COUNT
        assert paper_block["forward_candle_count"] == 2
        assert paper_block["new_fills_this_invocation"] == 1
        assert paper_block["resumed"] is False

    def test_resumed_second_run_reports_zero_new_fills(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        state_dir = tmp_path / "state"
        out1 = tmp_path / "report1.json"
        out2 = tmp_path / "report2.json"

        rc1 = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out1,
        )
        assert rc1 == 0, capsys.readouterr()
        rc2 = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out2,
        )
        assert rc2 == 0, capsys.readouterr()
        parsed2 = json.loads(out2.read_text(encoding="utf-8"))
        assert parsed2["paper"]["resumed"] is True
        assert parsed2["paper"]["new_fills_this_invocation"] == 0


class TestPaperRunOverwriteGuard:
    def test_report_overwrite_refused_state_dir_reused(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc1 = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out,
        )
        assert rc1 == 0, capsys.readouterr()

        # Second run with the SAME --out must refuse (D-76) even though
        # --state-dir legitimately resumes.
        rc2 = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out,
        )
        assert rc2 != 0
        err = capsys.readouterr().err
        assert "refuse to overwrite" in err or "already" in err.lower()


class TestPaperRunMissingArguments:
    @pytest.mark.parametrize(
        "omit_flag",
        ["--dataset", "--snapshot", "--config", "--state-dir", "--out"],
    )
    def test_missing_required_flag_is_a_usage_error(
        self, _env: Path, tmp_path: Path, omit_flag: str
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        argv = [
            "paper",
            "run",
            "--dataset",
            str(dataset),
            "--snapshot",
            str(snapshot),
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
            "--out",
            str(out),
        ]
        # Drop the flag AND its value.
        idx = argv.index(omit_flag)
        del argv[idx : idx + 2]

        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2


class TestPaperRunNoCredentials:
    def test_golden_run_succeeds_with_zero_bithumb_env_vars(
        self,
        _env: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for key in list(os.environ):
            if key.startswith("BITHUMB_"):
                monkeypatch.delenv(key, raising=False)

        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out,
        )
        assert rc == 0, capsys.readouterr()


class TestPaperRunHysteresisPin:
    def test_non_75_hysteresis_refuses(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path, hysteresis_bps="50")
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc = _run(
            dataset=dataset, snapshot=snapshot, config=config,
            state_dir=state_dir, out=out,
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "hysteresis_bps" in err
        assert not out.exists()


class TestPaperRunHelp:
    def test_help_names_all_five_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["paper", "run", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        for flag in ("--dataset", "--snapshot", "--config", "--state-dir", "--out"):
            assert flag in out
