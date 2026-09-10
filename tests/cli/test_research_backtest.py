"""Offline tests for `bt research backtest`.

Every scenario runs offline through committed fixtures. No network,
no credentials, no broker code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import sha256_hex
from bithumb_bot.bithumb_spec.sanitize import sanitize_orders_chance
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    build_snapshot,
    write_snapshot_with_sidecar,
)
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import (
    CandleDataset,
    DatasetProvenance,
    write_dataset_with_sidecar,
)


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
    monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
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
    step = timedelta(minutes=240)
    prices = [
        (100_000_000, 100_000_000, 100_000_000, 100_000_000),
        (100_000_000, 100_000_000, 100_000_000, 100_000_000),
        (100_000_000, 105_000_000, 99_000_000, 104_000_000),  # LONG cross
        (104_000_000, 106_000_000, 103_000_000, 105_000_000),
        (105_000_000, 105_000_000, 100_000_000, 100_000_000),  # CASH cross
        (100_000_000, 101_000_000, 99_000_000, 100_000_000),
    ]
    candles: list[Candle] = []
    for i, (o, h, low, c) in enumerate(prices):
        candles.append(
            Candle(
                market="KRW-BTC",
                unit_minutes=240,
                open_time_utc=start + i * step,
                open=o,
                high=h,
                low=low,
                close=c,
                volume="1.5",
                quote_volume="150000000",
            )
        )
    dataset = CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=240,
        requested_start_utc=candles[0].open_time_utc.isoformat(),
        requested_end_utc=(candles[-1].open_time_utc + step).isoformat(),
        fetched_at_utc="2026-09-10T00:00:00+00:00",
        candles=candles,
        missing_intervals_utc=[],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=["2026-01-02T09:00:00+09:00"],
            effective_end_utc=(candles[-1].open_time_utc + step).isoformat(),
        ),
    )
    target = tmp_path / "dataset.json"
    write_dataset_with_sidecar(dataset, target)
    return target


def _write_config(
    tmp_path: Path,
    *,
    allow_provisional: bool = True,
    quantum: str = "0.00000001",
    drop_key: str | None = None,
) -> Path:
    body = f"""\
[backtest]
starting_cash_krw = "10000000"
target_sleeve_fraction = "1.0"
protective_stop_fraction = "0.10"

[strategy]
rule_id = "price_over_sma"
ma_type = "SMA"
lookback_candles = 3
warmup_candles = 3
unit_minutes = 240
market = "KRW-BTC"

[execution]
slippage_bps_per_side = "50"
max_notional_krw = "100000000"
allow_provisional_fee_model = {"true" if allow_provisional else "false"}
simulation_quantity_quantum = "{quantum}"
"""
    if drop_key is not None:
        lines = [ln for ln in body.splitlines() if not ln.startswith(f"{drop_key} ")]
        body = "\n".join(lines) + "\n"
    path = tmp_path / "backtest.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestBacktestHappyPath:
    def test_end_to_end_flow_produces_report(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        out = tmp_path / "report.json"

        rc = main(
            [
                "research",
                "backtest",
                "--dataset",
                str(dataset),
                "--snapshot",
                str(snapshot),
                "--config",
                str(config),
                "--out",
                str(out),
            ]
        )
        assert rc == 0, capsys.readouterr()
        assert out.is_file()
        assert out.with_name(out.name + ".sha256").is_file()

        parsed = json.loads(out.read_text(encoding="utf-8"))
        # Input attestations
        inputs = parsed["inputs"]
        assert inputs["dataset_sha256"] == sha256_hex(dataset.read_bytes())
        assert inputs["snapshot_sha256"] == sha256_hex(snapshot.read_bytes())
        assert inputs["config_sha256"] == sha256_hex(config.read_bytes())
        # Readiness separation
        readiness = parsed["readiness"]
        assert readiness["research_simulation_readiness"] == "ready"
        assert readiness["live_execution_readiness"] == "unresolved"
        # Live-only unresolved facts surfaced
        for expected in (
            "market_buy_fee_reservation",
            "default_step",
            "market_buy_price_support",
            "market_sell_market_support",
            "live_order_acceptance",
        ):
            assert expected in readiness["live_only_unresolved_facts"]
        # Backtest validity + one closed trade
        assert parsed["backtest"]["valid"] is True
        assert parsed["backtest"]["trade_count"] == 1
        # Performance report has evaluation-valid strategy_net_return.
        perf = parsed["performance"]
        assert perf["evaluation_invalid_reason"] is None
        assert perf["strategy_net_return"] is not None
        # Exact-Decimal serialization: strategy_net_return is a string.
        assert isinstance(perf["strategy_net_return"], str)
        # Execution assumption carried explicitly.
        assert parsed["execution"]["allow_provisional_fee_model"] is True
        assert parsed["execution"]["simulation_quantity_quantum"] == "0.00000001"

    def test_report_bytes_deterministic(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two runs with byte-equal inputs at the same generated instant
        produce byte-identical reports.

        The report includes a ``generated_at_utc`` stamp, so we freeze
        the clock to make the comparison meaningful.
        """
        import bithumb_bot.artifact.timestamps as ts_mod
        import bithumb_bot.cli.handlers.research_backtest as handler_mod

        frozen = datetime(2026, 9, 10, 15, 0, 0, tzinfo=UTC)

        def _run(run_dir: Path) -> bytes:
            run_dir.mkdir(parents=True, exist_ok=True)
            dataset = _write_dataset(run_dir)
            snapshot = _write_snapshot(run_dir)
            config = _write_config(run_dir)
            out = run_dir / "report.json"
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr(ts_mod, "utc_now", lambda: frozen)
                mp.setattr(handler_mod, "utc_now", lambda: frozen, raising=False)
                rc = main(
                    [
                        "research",
                        "backtest",
                        "--dataset",
                        str(dataset),
                        "--snapshot",
                        str(snapshot),
                        "--config",
                        str(config),
                        "--out",
                        str(out),
                    ]
                )
            assert rc == 0, capsys.readouterr()
            return out.read_bytes()

        bytes_a = _run(tmp_path / "run_a")
        bytes_b = _run(tmp_path / "run_b")
        # Strip absolute-path components (dataset_path etc.) before comparing.
        parsed_a = json.loads(bytes_a.decode("utf-8"))
        parsed_b = json.loads(bytes_b.decode("utf-8"))
        for parsed in (parsed_a, parsed_b):
            parsed["inputs"]["dataset_path"] = "<path>"
            parsed["inputs"]["snapshot_path"] = "<path>"
            parsed["inputs"]["config_path"] = "<path>"
        assert parsed_a == parsed_b


