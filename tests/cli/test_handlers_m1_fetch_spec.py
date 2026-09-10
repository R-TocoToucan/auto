"""Tests for `bithumb_bot.cli.handlers.m1_fetch_spec.handler`."""

from __future__ import annotations

from http import HTTPStatus
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


class TestSanitizedHttpErrorLogging:
    """On `httpx.HTTPStatusError`, the handler MUST log only the HTTP
    status code and a strictly-sanitized Bithumb error name (or the
    sentinel ``"unknown"``). The response body, headers, JWT, keys,
    query hash, balances and any other detail are NEVER emitted.
    """

    _SECRET_BODY_MARKERS = (
        # error message text — MUST NOT appear in log records
        "internal-detail-leak-42",
        # sentinel body/field names — MUST NOT appear either
        "balance-SENSITIVE",
        "auth-secret-material",
    )

    def _run_with_error_response(
        self,
        _env: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_body: dict[str, Any] | str | None,
        status_code: int = 401,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Drive `bt m1 fetch-spec` against a mock transport that
        returns ``status_code`` with ``error_body``, and capture the
        structlog records emitted by the handler."""

        # Give the response header a would-be-sensitive value; the
        # sanitized logger MUST NOT surface it either.
        def handler_fn(request: httpx.Request) -> httpx.Response:
            if isinstance(error_body, dict):
                return httpx.Response(
                    status_code,
                    json=error_body,
                    headers={"x-auth-echo": "auth-secret-material"},
                )
            if isinstance(error_body, str):
                return httpx.Response(
                    status_code,
                    text=error_body,
                    headers={"x-auth-echo": "auth-secret-material"},
                )
            return httpx.Response(status_code, headers={"x-auth-echo": "auth-secret-material"})

        transport = httpx.MockTransport(handler_fn)

        def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            from bithumb_bot.bithumb_spec.http_client import create_client as real

            return real(*args, **kwargs)

        from structlog.testing import capture_logs

        with (
            capture_logs() as captured,
            mock.patch(
                "bithumb_bot.bithumb_spec.client.create_client",
                side_effect=_patched,
            ),
        ):
            rc = main(["m1", "fetch-spec", "--market", "KRW-BTC"])

        return rc, [dict(rec) for rec in captured]

    def test_bithumb_named_error_is_logged_verbatim(
        self,
        _env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc, captured = self._run_with_error_response(
            _env,
            monkeypatch,
            error_body={
                "error": {
                    "name": "jwt_verification",
                    "message": "internal-detail-leak-42 balance-SENSITIVE",
                },
                "balance": "balance-SENSITIVE",
            },
            status_code=401,
        )
        assert rc == 1
        # Exactly one m1.fetch-spec.http_status_error record captured.
        http_records = [r for r in captured if r.get("event") == "m1.fetch-spec.http_status_error"]
        assert len(http_records) == 1
        rec = http_records[0]
        assert rec["http_status"] == HTTPStatus.UNAUTHORIZED
        assert rec["bithumb_error_name"] == "jwt_verification"
        # Nothing else about the response leaks into the record.
        rendered = repr(rec)
        for marker in self._SECRET_BODY_MARKERS:
            assert (
                marker not in rendered
            ), f"marker {marker!r} leaked into structured log: {rendered!r}"
        # And nothing leaks into stderr either.
        err = capsys.readouterr().err
        for marker in self._SECRET_BODY_MARKERS:
            assert marker not in err

    def test_malformed_error_body_collapses_to_unknown(
        self,
        _env: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Non-JSON body with a would-be-sensitive substring.
        rc, captured = self._run_with_error_response(
            _env,
            monkeypatch,
            error_body="internal-detail-leak-42 balance-SENSITIVE",
            status_code=502,
        )
        assert rc == 1
        http_records = [r for r in captured if r.get("event") == "m1.fetch-spec.http_status_error"]
        assert len(http_records) == 1
        rec = http_records[0]
        assert rec["http_status"] == HTTPStatus.BAD_GATEWAY
        assert rec["bithumb_error_name"] == "unknown"
        rendered = repr(rec)
        for marker in self._SECRET_BODY_MARKERS:
            assert marker not in rendered

    def test_hostile_error_name_is_rejected(
        self,
        _env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A name with whitespace / non-allowlisted chars MUST NOT be
        # copied into the log line — it collapses to "unknown".
        rc, captured = self._run_with_error_response(
            _env,
            monkeypatch,
            error_body={
                "error": {
                    "name": "line-break\nauth-secret-material",
                    "message": "internal-detail-leak-42",
                }
            },
            status_code=400,
        )
        assert rc == 1
        http_records = [r for r in captured if r.get("event") == "m1.fetch-spec.http_status_error"]
        assert len(http_records) == 1
        rec = http_records[0]
        assert rec["bithumb_error_name"] == "unknown"
        rendered = repr(rec)
        assert "auth-secret-material" not in rendered
        assert "\n" not in rec["bithumb_error_name"]


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
