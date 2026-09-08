"""Tests for `bithumb_bot.config.gate1_model` — frozen pydantic v2
`Gate1Decisions` model + `StrictDecimal` type.

Covers Behavior contract from PLAN task 01-01-04:
- Loading a valid gate1.toml yields a frozen Gate1Decisions
- Assigning to any field raises pydantic.ValidationError (frozen=True)
- Unknown TOML key raises ValidationError (extra="forbid")
- A TOML numeric literal in a decimal-bearing field is rejected;
  a QUOTED "100000" produces Decimal("100000")
- max_validated_notional_krw with the "unset" sentinel → None
- Every branch of _decimal_from_str is unit-tested (positive str,
  positive existing Decimal, negative float, negative int)

TDD RED gate — these tests must fail before the model exists.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bithumb_bot.config.gate1_model import (
    Gate1Decisions,
    StrictDecimal,
    _decimal_from_str,
)


# =============================================================================
# _decimal_from_str — pure-function unit tests
# =============================================================================


class TestDecimalFromStr:
    def test_str_input_returns_decimal(self) -> None:
        assert _decimal_from_str("100000") == Decimal("100000")

    def test_str_input_preserves_precision(self) -> None:
        assert _decimal_from_str("0.00000001") == Decimal("0.00000001")

    def test_existing_decimal_passes_through(self) -> None:
        d = Decimal("1.23")
        assert _decimal_from_str(d) is d

    def test_float_raises(self) -> None:
        with pytest.raises(TypeError):
            _decimal_from_str(0.1)

    def test_int_raises_for_strict_decimal(self) -> None:
        # int would otherwise be exact, but the whole point of StrictDecimal is
        # to force operators to declare monetary values as quoted strings so
        # they are also unambiguous at the TOML source level.
        with pytest.raises(TypeError):
            _decimal_from_str(100000)

    def test_bool_raises(self) -> None:
        with pytest.raises(TypeError):
            _decimal_from_str(True)

    def test_none_raises(self) -> None:
        with pytest.raises(TypeError):
            _decimal_from_str(None)


# =============================================================================
# Gate1Decisions — model-level contract tests
# =============================================================================


def _valid_gate1_data(tmp_gate1_toml: Path) -> dict:
    """Parse the fixture into a dict, ready to hand to Gate1Decisions(**data)."""
    return tomllib.loads(tmp_gate1_toml.read_text(encoding="utf-8"))


class TestGate1Decisions:
    def test_valid_toml_yields_frozen_instance(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        gate1 = Gate1Decisions(**data)
        assert gate1.model_config.get("frozen") is True

    def test_provisional_cap_is_decimal(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        gate1 = Gate1Decisions(**data)
        assert gate1.provisional_engineering_notional_krw == Decimal("100000")
        assert isinstance(gate1.provisional_engineering_notional_krw, Decimal)

    def test_max_validated_notional_krw_unset_sentinel_becomes_none(
        self, tmp_gate1_toml: Path
    ) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        gate1 = Gate1Decisions(**data)
        assert gate1.max_validated_notional_krw is None

    def test_d40_http_values_present(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        gate1 = Gate1Decisions(**data)
        assert gate1.m1_spec_http_connect_timeout_ms == 5000
        assert gate1.m1_spec_http_read_timeout_ms == 15000
        assert gate1.m1_spec_http_max_attempts == 3
        assert gate1.m1_spec_http_backoff_initial_ms == 500
        assert gate1.m1_spec_http_backoff_cap_ms == 5000
        assert gate1.m1_spec_http_jitter == "full"

    def test_provenance_fields_present(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        gate1 = Gate1Decisions(**data)
        assert gate1.status == "frozen"
        assert gate1.schema_version == 1
        assert gate1.approved_at_utc  # non-empty
        assert gate1.source_commit  # non-empty
        assert gate1.research_spec_sha256
        assert gate1.execution_spec_sha256

    # -------- Negative cases (each is an explicit pytest.raises) --------

    def test_extra_key_raises(self, tmp_gate1_toml: Path) -> None:
        raw = tmp_gate1_toml.read_text(encoding="utf-8") + '\nunexpected_key = "x"\n'
        data = tomllib.loads(raw)
        with pytest.raises(ValidationError):
            Gate1Decisions(**data)

    def test_unquoted_decimal_raises(self, tmp_gate1_toml: Path) -> None:
        # Replace the quoted "100000" with a bare 100000 (int literal).
        raw = tmp_gate1_toml.read_text(encoding="utf-8").replace(
            'provisional_engineering_notional_krw = "100000"',
            "provisional_engineering_notional_krw = 100000",
        )
        data = tomllib.loads(raw)
        with pytest.raises(ValidationError):
            Gate1Decisions(**data)

    def test_toml_float_for_strict_decimal_raises(self, tmp_gate1_toml: Path) -> None:
        raw = tmp_gate1_toml.read_text(encoding="utf-8").replace(
            'provisional_engineering_notional_krw = "100000"',
            "provisional_engineering_notional_krw = 100000.0",
        )
        data = tomllib.loads(raw)
        with pytest.raises(ValidationError):
            Gate1Decisions(**data)

    def test_assignment_after_construction_raises(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        gate1 = Gate1Decisions(**data)
        with pytest.raises(ValidationError):
            gate1.status = "unfrozen"  # type: ignore[misc]

    def test_missing_required_provenance_field_raises(
        self, tmp_gate1_toml: Path
    ) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        data.pop("schema_version")
        with pytest.raises(ValidationError):
            Gate1Decisions(**data)

    def test_wrong_status_literal_raises(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        data["status"] = "draft"
        with pytest.raises(ValidationError):
            Gate1Decisions(**data)

    def test_wrong_schema_version_raises(self, tmp_gate1_toml: Path) -> None:
        data = _valid_gate1_data(tmp_gate1_toml)
        data["schema_version"] = 2
        with pytest.raises(ValidationError):
            Gate1Decisions(**data)


class TestStrictDecimalAnnotation:
    """The `StrictDecimal` type is `Annotated[Decimal, BeforeValidator(...)]`."""

    def test_is_annotated_alias(self) -> None:
        from typing import get_args, get_origin

        # `Annotated[X, ...]`'s origin is X for pydantic's introspection.
        origin = get_origin(StrictDecimal)
        # Just assert we get a non-None annotation structure.
        assert origin is not None or get_args(StrictDecimal), (
            "StrictDecimal must be Annotated[Decimal, BeforeValidator(...)]"
        )
