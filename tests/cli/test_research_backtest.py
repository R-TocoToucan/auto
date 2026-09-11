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
    starting_cash_krw: str = "100000",
    max_notional_krw: str = "100000",
) -> Path:
    # Defaults sit at the Gate-1 provisional engineering notional
    # (100_000 KRW) — the handler refuses any TOML value above it
    # pre-Gate-2. Tests that need a violation override the fields.
    body = f"""\
[backtest]
starting_cash_krw = "{starting_cash_krw}"
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
max_notional_krw = "{max_notional_krw}"
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
        # Engineering-smoke identity.
        assert parsed["schema_version"] == 2
        assert parsed["run_purpose"] == "engineering_smoke"
        assert parsed["selection_eligible"] is False
        assert parsed["holdout_eligible"] is False
        # Provenance.
        assert "gate1_source_commit" in parsed["provenance"]
        assert "gate1_file_sha256" in parsed["provenance"]
        # Input attestations
        inputs = parsed["inputs"]
        assert inputs["dataset_sha256"] == sha256_hex(dataset.read_bytes())
        assert inputs["snapshot_sha256"] == sha256_hex(snapshot.read_bytes())
        assert inputs["config_sha256"] == sha256_hex(config.read_bytes())
        # Absolute paths must not appear in the report.
        assert "dataset_path" not in inputs
        assert "snapshot_path" not in inputs
        assert "config_path" not in inputs
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
        # Backtest validity + one closed trade with distinct count semantics.
        assert parsed["backtest"]["valid"] is True
        assert parsed["backtest"]["closed_trade_count"] == 1
        assert parsed["backtest"]["position_entry_count"] == 1
        assert parsed["backtest"]["ledger_entry_count"] == 2
        # Full ledger + equity curve persisted.
        assert isinstance(parsed["ledger"], list)
        assert len(parsed["ledger"]) == 2
        assert isinstance(parsed["equity_curve"], list)
        assert len(parsed["equity_curve"]) >= 1
        # Performance report has evaluation-valid strategy_net_return.
        perf = parsed["performance"]
        assert perf["evaluation_invalid_reason"] is None
        assert perf["strategy_net_return"] is not None
        # Exact-Decimal serialization: strategy_net_return is a string.
        assert isinstance(perf["strategy_net_return"], str)
        # Renamed metric.
        assert "fee_addback_return" in perf
        assert "gross_before_fees_after_slippage_return" not in perf
        # Distinct count fields.
        assert perf["closed_trade_count"] == 1
        assert perf["position_entry_count"] == 1
        assert perf["ledger_entry_count"] == 2
        # Execution assumption carried explicitly.
        assert parsed["execution"]["allow_provisional_fee_model"] is True
        assert parsed["execution"]["simulation_quantity_quantum"] == "0.00000001"

    def test_report_bytes_deterministic(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two runs with byte-equal inputs at the same generated instant
        produce byte-identical reports.

        The report includes a ``generated_at_utc`` stamp; the snapshot
        includes a ``retrieved_at_utc`` stamp; freezing every wall-clock
        entry point makes the comparison meaningful.
        """
        import bithumb_bot.artifact.timestamps as ts_mod
        import bithumb_bot.bithumb_spec.snapshot as snap_mod
        import bithumb_bot.cli.handlers.research_backtest as handler_mod

        frozen = datetime(2026, 9, 10, 15, 0, 0, tzinfo=UTC)

        def _run(run_dir: Path) -> bytes:
            run_dir.mkdir(parents=True, exist_ok=True)
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr(ts_mod, "utc_now", lambda: frozen)
                mp.setattr(snap_mod, "utc_now", lambda: frozen, raising=False)
                mp.setattr(handler_mod, "utc_now", lambda: frozen, raising=False)
                dataset = _write_dataset(run_dir)
                snapshot = _write_snapshot(run_dir)
                config = _write_config(run_dir)
                out = run_dir / "report.json"
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
        # No absolute paths land in the report, so byte-equality holds
        # directly (deterministic canonical JSON + SHA-256 sidecar).
        assert bytes_a == bytes_b


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
        # `holdout` may appear only in refusal-message text that
        # describes the deferred selection/holdout gap; there must be
        # no reserved-handler dispatch and no broker/secret path.
        assert "reserved_handler" not in src
        assert "BithumbSecrets(" not in src
        assert "load_secrets(" not in src


# ---------------------------------------------------------------------------
# Research Smoke Correctness and Report Auditability regression tests
# ---------------------------------------------------------------------------


