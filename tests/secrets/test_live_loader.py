"""Tests for the trade-credential loader used ONLY by ``bt live breakout-cycle``.

Every test operates entirely offline. No credential value ever appears
in a raised exception, log, or persisted artifact.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bithumb_bot.secrets.live import (
    LoadedTradeCredentials,
    TradeCredentialsMissingError,
    load_trade_credentials,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("BITHUMB_"):
            monkeypatch.delenv(k, raising=False)


def test_missing_trade_credentials_refuses(tmp_path: Path) -> None:
    with pytest.raises(TradeCredentialsMissingError):
        load_trade_credentials(tmp_path)


def test_partial_trade_credentials_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "aaa")
    # No secret key.
    with pytest.raises(TradeCredentialsMissingError):
        load_trade_credentials(tmp_path)


def test_present_pair_returns_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "the-access-key")
    monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", "the-secret-key")
    creds = load_trade_credentials(tmp_path)
    assert isinstance(creds, LoadedTradeCredentials)
    assert creds.access_key == "the-access-key"
    assert creds.secret_key == "the-secret-key"


def test_repr_masks_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "seCrEt-A")
    monkeypatch.setenv("BITHUMB_TRADE_SECRET_KEY", "seCrEt-B")
    creds = load_trade_credentials(tmp_path)
    r = repr(creds)
    assert "seCrEt-A" not in r
    assert "seCrEt-B" not in r


def test_refusal_message_never_carries_key_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BITHUMB_TRADE_ACCESS_KEY", "value-should-not-leak")
    try:
        load_trade_credentials(tmp_path)
    except TradeCredentialsMissingError as exc:
        assert "value-should-not-leak" not in str(exc)
    else:
        pytest.fail("expected TradeCredentialsMissingError")
