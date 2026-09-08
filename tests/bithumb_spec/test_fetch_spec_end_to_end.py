"""End-to-end offline test for :func:`bithumb_bot.bithumb_spec.client.fetch_spec`.

Every HTTP request flows through `httpx.MockTransport` — the test suite
NEVER touches a real Bithumb endpoint (D-79). Sentinel credentials
never appear in raised exceptions (T-1-04-01).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest

from bithumb_bot.bithumb_spec import rate_limits
from bithumb_bot.bithumb_spec.client import fetch_spec
from bithumb_bot.bithumb_spec.snapshot import load_snapshot
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.errors import (
    ProhibitedCredentialDetectedError,
    SnapshotValidationError,
)


@pytest.fixture(autouse=True)
def _refill_sentinel_buckets() -> Any:
    """The sentinel per-channel buckets (capacity=1, refill=0.5) would
    otherwise drain across sequential tests and force real 2s waits.
    Reset both to full before every test in this module."""
    for bucket in (rate_limits.public_rest, rate_limits.private_rest, rate_limits.public_ws):
        bucket._tokens = bucket.capacity  # type: ignore[attr-defined]
        bucket._last_refill = float(bucket._monotonic())  # type: ignore[attr-defined]
    yield


# Same shape as the committed fixture.
_RAW_ORDERS_CHANCE = {
    "bid_fee": "0.0025",
    "ask_fee": "0.0025",
    "maker_bid_fee": "0.0025",
    "maker_ask_fee": "0.0025",
    "market": {
        "name": "KRW-BTC",
        "order_types": ["limit", "price", "market"],
        "bid": {"currency": "KRW", "min_total": "5000", "price_unit": "1000"},
        "ask": {"currency": "BTC", "min_total": "0.0001", "price_unit": "1000"},
        "max_total": "1000000000",
    },
}


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/orders/chance"
        assert "market" in request.url.params
        return httpx.Response(200, json=_RAW_ORDERS_CHANCE)

    return httpx.MockTransport(handler)


@pytest.fixture()
def _account_read_env(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Set REPO_ROOT_ENV to the gate1 tmp tree + inject account/read creds.

    Returns the artifact-root under `tmp_path` for the caller to pass
    into `fetch_spec(..., artifacts_root=...)`.
    """
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "ak_test_" + "x" * 32)
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "sk_test_" + "y" * 32)
    monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)
    return tmp_path


class TestEndToEndOffline:
    def test_all_three_artifacts_written(
        self, _account_read_env: Path, tmp_path: Path
    ) -> None:
        result = asyncio.run(
            fetch_spec(
                "KRW-BTC",
                transport=_mock_transport(),
                repo_root=Path(str(tmp_path)),
                artifacts_root=Path(str(tmp_path)),
            )
        )
        assert result.snapshot_path.is_file()
        assert result.fixture_path.is_file()
        assert result.verification_bundle_dir.is_dir()
        # VERIFICATION.md + manifest.json in the bundle dir.
        assert (result.verification_bundle_dir / "VERIFICATION.md").is_file()
        assert (result.verification_bundle_dir / "manifest.json").is_file()

    def test_snapshot_load_round_trip(
        self, _account_read_env: Path, tmp_path: Path
    ) -> None:
        result = asyncio.run(
            fetch_spec(
                "KRW-BTC",
                transport=_mock_transport(),
                repo_root=Path(str(tmp_path)),
                artifacts_root=Path(str(tmp_path)),
            )
        )
        snapshot = load_snapshot(result.snapshot_path)
        assert snapshot.market == "KRW-BTC"
        assert snapshot.venue == "bithumb"

    def test_sanitized_fixture_has_no_forbidden_keys(
        self, _account_read_env: Path, tmp_path: Path
    ) -> None:
        result = asyncio.run(
            fetch_spec(
                "KRW-BTC",
                transport=_mock_transport(),
                repo_root=Path(str(tmp_path)),
                artifacts_root=Path(str(tmp_path)),
            )
        )
        content = json.loads(result.fixture_path.read_text(encoding="utf-8"))
        for forbidden in (
            "access_key",
            "secret_key",
            "Authorization",
            "nonce",
            "signature",
            "account_id",
            "balance",
            "avg_buy_price",
            "locked_balance",
        ):
            assert forbidden not in content

    def test_verification_bundle_all_five_facts_unchecked(
        self, _account_read_env: Path, tmp_path: Path
    ) -> None:
        result = asyncio.run(
            fetch_spec(
                "KRW-BTC",
                transport=_mock_transport(),
                repo_root=Path(str(tmp_path)),
                artifacts_root=Path(str(tmp_path)),
            )
        )
        md = (result.verification_bundle_dir / "VERIFICATION.md").read_text(
            encoding="utf-8"
        )
        # Every fact section is present.
        for key in (
            "ws_v1_public_v2_private_boundary",
            "jwt_timestamp_claim_shape",
            "legacy_stop_limit_deferred",
            "orders_chance_pagination_cursor",
            "per_channel_rate_limit_values",
        ):
            assert f"## Fact: `{key}`" in md
        # None pre-checked.
        assert "[x] confirmed" not in md
        assert "[x] contradicted" not in md
        # D-84 phrase discipline.
        assert "human-approved" in md
        assert "human-signed" not in md


