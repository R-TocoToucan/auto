"""Focused CLI tests for the paper-only notional-scale patch.

Covers three properties:

1. ``bt paper run`` accepts starting_cash_krw="10000000",
   target_sleeve_fraction="1.0", max_notional_krw="100000000" —
   the pre-Gate-2 provisional-cap comparisons are relaxed here.
2. The persisted report carries
   ``notional_scale_status="unvalidated_engineering"``, the
   configured max_notional, AND Gate 1's provisional notional.
3. ``bt research backtest`` continues to REFUSE a TOML cap above
   Gate 1's ``provisional_engineering_notional_krw`` — the relaxation
   is scoped to the paper handler only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bithumb_bot.cli.main import main

from tests.cli.test_handlers_paper_run import (  # noqa: F401
    _env,
    _run,
    _write_config,
    _write_dataset,
    _write_snapshot,
)


class TestPaperRunNotionalScaleRelaxation:
    def test_paper_accepts_10m_cash_100m_configured_cap(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(
            tmp_path,
            starting_cash_krw="10000000",
            max_notional_krw="100000000",
        )
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc = _run(
            dataset=dataset,
            snapshot=snapshot,
            config=config,
            state_dir=state_dir,
            out=out,
        )
        assert rc == 0, capsys.readouterr()
        assert out.is_file()

    def test_report_records_unvalidated_label_and_both_notionals(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(
            tmp_path,
            starting_cash_krw="10000000",
            max_notional_krw="100000000",
        )
        state_dir = tmp_path / "state"
        out = tmp_path / "report.json"

        rc = _run(
            dataset=dataset,
            snapshot=snapshot,
            config=config,
            state_dir=state_dir,
            out=out,
        )
        assert rc == 0, capsys.readouterr()

        parsed = json.loads(out.read_text(encoding="utf-8"))

        assert parsed["notional_scale_status"] == "unvalidated_engineering"
        assert parsed["run_purpose"] == "engineering_smoke"
        assert parsed["selection_eligible"] is False
        assert parsed["holdout_eligible"] is False

        assert parsed["execution"]["max_notional_krw"] == "100000000"
        assert (
            parsed["provenance"]["provisional_engineering_notional_krw"]
            == "100000"
        )


class TestResearchBacktestStillRejectsAboveGate1Cap:
    """The paper relaxation must NOT leak into `bt research backtest`."""

    def test_research_backtest_refuses_100m_configured_cap(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `_env`, `_write_dataset`, `_write_snapshot`, `_write_config`
        # imported from the paper test module all use `KRW-BTC` and the
        # 240-minute unit_minutes the research handler also expects.
        dataset = _write_dataset(tmp_path)
        snapshot = _write_snapshot(tmp_path)
        config = _write_config(
            tmp_path,
            starting_cash_krw="10000000",
            max_notional_krw="100000000",
        )
        out = tmp_path / "research_report.json"

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
        assert not out.exists()
