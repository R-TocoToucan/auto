"""Wave-2 integration smoke — composes load_gate1, validate, and
build_config_hash_manifest against the REAL committed
`config/decisions/gate1.toml`.

Covers Task 01-01-09 Description:
- Load the real, committed gate1.toml.
- Run validate() for every Phase-1 D-86 verb that this plan owns:
  config validate, m0 selfcheck, m1 fetch-spec (with the two dummy
  BITHUMB_ACCOUNT_READ_* env vars set — never their real values).
- Feed the returned gate1_sha256 into build_config_hash_manifest and
  assert the result is byte-stable across two invocations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.config.config_hash import build_config_hash_manifest
from bithumb_bot.config.gate_loader import load_gate1
from bithumb_bot.config.validator import REPO_ROOT_ENV, validate


REPO_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture()
def use_committed_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REPO_ROOT_ENV, str(REPO_ROOT))


def test_committed_gate1_toml_loads_and_hash_is_stable() -> None:
    target = REPO_ROOT / "config" / "decisions" / "gate1.toml"
    assert target.is_file()
    _, sha_a = load_gate1(target)
    _, sha_b = load_gate1(target)
    assert sha_a == sha_b
    assert len(sha_a) == 64


def test_validate_config_validate_ok(use_committed_repo_root: None) -> None:
    result = validate(("config", "validate"))
    assert result.ok is True, f"got {result!r}"


def test_validate_m0_selfcheck_ok(use_committed_repo_root: None) -> None:
    result = validate(("m0", "selfcheck"))
    assert result.ok is True, f"got {result!r}"


def test_validate_m1_fetch_spec_ok_with_dummy_creds(
    use_committed_repo_root: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Values are never read — presence is the whole point of the check
    # (D-70). Use obvious placeholders so a mistaken log leak would be
    # visible in a review.
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "test-access-placeholder")
    monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "test-secret-placeholder")
    result = validate(("m1", "fetch-spec"))
    assert result.ok is True, f"got {result!r}"


def test_config_hash_manifest_byte_stable_across_invocations() -> None:
    target = REPO_ROOT / "config" / "decisions" / "gate1.toml"
    _, gate1_sha = load_gate1(target)
    manifest_a, hash_a = build_config_hash_manifest(gate1_sha)
    manifest_b, hash_b = build_config_hash_manifest(gate1_sha)
    assert manifest_a == manifest_b
    assert hash_a == hash_b


def test_committed_gate1_hash_feeds_manifest_deterministically() -> None:
    target = REPO_ROOT / "config" / "decisions" / "gate1.toml"
    _, gate1_sha = load_gate1(target)
    _, config_hash = build_config_hash_manifest(gate1_sha)
    # Phase 1 does not know the final config_hash value; we only assert
    # the shape (64 lowercase hex) — the value freezes at Phase 5 FRZ-02.
    assert len(config_hash) == 64
    assert all(c in "0123456789abcdef" for c in config_hash)
