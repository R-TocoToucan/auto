"""Tests for `bithumb_bot.cli.handlers.m1_fetch_spec.handler`."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest

from bithumb_bot.bithumb_spec import rate_limits
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV

_RAW = {
    "bid_fee": "0.0025",
    "ask_fee": "0.0025",
    "market": {
        "name": "KRW-BTC",
        "order_types": ["limit", "market"],
        "bid": {"currency": "KRW", "min_total": "5000", "price_unit": "1000"},
        "ask": {"currency": "BTC", "min_total": "0.0001", "price_unit": "1000"},
    },
}


@pytest.fixture(autouse=True)
def _refill_sentinel_buckets() -> Any:
    for bucket in (rate_limits.public_rest, rate_limits.private_rest, rate_limits.public_ws):
        bucket._tokens = bucket.capacity  # type: ignore[attr-defined]
        bucket._last_refill = float(bucket._monotonic())  # type: ignore[attr-defined]
    yield


@pytest.fixture()
def _env(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "ak_" + "x" * 32)
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "sk_" + "y" * 32)
    monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestSuccess:
    def test_main_returns_zero_with_mock_transport(
        self, _env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Monkey-patch `create_client` at the client layer so the real
        `fetch_spec` path runs but never touches a real network."""

        def handler_fn(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_RAW)

        transport = httpx.MockTransport(handler_fn)
        real_create_client = None

        def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            from bithumb_bot.bithumb_spec.http_client import create_client as real

            return real(*args, **kwargs)

        with mock.patch(
            "bithumb_bot.bithumb_spec.client.create_client",
            side_effect=_patched,
        ):
            rc = main(["m1", "fetch-spec", "--market", "KRW-BTC"])
        assert rc == 0
        out = capsys.readouterr().out
        # Three output paths printed.
        assert "snapshot:" in out
        assert "sanitized fixture:" in out
        assert "verification bundle:" in out


class TestTradeCredRefused:
    def test_trade_cred_env_returns_nonzero_and_no_client_constructed(
        self,
        tmp_path: Path,
        tmp_gate1_toml: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "ak_" + "x" * 32)
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "sk_" + "y" * 32)
        monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "TRADE-SENTINEL-VALUE-32bytes-XXX")
        monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", "TRADE-SECRET-SENTINEL-VALUE-XXXX")
        monkeypatch.chdir(tmp_path)

        with mock.patch("httpx.AsyncClient") as async_client_mock:
            rc = main(["m1", "fetch-spec", "--market", "KRW-BTC"])
        assert rc != 0
        async_client_mock.assert_not_called()
        err = capsys.readouterr().err
        # Sentinel MUST NOT appear anywhere in stderr.
        assert "TRADE-SENTINEL-VALUE" not in err
        assert "TRADE-SECRET-SENTINEL" not in err
