"""Tests for `bithumb_bot.config.validator.validate` — capability-scoped,
fail-closed startup validation (SAFE-02 restructured per D-99).

Covers Behavior contract from PLAN task 01-01-07:
- Unknown capability → UnknownCapabilityError (D-89).
- Missing / malformed gate1.toml → ValidationResult(ok=False, missing=("gate1",),
  reason=<load-error message>) — never a bare pass-through.
- Missing gate2.toml → ValidationResult(ok=False, missing=("gate2",),
  reason="gate2 not yet frozen — Phase 3"). Do NOT create it (D-56).
- Every Phase-1 D-86 row returns ok=True when its prereqs are satisfied.
- Cred check for m1 fetch-spec only asserts env-var PRESENCE, never reads
  values.
- ValidationResult is a frozen dataclass.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bithumb_bot.config.capability_registry import PHASE1_KEYS
from bithumb_bot.config.validator import (
    REPO_ROOT_ENV,
    ValidationResult,
    validate,
)
from bithumb_bot.errors import UnknownCapabilityError


# ---------------------------------------------------------------------------
# Fixture: point the validator at a tmp repo root with a valid gate1.toml
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_repo_root(
    tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Points the validator at `tmp_path` (the tmp_gate1_toml's grandparent).

    tmp_gate1_toml is placed under `tmp_path/config/decisions/gate1.toml`;
    tmp_path is therefore the "repo root" for validator lookups.
    """
    repo_root = tmp_gate1_toml.parent.parent.parent
    monkeypatch.setenv(REPO_ROOT_ENV, str(repo_root))
    return repo_root


# ---------------------------------------------------------------------------
# ValidationResult shape
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_default_ok_true_no_missing(self) -> None:
        result = ValidationResult(ok=True, missing=(), reason=None)
        assert result.ok is True
        assert result.missing == ()
        assert result.reason is None

    def test_is_frozen(self) -> None:
        result = ValidationResult(ok=True, missing=(), reason=None)
        with pytest.raises(FrozenInstanceError):
            result.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unknown capability (D-89)
# ---------------------------------------------------------------------------


class TestUnknownCapability:
    def test_unknown_verb_raises(self, tmp_repo_root: Path) -> None:
        with pytest.raises(UnknownCapabilityError):
            validate(("bogus", "verb"))

    def test_unknown_subverb_raises(self, tmp_repo_root: Path) -> None:
        with pytest.raises(UnknownCapabilityError):
            validate(("m0", "does-not-exist"))


# ---------------------------------------------------------------------------
# Phase-1 rows return ok=True when prereqs satisfied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", sorted(PHASE1_KEYS))
def test_phase1_capability_ok_when_gate1_present(
    capability: tuple[str, str],
    tmp_repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `m1 fetch-spec` also needs the account/read env vars to be *present*
    # (never read their values). Set two dummy values.
    if capability == ("m1", "fetch-spec"):
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "dummy-access")
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "dummy-secret")
    result = validate(capability)
    assert result.ok is True, f"expected ok=True for {capability}, got {result!r}"
    assert result.missing == ()


# ---------------------------------------------------------------------------
# Gate-1 missing / malformed
# ---------------------------------------------------------------------------


class TestGate1Missing:
    def test_gate1_missing_returns_ok_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No gate1.toml at all under this repo root
        monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_path))
        result = validate(("m0", "selfcheck"))
        assert result.ok is False
        assert "gate1" in result.missing
        assert result.reason is not None
        assert "gate1" in result.reason.lower()

    def test_gate1_malformed_reports_parse_error(
        self, tmp_gate1_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_gate1_toml.write_text("this is [ not valid", encoding="utf-8")
        repo_root = tmp_gate1_toml.parent.parent.parent
        monkeypatch.setenv(REPO_ROOT_ENV, str(repo_root))
        result = validate(("m0", "selfcheck"))
        assert result.ok is False
        assert "gate1" in result.missing
        assert result.reason is not None
        # The specific parse error must surface in the reason (not a
        # generic "gate1 missing").
        assert "toml" in result.reason.lower() or "parse" in result.reason.lower()


# ---------------------------------------------------------------------------
# Gate-2 not yet frozen (Phase 3)
# ---------------------------------------------------------------------------


class TestGate2NotYetFrozen:
    def test_m4_evaluate_selection_missing_gate2(
        self, tmp_repo_root: Path
    ) -> None:
        result = validate(("m4", "evaluate-selection"))
        assert result.ok is False
        assert "gate2" in result.missing
        assert result.reason is not None
        assert "gate2" in result.reason.lower()

    def test_gate2_not_created_by_validator(
        self, tmp_repo_root: Path
    ) -> None:
        # After a validate call that would have needed gate2, the file
        # must NOT have been created (D-56).
        _ = validate(("m4", "evaluate-selection"))
        assert not (tmp_repo_root / "config" / "decisions" / "gate2.toml").exists()

    def test_holdout_evaluate_reports_a_missing_prereq(
        self, tmp_repo_root: Path
    ) -> None:
        result = validate(("holdout", "evaluate"))
        assert result.ok is False
        # Either gate2 or gate3 (both are missing) — plan Oracle says
        # either order is acceptable so long as at least one missing
        # prereq is identified.
        assert set(result.missing) & {"gate2", "gate3"}


# ---------------------------------------------------------------------------
# Cred requirement: env-var PRESENCE only, values never read
# ---------------------------------------------------------------------------


class TestCredentialPresenceCheck:
    def test_m1_fetch_spec_refuses_without_creds(
        self, tmp_repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", raising=False)
        monkeypatch.delenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", raising=False)
        result = validate(("m1", "fetch-spec"))
        assert result.ok is False
        assert "account_read" in "".join(result.missing).lower() or "cred" in "".join(
            result.missing
        ).lower()

    def test_m1_fetch_spec_ok_with_creds_present(
        self, tmp_repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_ACCESS_KEY", "any-value-not-read")
        monkeypatch.setenv("BITHUMB_ACCOUNT_READ_SECRET_KEY", "any-value-not-read")
        result = validate(("m1", "fetch-spec"))
        assert result.ok is True