class TestEngineeringSmokeSeparation:
    """Pre-Gate-2 runs are engineering smoke, never selection-ready."""

    def test_arbitrary_toml_cap_cannot_bypass_gate1_provisional(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """10M starting cash + 100M configured cap must fail closed.

        The handler compares the TOML ``max_notional_krw`` against Gate 1's
        frozen ``provisional_engineering_notional_krw`` (100_000 KRW). An
        arbitrary TOML value cannot promote a run to selection-ready.
        """
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(
            tmp_path,
            starting_cash_krw="10000000",
            max_notional_krw="100000000",
        )
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
        assert rc != 0
        err = capsys.readouterr().err
        assert "provisional_engineering_notional_krw" in err
        assert "gate2_validated_cap" in err
        assert not out.exists()

    def test_intended_order_above_100k_fails_closed(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A 200_000 KRW sleeve intent must fail closed even if the
        configured cap sits at the provisional 100_000 boundary.

        The intended pre-fee order (cash * sleeve / (1 + fee_bid))
        exceeds 100_000 KRW → refuse.
        """
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(
            tmp_path,
            starting_cash_krw="200000",
            max_notional_krw="100000",
        )
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
        assert "intended pre-fee order" in err
        assert "provisional_engineering_notional_krw" in err

    def test_smoke_run_at_or_below_100k_reports_smoke_identity(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """At-cap engineering smoke still emits the smoke identity."""
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        # 100_000 KRW cash / (1 + 0.0025) ≈ 99_750 < 100_000 ✓
        config = _write_config(
            tmp_path,
            starting_cash_krw="100000",
            max_notional_krw="100000",
        )
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
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed["run_purpose"] == "engineering_smoke"
        assert parsed["selection_eligible"] is False
        assert parsed["holdout_eligible"] is False


class TestReportPersistedAuditFields:
    """The persisted report contains ledger, equity curve, and audit fields."""

    def test_persisted_report_contains_full_ledger_and_equity_curve(
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
        parsed = json.loads(out.read_text(encoding="utf-8"))
        # Full ledger: two entries (one buy, one sell); Money fields
        # serialize as nested `{"value": "<decimal-str>"}` via asdict.
        ledger = parsed["ledger"]
        assert len(ledger) == 2
        sides = [e["side"] for e in ledger]
        assert sides == ["buy", "sell"]
        for e in ledger:
            assert isinstance(e["fill_price"]["value"], str)
        # Full equity curve.
        curve = parsed["equity_curve"]
        assert len(curve) >= 1
        for point in curve:
            assert isinstance(point["cash_krw"]["value"], str)
            assert isinstance(point["net_liquidation_equity_krw"]["value"], str)
        # Distinct count fields.
        bt = parsed["backtest"]
        assert bt["ledger_entry_count"] == 2
        assert bt["position_entry_count"] == 1
        assert bt["closed_trade_count"] == 1

    def test_pending_intent_persisted_when_present(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A LONG signal on the last candle leaves a pending intent —
        it must be recorded in the report along with pending_intent_count.
        """
        # Build a dataset where the LAST candle (index 3) emits a LONG
        # signal so the runner records a pending intent (there is no
        # next candle for the fill). At i=2 close==sma → CASH; at i=3
        # close > sma → LONG, buy pending, no candle 4.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        step = timedelta(minutes=240)
        prices = [
            (100_000_000, 100_000_000, 100_000_000, 100_000_000),
            (100_000_000, 100_000_000, 100_000_000, 100_000_000),
            (100_000_000, 100_000_000, 100_000_000, 100_000_000),
            (100_000_000, 105_000_000, 100_000_000, 104_000_000),  # LONG
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
                    volume="1",
                    quote_volume="100000000",
                )
            )
        dataset_obj = CandleDataset(
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
                page_cursors_kst=[],
                effective_end_utc=(candles[-1].open_time_utc + step).isoformat(),
            ),
        )
        dataset = tmp_path / "dataset_pending.json"
        write_dataset_with_sidecar(dataset_obj, dataset)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(tmp_path)
        out = tmp_path / "report_pending.json"
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
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed["backtest"]["pending_intent_count"] == 1
        pending = parsed["backtest"]["pending_intent"]
        assert pending is not None
        assert pending["side"] == "buy"
        assert isinstance(pending["requested_notional_krw"], str)

    def test_persisted_report_contains_no_absolute_windows_path(
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
        raw = out.read_text(encoding="utf-8")
        # No Windows-style absolute paths (drive-letter prefix).
        assert "C:\\" not in raw
        assert "C:/" not in raw
        # No `Users\<name>` fragment.
        assert "Users\\" not in raw
        assert "Users/" not in raw
        parsed = json.loads(raw)
        # No path fields at all — only hashes.
        assert "dataset_path" not in parsed["inputs"]

    def test_fee_addback_return_matches_documented_formula(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`fee_addback_return` = (end_net_liq + total_fees + est_liq_fee)
        / starting_equity - 1.
        """
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
        perf = json.loads(out.read_text(encoding="utf-8"))["performance"]
        start = Decimal(perf["starting_equity_krw"])
        end_nl = Decimal(perf["ending_net_liquidation_equity_krw"])
        fees = Decimal(perf["total_actual_fees_krw"])
        est_liq = Decimal(perf["estimated_final_liquidation_fee_krw"])
        expected = (end_nl + fees + est_liq) / start - Decimal("1")
        assert Decimal(perf["fee_addback_return"]) == expected


class TestFixtureSidecarStableAcrossPlatforms:
    """The tracked fixture sidecar must verify from clean git-archive bytes."""

    def test_git_archive_bytes_match_tracked_sidecar(self) -> None:
        import subprocess
        import tarfile
        import tempfile

        repo_root = Path(__file__).parent.parent.parent
        rel_json = (
            "tests/fixtures/bithumb/sanitized/orders_chance/"
            "observed_krw_btc.json"
        )
        rel_sidecar = rel_json + ".sha256"
        sidecar_bytes = (repo_root / rel_sidecar).read_bytes()
        recorded_hex = sidecar_bytes.decode("utf-8").strip().split()[0]
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive.tar"
            subprocess.run(
                [
                    "git",
                    "archive",
                    "HEAD",
                    # `--worktree-attributes` honors an uncommitted
                    # `.gitattributes` too — the assertion holds both
                    # pre- and post-commit of the attribute rule.
                    "--worktree-attributes",
                    "--format=tar",
                    f"-o{archive}",
                    "--",
                    rel_json,
                ],
                cwd=str(repo_root),
                check=True,
                capture_output=True,
            )
            with tarfile.open(archive, "r") as tf:
                member = tf.getmember(rel_json)
                fh = tf.extractfile(member)
                assert fh is not None
                archived_bytes = fh.read()
        archived_hex = sha256_hex(archived_bytes)
        assert archived_hex == recorded_hex, (
            f"tracked sidecar {recorded_hex!r} does not match clean "
            f"git-archive bytes {archived_hex!r}; check .gitattributes"
        )
