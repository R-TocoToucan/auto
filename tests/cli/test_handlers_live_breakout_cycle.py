"""Focused tests for the ``bt live breakout-cycle`` CLI handler.

Every test operates entirely offline: no network, no real credentials,
no live orders. Dry-run mode is asserted to leave zero credential
material behind and reject every trade credential at load time.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import pytest

from bithumb_bot.cli.dispatcher import dispatch


def _write_gate1(repo_root: Path) -> None:
    (repo_root / "config" / "decisions").mkdir(parents=True, exist_ok=True)
    (repo_root / "config" / "decisions" / "gate1.toml").write_text(
        """\
schema_version = 1
status = "frozen"
approved_at_utc = "2026-09-08T00:00:00Z"
source_commit = "0000000000000000000000000000000000000000"
research_spec_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
execution_spec_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
m1_spec_http_connect_timeout_ms = 5000
m1_spec_http_read_timeout_ms = 15000
m1_spec_http_max_attempts = 3
m1_spec_http_backoff_initial_ms = 500
m1_spec_http_backoff_cap_ms = 5000
m1_spec_http_jitter = "full"
provisional_engineering_notional_krw = "1000000"
"""
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every BITHUMB_* env var before each test."""
    for k in list(os.environ):
        if k.startswith("BITHUMB_"):
            monkeypatch.delenv(k, raising=False)


def test_help_never_loads_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bt live breakout-cycle --help` must not touch load_secrets."""
    called = {"n": 0}

    def spy(*args: object, **kwargs: object) -> None:
        called["n"] += 1
        raise RuntimeError("load_secrets should not be called for --help")

    monkeypatch.setattr("bithumb_bot.secrets.loader.load_secrets", spy)
    with pytest.raises(SystemExit) as exc_info:
        dispatch(["live", "breakout-cycle", "--help"])
    assert exc_info.value.code == 0
    assert called["n"] == 0


def test_dry_run_rejects_when_trade_creds_present_only_via_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `live` capability itself has `trade_cred_prohibited=False`, so
    trade credentials do NOT trip the validator's prohibition path for
    this specific verb. That is the whole point — dry-run still works.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    from bithumb_bot.config.validator import validate

    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "a")
    monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", "b")
    # live/breakout-cycle passes validate() even with trade creds in env.
    result = validate(("live", "breakout-cycle"))
    assert result.ok, result.reason


def test_paper_run_still_rejects_trade_creds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other capability must continue to reject trade credentials."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "a")
    monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", "b")
    from bithumb_bot.config.validator import validate

    for cap in (
        ("paper", "run"),
        ("paper", "breakout-run"),
        ("m1", "fetch-spec"),
        ("research", "backtest"),
    ):
        result = validate(cap)
        assert not result.ok
        assert "trade_credential_prohibited" in result.missing


def test_live_mode_requires_enable_live_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))

    # Both flags must appear together. --mode live without
    # --enable-live-orders → rc=1.
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "does_not_matter"),
        "--snapshot", str(tmp_path / "does_not_matter"),
        "--state-dir", str(tmp_path / "state"),
        "--max-notional-krw", "1000000",
        "--mode", "live",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "enable-live-orders" in err


def test_enable_live_orders_alone_without_mode_live_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "does_not_matter"),
        "--snapshot", str(tmp_path / "does_not_matter"),
        "--state-dir", str(tmp_path / "state"),
        "--max-notional-krw", "1000000",
        "--enable-live-orders",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --mode live" in err


def test_live_mode_missing_credentials_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--mode live --enable-live-orders with no trade creds → clean refusal."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    # A dataset + snapshot that exist enough to reach the credential
    # check; the load_dataset / load_snapshot will refuse first if
    # missing — either way we get rc=1 without a network call.
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "no_such.json"),
        "--snapshot", str(tmp_path / "no_such.json"),
        "--state-dir", str(tmp_path / "state"),
        "--max-notional-krw", "1000000",
        "--mode", "live",
        "--enable-live-orders",
    ])
    assert rc == 1


def test_max_notional_must_be_positive_decimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "does_not_matter"),
        "--snapshot", str(tmp_path / "does_not_matter"),
        "--state-dir", str(tmp_path / "state"),
        "--max-notional-krw", "0",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "max-notional" in err


def test_dry_run_requires_starting_cash_krw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """dry-run without --starting-cash-krw refuses cleanly; no state_dir is created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    state = tmp_path / "state"
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "does_not_matter"),
        "--snapshot", str(tmp_path / "does_not_matter"),
        "--state-dir", str(state),
        "--max-notional-krw", "1000000",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "starting-cash-krw" in err
    assert not state.exists()


def test_live_mode_forbids_starting_cash_krw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    state = tmp_path / "state"
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "does_not_matter"),
        "--snapshot", str(tmp_path / "does_not_matter"),
        "--state-dir", str(state),
        "--max-notional-krw", "1000000",
        "--mode", "live",
        "--enable-live-orders",
        "--starting-cash-krw", "500000",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "forbidden" in err.lower() or "starting-cash-krw" in err
    assert not state.exists()


def test_live_missing_credentials_creates_no_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing credential refusal must NOT create state_dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_gate1(repo)
    monkeypatch.setenv("BITHUMB_BOT_REPO_ROOT", str(repo))
    state = tmp_path / "state"
    rc = dispatch([
        "live", "breakout-cycle",
        "--dataset", str(tmp_path / "no_dataset.json"),
        "--snapshot", str(tmp_path / "no_snapshot.json"),
        "--state-dir", str(state),
        "--max-notional-krw", "1000000",
        "--mode", "live",
        "--enable-live-orders",
    ])
    assert rc == 1
    # State dir is never created because a guard (dataset / snapshot /
    # readiness / creds) refused before mkdir.
    assert not state.exists()


def test_no_withdrawal_endpoint_or_symbol_in_new_modules() -> None:
    """Static sweep: no withdrawal URL, function, or class in the new modules (D-69).

    Docstring / comment references to "no withdrawal" (i.e. explicit
    documentation that the code path is absent, per D-69) are permitted;
    a real code artifact is not.
    """
    import re

    # D-69: withdrawal permission is permanently disabled on every key
    # ever configured for this project; this sweep is the source-level
    # enforcement of that invariant.
    forbidden = [
        re.compile(r"/withdraw", re.IGNORECASE),          # D-69
        re.compile(r"\bdef\s+\w*withdraw\w*", re.IGNORECASE),   # D-69
        re.compile(r"\bclass\s+\w*withdraw\w*", re.IGNORECASE), # D-69
        re.compile(r"WITHDRAW\w*_", re.IGNORECASE),       # D-69
    ]
    for path in (
        Path(__file__).parents[2]
        / "src"
        / "bithumb_bot"
        / "broker"
        / "live.py",
        Path(__file__).parents[2]
        / "src"
        / "bithumb_bot"
        / "order_flow"
        / "live_cycle.py",
        Path(__file__).parents[2]
        / "src"
        / "bithumb_bot"
        / "cli"
        / "handlers"
        / "live_breakout_cycle.py",
        Path(__file__).parents[2]
        / "src"
        / "bithumb_bot"
        / "secrets"
        / "live.py",
    ):
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            # D-69 sweep: no withdrawal artifact permitted in these modules.
            assert not pat.search(text), (
                f"forbidden withdrawal artifact matched {pat.pattern!r} in {path}"
            )
