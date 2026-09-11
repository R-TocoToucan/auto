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


class TestProductionWiring:
    """Regression coverage for the fetched CLI wiring.

    Pre-fix bug: the production handler forwarded ``transport=None`` to
    :func:`fetch_candles`, which fail-closed with
    :class:`PublicRestNotVerifiedError`. These tests pin the fix in
    place: the handler MUST construct a real transport and MUST pass
    the frozen ``public_rest`` bucket, while the library-level guard
    stays intact.
    """

    def test_production_wiring_supplies_real_transport_and_verified_bucket(
        self,
        _env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The un-monkey-patched handler must reach `fetch_candles` with:

        * a `transport` that came from `_make_public_rest_transport`
          (production socket factory — swapped here for a MockTransport
          via the module-local seam, no real network I/O),
        * the module-level frozen `public_rest` bucket (identity check),
        * the exact market/unit/start/end from argv,

        and must produce a dataset + SHA-256 sidecar with no
        credentials/JWT/Authorization headers on the wire.
        """
        import bithumb_bot.cli.handlers.research_collect_candles as handler_mod
        import bithumb_bot.market_data.candles as candles_mod
        from bithumb_bot.bithumb_spec import rate_limits

        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        c0 = _valid_row(start, price=100_000_000)

        captured_headers: list[dict[str, str]] = []

        def _http_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(dict(request.headers))
            assert request.url.path.startswith("/v1/candles/minutes/")
            return httpx.Response(200, json=[c0])

        mock_transport = httpx.MockTransport(_http_handler)
        transport_factory_calls: dict[str, int] = {"count": 0}

        def _fake_factory() -> httpx.MockTransport:
            transport_factory_calls["count"] += 1
            return mock_transport

        monkeypatch.setattr(
            handler_mod, "_make_public_rest_transport", _fake_factory
        )

        # Fix `now` so `effective_end` covers our single-candle window.
        monkeypatch.setattr(
            candles_mod,
            "utc_now",
            lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        )

        # Spy on the real fetch_candles to record what the handler
        # passes through (bucket identity, transport identity, args).
        recorded: dict[str, object] = {}
        real_fetch = candles_mod.fetch_candles

        async def _spy_fetch(*args: object, **kwargs: object) -> object:
            recorded["positional"] = args
            recorded["bucket_is_public_rest"] = (
                kwargs.get("bucket") is rate_limits.public_rest
            )
            recorded["transport_is_mock"] = kwargs.get("transport") is mock_transport
            recorded["unit_minutes"] = kwargs.get("unit_minutes")
            recorded["start_utc"] = kwargs.get("start_utc")
            recorded["end_utc"] = kwargs.get("end_utc")
            return await real_fetch(*args, **kwargs)

        monkeypatch.setattr(candles_mod, "fetch_candles", _spy_fetch)

        # No credentials of any Bithumb-related class in this session.
        for name in list(__import__("os").environ):
            if name.startswith("BITHUMB_"):
                monkeypatch.delenv(name, raising=False)

        out = tmp_path / "prod.dataset.json"
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
        assert rc == 0, capsys.readouterr()

        # Production wiring: real transport factory was invoked exactly
        # once, and the resulting transport reached fetch_candles.
        assert transport_factory_calls["count"] == 1
        assert recorded["transport_is_mock"] is True
        # Frozen verified public-REST bucket was supplied (identity).
        assert recorded["bucket_is_public_rest"] is True
        # Request args flowed unchanged from argv to fetch_candles.
        assert recorded["positional"] == ("KRW-BTC",)
        assert recorded["unit_minutes"] == 240
        assert recorded["start_utc"] == start
        assert recorded["end_utc"] == start + timedelta(minutes=240)

        # No credential / JWT header on the wire (public REST only).
        assert captured_headers, "expected at least one intercepted request"
        for headers in captured_headers:
            for key in headers:
                assert key.lower() != "authorization", headers
            for value in headers.values():
                assert "Bearer " not in value, headers

        # Dataset + SHA-256 sidecar written to the requested path.
        assert out.is_file()
        assert out.with_name(out.name + ".sha256").is_file()

    def test_pre_fix_missing_transport_still_refuses(self) -> None:
        """The library-level guard must remain: a caller that omits
        ``transport`` (the exact pre-fix production call shape) still
        fails-closed with `PublicRestNotVerifiedError`. This locks in
        that the fix is CLI-wiring only, not a guard weakening."""
        import asyncio

        from bithumb_bot.bithumb_spec import rate_limits
        from bithumb_bot.errors import PublicRestNotVerifiedError
        from bithumb_bot.market_data.candles import fetch_candles

        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        with pytest.raises(PublicRestNotVerifiedError):
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=240,
                    start_utc=start,
                    end_utc=start + timedelta(minutes=240),
                    bucket=rate_limits.public_rest,
                    transport=None,
                )
            )

    def test_transport_factory_returns_real_async_http_transport(self) -> None:
        """The un-patched factory must return a real `httpx.AsyncHTTPTransport`
        so the seam actually opens sockets in production (not a mock)."""
        from bithumb_bot.cli.handlers.research_collect_candles import (
            _make_public_rest_transport,
        )

        transport = _make_public_rest_transport()
        try:
            assert isinstance(transport, httpx.AsyncHTTPTransport)
        finally:
            import asyncio

            asyncio.run(transport.aclose())