class TestDefenseInDepth:
    def test_missing_account_read_creds_refused_before_http(
        self, tmp_path: Path, tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate() refuses; NO httpx client is ever constructed."""
        monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
        monkeypatch.delenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", raising=False)
        monkeypatch.delenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", raising=False)
        monkeypatch.delenv("BITHUMB_TRADE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("BITHUMB_TRADE_SECRET_KEY", raising=False)

        with mock.patch("httpx.AsyncClient") as async_client_mock:
            with pytest.raises(SnapshotValidationError):
                asyncio.run(
                    fetch_spec(
                        "KRW-BTC",
                        transport=None,
                        repo_root=Path(str(tmp_path)),
                        artifacts_root=Path(str(tmp_path)),
                    )
                )
        async_client_mock.assert_not_called()

    def test_trade_cred_present_refused_before_http(
        self, tmp_path: Path, tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-68/D-97: trade cred in env → refusal before any HTTP call."""
        monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "ak_" + "x" * 32)
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "sk_" + "y" * 32)
        monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "TRADE-SENTINEL-VALUE-32bytes-XXX")
        monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", "TRADE-SECRET-SENTINEL-VALUE-XXXX")

        with mock.patch("httpx.AsyncClient") as async_client_mock:
            with pytest.raises((SnapshotValidationError, ProhibitedCredentialDetectedError)) as excinfo:
                asyncio.run(
                    fetch_spec(
                        "KRW-BTC",
                        transport=None,
                        repo_root=Path(str(tmp_path)),
                        artifacts_root=Path(str(tmp_path)),
                    )
                )
        async_client_mock.assert_not_called()
        # D-70: sentinel MUST NOT appear in the exception.
        rendered = str(excinfo.value) + repr(excinfo.value)
        assert "TRADE-SENTINEL-VALUE" not in rendered
        assert "TRADE-SECRET-SENTINEL" not in rendered


class TestSecretDiscipline:
    def test_jwt_encode_raises_sentinel_never_in_exception(
        self, _account_read_env: Path, tmp_path: Path
    ) -> None:
        # Force jwt.encode to raise.
        with mock.patch("jwt.encode", side_effect=RuntimeError("inner")):
            # sentinel secret is set via env; force it high-signal.
            with pytest.raises(Exception) as excinfo:
                asyncio.run(
                    fetch_spec(
                        "KRW-BTC",
                        transport=_mock_transport(),
                        repo_root=Path(str(tmp_path)),
                        artifacts_root=Path(str(tmp_path)),
                    )
                )
        rendered = str(excinfo.value) + repr(excinfo.value)
        # The env-set sentinel prefix should not appear.
        assert "ak_test_" not in rendered
        assert "sk_test_" not in rendered