class TestBacktestRefusals:
    def test_invalid_dataset_sidecar(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        # Tamper.
        dataset.write_bytes(dataset.read_bytes()[:-1] + b"?")
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        rc = main(
            [
                "research",
                "backtest",
                "--dataset",
                str(dataset),
                "--snapshot",
                str(snapshot),
                "--config",
                str(config),
                "--out",
                str(tmp_path / "report.json"),
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "dataset refused" in err
        assert "SidecarHashMismatchError" in err

    def test_invalid_snapshot_sidecar(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        snapshot.write_bytes(snapshot.read_bytes()[:-1] + b"?")
        config = _write_config(tmp_path)
        rc = main(
            [
                "research",
                "backtest",
                "--dataset",
                str(dataset),
                "--snapshot",
                str(snapshot),
                "--config",
                str(config),
                "--out",
                str(tmp_path / "report.json"),
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "snapshot refused" in err
        assert "SidecarHashMismatchError" in err

    def test_missing_config_field(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        # Drop `simulation_quantity_quantum` line.
        config = _write_config(tmp_path, drop_key="simulation_quantity_quantum")
        rc = main(
            [
                "research",
                "backtest",
                "--dataset",
                str(dataset),
                "--snapshot",
                str(snapshot),
                "--config",
                str(config),
                "--out",
                str(tmp_path / "report.json"),
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "config refused" in err
        assert "simulation_quantity_quantum" in err

    def test_research_readiness_refusal(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Config without provisional-fee opt-in refuses research readiness."""
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        # `market_buy_fee_reservation` is `provisional_documented` on the
        # observed KRW-BTC snapshot; without opt-in the research surface
        # cannot be ready.
        config = _write_config(tmp_path, allow_provisional=False)
        rc = main(
            [
                "research",
                "backtest",
                "--dataset",
                str(dataset),
                "--snapshot",
                str(snapshot),
                "--config",
                str(config),
                "--out",
                str(tmp_path / "report.json"),
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "research_simulation_readiness=unresolved" in err
        assert "market_buy_fee_reservation" in err

    def test_report_overwrite_refused(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        out = tmp_path / "report.json"
        argv = [
            "research",
            "backtest",
            "--dataset",
            str(dataset),
            "--snapshot",
            str(snapshot),
            "--config",
            str(config),
            "--out",
            str(out),
        ]
        rc = main(argv)
        assert rc == 0, capsys.readouterr()
        # Second run must refuse.
        rc2 = main(argv)
        assert rc2 != 0
        err = capsys.readouterr().err
        assert "refuse to overwrite" in err or "already" in err.lower()


class TestNoBrokerOrHoldout:
    def test_handler_source_contains_no_broker_or_holdout(self) -> None:
        import inspect

        import bithumb_bot.cli.handlers.research_backtest as mod

        src = inspect.getsource(mod)
        assert "bithumb_bot.broker" not in src
        assert "holdout" not in src.lower()
        assert "reserved_handler" not in src
        assert "BithumbSecrets(" not in src
        assert "load_secrets(" not in src
