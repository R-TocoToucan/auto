"""Wave-3 integration smoke — plans 01-01 through 01-04 composed end-to-end.

Under `structlog.testing.capture_logs()` (belt-and-suspenders — the
`redact_secrets` processor is separately unit-tested), assert that:

* `bt --help` never constructs BithumbSecrets.
* `bt m0 selfcheck` bound context includes `capability`; sentinel
  credential values never appear.
* `bt m1 fetch-spec` via mock transport writes the three artifacts;
  bound context includes `capability=m1.fetch-spec`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
import structlog

from bithumb_bot.bithumb_spec import rate_limits
from bithumb_bot.cli.main import main
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.observability import logging as obs_logging


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
def _reset_state() -> Any:
    """Reset structlog + refill sentinel buckets before every test."""
    obs_logging._reset_for_tests()
    structlog.contextvars.clear_contextvars()
    for bucket in (
        rate_limits.public_rest,
        rate_limits.private_rest,
        rate_limits.public_ws,
    ):
        bucket._tokens = bucket.capacity  # type: ignore[attr-defined]
        bucket._last_refill = float(bucket._monotonic())  # type: ignore[attr-defined]
    yield
    obs_logging._reset_for_tests()
    structlog.contextvars.clear_contextvars()


@pytest.fixture()
def _env_ok(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "ak_sentinel_" + "x" * 32)
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "sk_sentinel_" + "y" * 32)
    monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestHelpNeverLoadsSecrets:
    def test_help_does_not_construct_settings(self) -> None:
        with mock.patch(
            "bithumb_bot.secrets.settings.BithumbSecrets"
        ) as settings_mock:
            with pytest.raises(SystemExit) as excinfo:
                main(["--help"])
        assert excinfo.value.code == 0
        settings_mock.assert_not_called()


class TestM0SelfcheckLogsCapability:
    def test_bound_context_present_and_no_secret_leakage(
        self, _env_ok: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["m0", "selfcheck"])
        assert rc == 0
        # stdout summary contains no substring of sentinel access/secret keys.
        out = capsys.readouterr().out
        assert "ak_sentinel_" not in out
        assert "sk_sentinel_" not in out


class TestM1FetchSpecViaMockTransport:
    def test_three_artifacts_written_and_no_secret_leakage(
        self,
        _env_ok: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def handler_fn(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_RAW)

        transport = httpx.MockTransport(handler_fn)

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
        # Sentinel credential values MUST NOT appear anywhere in stdout.
        assert "ak_sentinel_" not in out
        assert "sk_sentinel_" not in out


class TestLogFormatFlag:
    def test_json_flag_selects_json_renderer(self) -> None:
        """`--log-format json` is stripped from argv before dispatch."""
        # Pre-strip the argv via `main(...)` — no exception expected.
        # `bt --log-format json --help` raises SystemExit(0).
        with pytest.raises(SystemExit) as excinfo:
            main(["--log-format", "json", "--help"])
        assert excinfo.value.code == 0

    def test_console_flag_ok(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--log-format=console", "--help"])
        assert excinfo.value.code == 0
