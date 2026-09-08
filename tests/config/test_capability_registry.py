"""Tests for `bithumb_bot.config.capability_registry`.

Covers Behavior contract from PLAN task 01-01-06:
- REGISTRY is keyed by (verb, subverb) tuples.
- Every Phase-1 D-86 row is present with the exact D-88 CapabilityRequirements.
- Every D-87 reserved future verb is present with its D-88 CapabilityRequirements
  AND a reserved handler stub that raises NotImplementedError (D-90).
- No M6B / live verb is registered (D-96).
- trade_cred_prohibited=True on EVERY registered capability (D-97).
- CapabilityRequirements is a frozen dataclass.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bithumb_bot.config.capability_registry import (
    REGISTRY,
    RESERVED_HANDLER_ERROR,
    CapRequirement,
    CapabilityRequirements,
    CredRequirement,
    HumanAuthRequirement,
    SnapshotRequirement,
)


# ---------------------------------------------------------------------------
# Phase-1 verbs — D-86 rows
# ---------------------------------------------------------------------------

PHASE1_ROWS: dict[tuple[str, str], CapabilityRequirements] = {
    ("config", "validate"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m0", "selfcheck"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m1", "fetch-spec"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.NONE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.ACCOUNT_READ_REQUIRED,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.INVOCATION_ONLY,
    ),
    ("m1", "verify-facts"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.EVIDENCE_INPUT,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m1", "verify-snapshot"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.CANDIDATE,
        cap=CapRequirement.NOT_REQUIRED,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
}

# ---------------------------------------------------------------------------
# D-87 reserved future verbs — registered but not scaffolded as functional
# ---------------------------------------------------------------------------

RESERVED_FUTURE_ROWS: dict[tuple[str, str], CapabilityRequirements] = {
    ("m2", "collect-observations"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.MAY_BE_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m2", "calibrate-costs"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.MAY_BE_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m2", "replay-known-answer"): CapabilityRequirements(
        gate1=True,
        gate2=False,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.MAY_BE_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m4", "evaluate-selection"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("m5", "evaluate-module"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("freeze", "strategy"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.EXPLICIT_APPROVAL,
    ),
    ("m6a", "verify-mock-broker"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.NONE,
    ),
    ("freeze", "final"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=False,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.EXPLICIT_APPROVAL,
    ),
    ("holdout", "evaluate"): CapabilityRequirements(
        gate1=True,
        gate2=True,
        gate3=True,
        snapshot=SnapshotRequirement.VERIFIED_REQUIRED,
        cap=CapRequirement.REQUIRED_NON_NULL,
        cred=CredRequirement.NONE,
        trade_cred_prohibited=True,
        human_auth=HumanAuthRequirement.ONE_TIME_APPROVAL,
    ),
}


class TestRegistryStructure:
    def test_registry_is_dict(self) -> None:
        assert isinstance(REGISTRY, dict)

    def test_registry_keys_are_two_tuples(self) -> None:
        for key in REGISTRY:
            assert isinstance(key, tuple)
            assert len(key) == 2
            assert all(isinstance(part, str) for part in key)

    def test_registry_values_are_capability_requirements(self) -> None:
        for reqs in REGISTRY.values():
            assert isinstance(reqs, CapabilityRequirements)


class TestPhase1RowsExactMatch:
    """Every Phase-1 D-86 row is present with the exact D-88 requirements."""

    @pytest.mark.parametrize("key,expected", list(PHASE1_ROWS.items()))
    def test_phase1_row_matches_d88(
        self,
        key: tuple[str, str],
        expected: CapabilityRequirements,
    ) -> None:
        assert key in REGISTRY, f"Phase-1 D-86 row missing: {key!r}"
        assert REGISTRY[key] == expected


class TestReservedFutureRowsExactMatch:
    """Every D-87 reserved verb is present + has its D-88 requirements."""

    @pytest.mark.parametrize("key,expected", list(RESERVED_FUTURE_ROWS.items()))
    def test_reserved_row_matches_d88(
        self,
        key: tuple[str, str],
        expected: CapabilityRequirements,
    ) -> None:
        assert key in REGISTRY, f"Reserved D-87 row missing: {key!r}"
        assert REGISTRY[key] == expected

    @pytest.mark.parametrize("key", list(RESERVED_FUTURE_ROWS.keys()))
    def test_reserved_row_trade_cred_prohibited(
        self, key: tuple[str, str]
    ) -> None:
        assert REGISTRY[key].trade_cred_prohibited is True


class TestNoLiveOrM6BVerbs:
    """D-96: no M6B / live handler is implemented or registered."""

    def test_no_m6b_key_in_registry(self) -> None:
        for verb, subverb in REGISTRY:
            assert "m6b" not in verb.lower()
            assert "m6b" not in subverb.lower()

    def test_no_live_key_in_registry(self) -> None:
        for verb, subverb in REGISTRY:
            assert "live" not in verb.lower()
            assert "live" not in subverb.lower()

    def test_no_withdraw_key_in_registry(self) -> None:
        for verb, subverb in REGISTRY:
            assert "withdraw" not in verb.lower()
            assert "withdraw" not in subverb.lower()


class TestUniversalTradeProhibition:
    """D-97: trade_cred_prohibited=True on EVERY registered capability."""

    def test_every_row_prohibits_trade_cred(self) -> None:
        for key, reqs in REGISTRY.items():
            assert reqs.trade_cred_prohibited is True, (
                f"row {key!r} must have trade_cred_prohibited=True (D-97)"
            )


class TestCapabilityRequirementsFrozen:
    def test_cannot_mutate_after_construction(self) -> None:
        reqs = REGISTRY[("m0", "selfcheck")]
        with pytest.raises(FrozenInstanceError):
            reqs.gate1 = False  # type: ignore[misc]


class TestReservedHandlerStub:
    def test_reserved_handler_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError) as excinfo:
            RESERVED_HANDLER_ERROR()
        assert "reserved" in str(excinfo.value).lower()

    def test_reserved_handler_message_names_future_phase(self) -> None:
        with pytest.raises(NotImplementedError) as excinfo:
            RESERVED_HANDLER_ERROR()
        assert "future" in str(excinfo.value).lower() or "phase" in str(
            excinfo.value
        ).lower()
