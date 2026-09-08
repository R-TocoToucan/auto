"""Task 01-03-09 — defense-in-depth (D-85) tests.

Every handler function reachable via direct Python import (bypassing the
CLI) MUST call ``validate()`` as its FIRST statement. A monkeypatched
``validate()`` returning ``ok=False`` MUST cause the handler to refuse
BEFORE any credential load, TOML parse, or side effect.

Also covers ``m1_stubs`` — the three ``m1`` handler stubs that plan
01-04 replaces:

* ``m1_fetch_spec_stub``
* ``m1_verify_facts_stub``
* ``m1_verify_snapshot_stub``

Each stub follows the same pattern: ``validate()`` first, then a stub
body that raises ``RuntimeError("plan 01-04 required — not yet
implemented")``. If ``validate()`` fails, the stub refuses at the guard
and NEVER reaches the stub body.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bithumb_bot.cli.handlers import config_validate, m0_selfcheck, m1_stubs
from bithumb_bot.config.validator import ValidationResult


REPO_ROOT = Path(__file__).resolve().parents[2]


def _passing() -> ValidationResult:
    return ValidationResult(ok=True)


def _failing() -> ValidationResult:
    return ValidationResult(
        ok=False, missing=("gate1",), reason="fixture-fail"
    )


# ---------------------------------------------------------------------------
# config_validate — defense in depth
# ---------------------------------------------------------------------------


class TestConfigValidateDefenseInDepth:
    def test_direct_call_with_failing_validate_never_reaches_load_gate1(
        self,
    ) -> None:
        """Direct-Python bypass of the CLI must ALSO trip validate() first."""
        stderr = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.handlers.config_validate.validate",
            return_value=_failing(),
        ), mock.patch(
            "bithumb_bot.cli.handlers.config_validate.load_gate1"
        ) as load_mock, redirect_stderr(stderr):
            rc = config_validate.handler(SimpleNamespace(through="gate1"))
        assert rc == 1
        load_mock.assert_not_called()


# ---------------------------------------------------------------------------
# m0_selfcheck — defense in depth
# ---------------------------------------------------------------------------


class TestM0SelfcheckDefenseInDepth:
    def test_direct_call_with_failing_validate_never_reaches_load_gate1(
        self,
    ) -> None:
        stderr = io.StringIO()
        # If validate refuses, load_gate1 and load_secrets must not run.
        with mock.patch(
            "bithumb_bot.cli.handlers.m0_selfcheck.validate",
            return_value=_failing(),
        ), mock.patch(
            "bithumb_bot.cli.handlers.m0_selfcheck.load_gate1"
        ) as load_gate1_mock, mock.patch(
            "bithumb_bot.cli.handlers.m0_selfcheck.load_secrets"
        ) as load_secrets_mock, redirect_stderr(stderr):
            rc = m0_selfcheck.handler(SimpleNamespace())
        assert rc == 1
        load_gate1_mock.assert_not_called()
        load_secrets_mock.assert_not_called()


# ---------------------------------------------------------------------------
# m1_stubs — validate-first + stub-body-only-if-validate-passes
# ---------------------------------------------------------------------------


class TestM1StubsFollowValidateFirstDiscipline:
    @pytest.mark.parametrize(
        ("stub_fn_name", "capability"),
        [
            ("m1_fetch_spec_stub", ("m1", "fetch-spec")),
            ("m1_verify_facts_stub", ("m1", "verify-facts")),
            ("m1_verify_snapshot_stub", ("m1", "verify-snapshot")),
        ],
    )
    def test_stub_refuses_when_validate_fails(
        self, stub_fn_name: str, capability: tuple[str, str]
    ) -> None:
        stub = getattr(m1_stubs, stub_fn_name)
        stderr = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.handlers.m1_stubs.validate",
            return_value=_failing(),
        ) as validate_mock, redirect_stderr(stderr):
            rc = stub(SimpleNamespace())
        assert rc == 1
        validate_mock.assert_called_once_with(capability)

    @pytest.mark.parametrize(
        "stub_fn_name",
        ["m1_fetch_spec_stub", "m1_verify_facts_stub", "m1_verify_snapshot_stub"],
    )
    def test_stub_raises_when_validate_passes(self, stub_fn_name: str) -> None:
        """If validate passes, the stub body raises RuntimeError('plan 01-04')."""
        stub = getattr(m1_stubs, stub_fn_name)
        with mock.patch(
            "bithumb_bot.cli.handlers.m1_stubs.validate",
            return_value=_passing(),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                stub(SimpleNamespace(market="KRW-BTC", bundle=None, snapshot=None))
            assert "plan 01-04" in str(excinfo.value).lower()

    def test_module_docstring_documents_defense_in_depth(self) -> None:
        """m1_stubs module docstring must cite D-85."""
        doc = m1_stubs.__doc__ or ""
        assert "D-85" in doc or "defense-in-depth" in doc.lower()


# ---------------------------------------------------------------------------
# Symmetry: config_validate and m0_selfcheck docstrings mention D-85
# ---------------------------------------------------------------------------


class TestHandlerDocstringsDocumentDefenseInDepth:
    def test_config_validate_docstring_cites_d85(self) -> None:
        doc = config_validate.__doc__ or ""
        assert "D-85" in doc or "defense-in-depth" in doc.lower()

    def test_m0_selfcheck_docstring_cites_d85(self) -> None:
        doc = m0_selfcheck.__doc__ or ""
        assert "D-85" in doc or "defense-in-depth" in doc.lower()
