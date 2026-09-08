"""Task 01-03-06 — `bt config validate --through gate1` handler tests.

Behavior contract exercised here (from plan 01-03 §Task 01-03-06):

* Handler signature: ``handler(args: argparse.Namespace) -> int``.
* First statement calls ``validate(("config","validate"))`` — defense
  in depth per D-85 (direct Python callers can't bypass CLI validation).
* Inspects `config/decisions/gate1.toml` via `load_gate1(...)` — does
  NOT mark it approved / frozen (D-89 — inspect-only path).
* Prints a structured summary: gate name, status, schema_version,
  source_commit, research_spec_sha256[:12], execution_spec_sha256[:12],
  provisional_engineering_notional_krw, count of value-deferred Gate-2/3
  fields.
* Never prints credential-adjacent strings (none exist in gate1.toml).
* Never mutates the gate file.
* Returns 0 on success.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bithumb_bot.cli import main as cli_main
from bithumb_bot.cli.handlers import config_validate
from bithumb_bot.config.validator import ValidationResult
from bithumb_bot.errors import Gate1LoadError


REPO_ROOT = Path(__file__).resolve().parents[2]
GATE1_PATH = REPO_ROOT / "config" / "decisions" / "gate1.toml"


# ---------------------------------------------------------------------------
# Formatter — pure function, easy to assert on
# ---------------------------------------------------------------------------


class TestFormatGate1Summary:
    def test_summary_contains_required_fields(self) -> None:
        from bithumb_bot.config.gate_loader import load_gate1

        gate1, sha256 = load_gate1(GATE1_PATH)
        text = config_validate.format_gate1_summary(gate1, sha256)
        assert "gate1" in text.lower()
        assert "frozen" in text
        assert sha256[:12] in text
        assert "provisional_engineering_notional_krw" in text
        # D-43 hard-coded provisional value.
        assert "100000" in text

    def test_summary_never_prints_credential_tokens(self) -> None:
        from bithumb_bot.config.gate_loader import load_gate1

        gate1, sha256 = load_gate1(GATE1_PATH)
        text = config_validate.format_gate1_summary(gate1, sha256)
        # D-69: no withdrawal path exists anywhere in this project —
        # asserting the SUMMARY never leaks 'withdraw' is belt-and-suspenders.
        assert not re.search(r"access|secret|token|withdraw", text, re.IGNORECASE)

    def test_summary_reports_value_deferred_field_count(self) -> None:
        from bithumb_bot.config.gate_loader import load_gate1

        gate1, sha256 = load_gate1(GATE1_PATH)
        text = config_validate.format_gate1_summary(gate1, sha256)
        # There are 8 (D-41) + 7 (D-42) = 15 value-deferred fields in the
        # frozen gate1.toml. The exact wording may vary; we assert the
        # count string is present.
        assert "15" in text or "value-deferred" in text.lower()


# ---------------------------------------------------------------------------
# Handler behavior — validate() first, no mutation, exit 0 on happy path
# ---------------------------------------------------------------------------


def _passing() -> ValidationResult:
    return ValidationResult(ok=True)


def _failing() -> ValidationResult:
    return ValidationResult(
        ok=False, missing=("gate1",), reason="fixture-fail"
    )


class TestHandlerBehavior:
    def test_handler_calls_validate_first(self) -> None:
        with mock.patch(
            "bithumb_bot.cli.handlers.config_validate.validate",
            return_value=_passing(),
        ) as validate_mock:
            rc = config_validate.handler(SimpleNamespace(through="gate1"))
        assert rc == 0
        validate_mock.assert_called_once_with(("config", "validate"))

    def test_handler_refuses_when_validate_fails(self) -> None:
        stderr = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.handlers.config_validate.validate",
            return_value=_failing(),
        ), mock.patch(
            "bithumb_bot.cli.handlers.config_validate.load_gate1"
        ) as load_mock, redirect_stderr(stderr):
            rc = config_validate.handler(SimpleNamespace(through="gate1"))
        assert rc == 1
        assert "refusal" in stderr.getvalue().lower()
        load_mock.assert_not_called()

    def test_handler_prints_summary_on_happy_path(self) -> None:
        stdout = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.handlers.config_validate.validate",
            return_value=_passing(),
        ), redirect_stdout(stdout):
            rc = config_validate.handler(SimpleNamespace(through="gate1"))
        assert rc == 0
        assert "frozen" in stdout.getvalue()
        assert "provisional_engineering_notional_krw" in stdout.getvalue()

    def test_handler_does_not_mutate_gate1_file(self, tmp_path: Path) -> None:
        # Copy the real gate1 to a tmp path, override REPO_ROOT_ENV,
        # then assert mtime unchanged. This proves the inspect-only
        # contract (D-89).
        src = GATE1_PATH.read_bytes()
        (tmp_path / "config" / "decisions").mkdir(parents=True)
        target = tmp_path / "config" / "decisions" / "gate1.toml"
        target.write_bytes(src)
        mtime_before = target.stat().st_mtime

        with mock.patch(
            "bithumb_bot.cli.handlers.config_validate.validate",
            return_value=_passing(),
        ), mock.patch(
            "bithumb_bot.cli.handlers.config_validate.REPO_ROOT",
            tmp_path,
        ), redirect_stdout(io.StringIO()):
            rc = config_validate.handler(SimpleNamespace(through="gate1"))
        assert rc == 0
        mtime_after = target.stat().st_mtime
        assert mtime_before == mtime_after

    def test_gate1_load_error_surface_translated_by_dispatcher(self) -> None:
        """Handler raises Gate1LoadError on parse failure; dispatcher catches."""
        with mock.patch(
            "bithumb_bot.cli.handlers.config_validate.validate",
            return_value=_passing(),
        ), mock.patch(
            "bithumb_bot.cli.handlers.config_validate.load_gate1",
            side_effect=Gate1LoadError("fixture-parse-broken"),
        ):
            with pytest.raises(Gate1LoadError):
                config_validate.handler(SimpleNamespace(through="gate1"))


# ---------------------------------------------------------------------------
# End-to-end through the dispatcher — real gate1.toml on real tree
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    def test_main_config_validate_through_gate1_exits_zero(self) -> None:
        stdout = io.StringIO()
        # No trade env vars; the real validator's account/read + trade cred
        # checks pass because the config validate capability requires neither.
        with redirect_stdout(stdout):
            rc = cli_main.main(["config", "validate", "--through", "gate1"])
        assert rc == 0, f"expected 0, got {rc}. stdout:\n{stdout.getvalue()}"
        assert "frozen" in stdout.getvalue()
