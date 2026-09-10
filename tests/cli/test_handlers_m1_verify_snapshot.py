"""Tests for `bithumb_bot.cli.handlers.m1_verify_snapshot.handler`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    build_snapshot,
    write_snapshot_with_sidecar,
)
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
    / "example_20260908T012345Z.json"
)


def _make_valid_snapshot(tmp_path: Path) -> Path:
    parsed = OrdersChanceResponse.model_validate(
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    )
    snapshot = build_snapshot(parsed, market="KRW-BTC", fixture_paths=[FIXTURE_PATH])
    target = tmp_path / "snap.json"
    write_snapshot_with_sidecar(snapshot, target)
    return target


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


class TestVerifySnapshotHandler:
    def test_valid_snapshot_returns_zero_and_prints_summary(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _make_valid_snapshot(tmp_path)
        rc = main(["m1", "verify-snapshot", "--snapshot", str(snap)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "KRW-BTC" in out
        assert "verification_status:" in out
        assert "general_fee_rate" in out
        assert "snapshot_sha256[:12]:" in out

    def test_tampered_snapshot_returns_nonzero(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _make_valid_snapshot(tmp_path)
        # Mutate a byte.
        original = snap.read_bytes()
        snap.write_bytes(original[:-1] + b"?")
        rc = main(["m1", "verify-snapshot", "--snapshot", str(snap)])
        assert rc != 0
        err = capsys.readouterr().err
        assert "SidecarHashMismatchError" in err

    def test_missing_snapshot_arg_returns_nonzero(
        self, _env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["m1", "verify-snapshot"])
        assert rc != 0

    def test_handler_source_never_imports_bithumbsecrets(self) -> None:
        """D-89: offline verification handler code MUST NOT construct
        `BithumbSecrets` or call `load_secrets`. Static source check."""
        import inspect

        import bithumb_bot.cli.handlers.m1_verify_snapshot as mod

        src = inspect.getsource(mod)
        assert "BithumbSecrets(" not in src
        assert "load_secrets(" not in src
        assert "from bithumb_bot.secrets" not in src
        assert "import bithumb_bot.secrets" not in src


class TestRequireExecutionReadyFlag:
    """Batch 2 §3: `--require-execution-ready` turns unresolved readiness
    into a non-zero exit; default invocation stays diagnostic (exits 0)."""

    def test_default_invocation_exits_zero_when_readiness_unresolved(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The committed fixture yields an unresolved readiness report
        # (five Batch-1B facts). Default invocation must still exit 0.
        snap = _make_valid_snapshot(tmp_path)
        rc = main(["m1", "verify-snapshot", "--snapshot", str(snap)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "execution_readiness:    unresolved" in out

    def test_require_flag_exits_nonzero_when_readiness_unresolved(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _make_valid_snapshot(tmp_path)
        rc = main(
            [
                "m1",
                "verify-snapshot",
                "--snapshot",
                str(snap),
                "--require-execution-ready",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--require-execution-ready failed" in err
        assert "unresolved" in err

    def test_require_flag_still_reports_invalid_on_tampered_snapshot(
        self, _env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap = _make_valid_snapshot(tmp_path)
        snap.write_bytes(snap.read_bytes()[:-1] + b"?")
        rc = main(
            [
                "m1",
                "verify-snapshot",
                "--snapshot",
                str(snap),
                "--require-execution-ready",
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "SidecarHashMismatchError" in err
