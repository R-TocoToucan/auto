"""Offline tests for `bt research collect-candles`.

Every HTTP request flows through `httpx.MockTransport`. No real
network I/O.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx
import pytest

from bithumb_bot.bithumb_spec import rate_limits
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV


@pytest.fixture(autouse=True)
def _refill_buckets() -> None:
    """Reset per-channel buckets so sequential tests never wait."""
    for bucket in (rate_limits.public_rest,):
        bucket._tokens = bucket.capacity  # type: ignore[attr-defined]
        bucket._last_refill = float(bucket._monotonic())  # type: ignore[attr-defined]


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


def _valid_row(open_time_utc: datetime, price: int = 100_000_000) -> dict:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": open_time_utc.replace(tzinfo=None).isoformat(),
        "candle_date_time_kst": (open_time_utc.astimezone(timezone_kst()).replace(tzinfo=None)).isoformat(),
        "opening_price": price,
        "high_price": price,
        "low_price": price,
        "trade_price": price,
        "candle_acc_trade_price": 150_000_000,
        "candle_acc_trade_volume": "1.5",
        "unit": 240,
    }


def timezone_kst():
    from datetime import timezone as _tz
    return _tz(timedelta(hours=9))


def _mock_transport_ok(candles: list[dict]) -> httpx.MockTransport:
    """Return a transport that responds with `candles` (descending)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/v1/candles/minutes/")
        return httpx.Response(200, json=candles)

    return httpx.MockTransport(handler)


def _mock_transport_malformed() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not a list, not json shaped right")

    return httpx.MockTransport(handler)


@pytest.fixture()
def _patch_fetch_transport() -> mock.MagicMock:
    """Return a MagicMock that lets tests set the transport for one call."""
    return mock.MagicMock()


class TestCollectCandlesHappy:
    def test_successful_collection_writes_dataset_and_sidecar(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two 240-minute candles descending, both inside the requested range.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        c0 = _valid_row(start, price=100_000_000)
        c1 = _valid_row(start + timedelta(minutes=240), price=101_000_000)
        transport = _mock_transport_ok([c1, c0])  # descending

        # Inject transport through the module patch.
        import bithumb_bot.market_data.candles as candles_mod

        real_fetch = candles_mod.fetch_candles

        async def _fetch_with_transport(*args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["transport"] = transport
            # `now_utc` is not injected via kwargs from the CLI; provide it here.
            kwargs["now_utc"] = lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            return await real_fetch(*args, **kwargs)

        monkeypatch.setattr(candles_mod, "fetch_candles", _fetch_with_transport)
        # Handler imports from module too — patch there.
        import bithumb_bot.cli.handlers.research_collect_candles as handler_mod

        monkeypatch.setattr(
            handler_mod, "fetch_candles", _fetch_with_transport, raising=False
        )

        out = tmp_path / "dataset.json"
        rc = main(
            [
                "research",
                "collect-candles",
                "--market",
                "KRW-BTC",
                "--unit-minutes",
                "240",
                "--start-utc",
                start.isoformat(),
                "--end-utc",
                (start + timedelta(minutes=480)).isoformat(),
                "--out",
                str(out),
            ]
        )
        assert rc == 0, capsys.readouterr()
        assert out.is_file()
        assert out.with_name(out.name + ".sha256").is_file()
        stdout = capsys.readouterr().out
        assert "dataset:" in stdout
        assert "candle_count:           2" in stdout
        assert "missing_interval_count: 0" in stdout
        assert "dataset_sha256[:12]:" in stdout


class TestCollectCandlesRefusals:
    def test_malformed_response_refused(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = _mock_transport_malformed()
        import bithumb_bot.market_data.candles as candles_mod

        real = candles_mod.fetch_candles

        async def _fetch(*a, **k):  # type: ignore[no-untyped-def]
            k["transport"] = transport
            k["now_utc"] = lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            return await real(*a, **k)

        monkeypatch.setattr(candles_mod, "fetch_candles", _fetch)
        import bithumb_bot.cli.handlers.research_collect_candles as handler_mod

        monkeypatch.setattr(handler_mod, "fetch_candles", _fetch, raising=False)

        out = tmp_path / "dataset.json"
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rc = main(
            [
                "research",
                "collect-candles",
                "--market",
                "KRW-BTC",
                "--unit-minutes",
                "240",
                "--start-utc",
                start.isoformat(),
                "--end-utc",
                (start + timedelta(minutes=240)).isoformat(),
                "--out",
                str(out),
            ]
        )
        assert rc != 0
        err = capsys.readouterr().err
        assert "fetch refused" in err
        assert not out.exists()

    def test_overwrite_refused(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First run writes; second run must refuse.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        c0 = _valid_row(start, price=100_000_000)
        transport = _mock_transport_ok([c0])

        import bithumb_bot.market_data.candles as candles_mod

        real = candles_mod.fetch_candles

        async def _fetch(*a, **k):  # type: ignore[no-untyped-def]
            k["transport"] = transport
            k["now_utc"] = lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            return await real(*a, **k)

        monkeypatch.setattr(candles_mod, "fetch_candles", _fetch)
        import bithumb_bot.cli.handlers.research_collect_candles as handler_mod

        monkeypatch.setattr(handler_mod, "fetch_candles", _fetch, raising=False)

        out = tmp_path / "dataset.json"
        argv = [
            "research",
            "collect-candles",
            "--market",
            "KRW-BTC",
            "--unit-minutes",
            "240",
            "--start-utc",
            start.isoformat(),
            "--end-utc",
            (start + timedelta(minutes=240)).isoformat(),
            "--out",
            str(out),
        ]
        rc = main(argv)
        assert rc == 0, capsys.readouterr()
        rc2 = main(argv)
        assert rc2 != 0
        err = capsys.readouterr().err
        assert "write refused" in err or "already" in err.lower()


class TestNoBrokerOrHoldoutSideEffect:
    def test_handler_source_contains_no_broker_or_holdout_wiring(self) -> None:
        """Static discipline: the collect-candles handler MUST NOT import
        broker code, reserved handlers, or the holdout capability."""
        import inspect

        import bithumb_bot.cli.handlers.research_collect_candles as mod

        src = inspect.getsource(mod)
        assert "bithumb_bot.broker" not in src
        assert "holdout" not in src.lower()
        assert "reserved_handler" not in src
        assert "BithumbSecrets(" not in src
        assert "load_secrets(" not in src

    def test_ambient_proxy_env_vars_are_ignored(self) -> None:
        """`create_client` sets `trust_env=False` so ambient HTTP_PROXY
        cannot silently reroute exchange traffic."""
        from bithumb_bot.bithumb_spec.http_client import create_client

        client = create_client(
            base_url="https://api.bithumb.com",
            connect_timeout_s=5.0,
            read_timeout_s=15.0,
        )
        try:
            assert client._trust_env is False
        finally:
            import asyncio

            asyncio.run(client.aclose())
