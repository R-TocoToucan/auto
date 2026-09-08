"""Task 01-03-05 — `bt` CLI dispatcher (argparse + validate-before-dispatch).

Behavior contract exercised here:

* ``--help`` / ``--version`` / empty argv are handled by argparse BEFORE
  any registry lookup, credential load, or ``BithumbSecrets`` construction
  occurs (T-1-03-04).
* Unknown verb → stderr contains ``unknown command``; exit non-zero
  (D-89 fail-hard).
* Registered verb → ``validate(capability)`` is called first; only if
  ``ok=True`` does the handler execute (T-1-03-02).
* ``validate()`` failing → handler NEVER runs; stderr contains ``refusal:``;
  exit ``1``.
* ``config validate --through gate1`` is recognized by argparse.

These tests intentionally patch the dispatcher's HANDLER_MAP or the
handler resolver so this task (01-03-05) can land BEFORE the real
handler modules exist (which arrive in 01-03-06 / 01-03-07 / 01-03-09).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from bithumb_bot.cli import dispatcher, main as cli_main
from bithumb_bot.config.validator import ValidationResult


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# --help / --version / empty argv — no side effects (T-1-03-04)
# ---------------------------------------------------------------------------


class TestHelpAndVersionAreSideEffectFree:
    def test_help_returns_zero(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), pytest.raises(SystemExit) as excinfo:
            cli_main.main(["--help"])
        assert excinfo.value.code == 0
        assert "usage" in stdout.getvalue().lower()

    def test_help_does_not_load_secrets(self) -> None:
        """`bt --help` MUST NOT construct BithumbSecrets (T-1-03-04)."""
        with mock.patch(
            "bithumb_bot.secrets.settings.BithumbSecrets"
        ) as secrets_ctor:
            with pytest.raises(SystemExit):
                cli_main.main(["--help"])
        secrets_ctor.assert_not_called()

    def test_help_does_not_call_validate(self) -> None:
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate"
        ) as validate_mock:
            with pytest.raises(SystemExit):
                cli_main.main(["--help"])
        validate_mock.assert_not_called()

    def test_version_returns_zero(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), pytest.raises(SystemExit) as excinfo:
            cli_main.main(["--version"])
        assert excinfo.value.code == 0
        # Some argparse configurations print a bare version string; we only
        # care that SOMETHING was written and the exit was clean.
        assert stdout.getvalue().strip() != ""

    def test_version_does_not_call_validate(self) -> None:
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate"
        ) as validate_mock:
            with pytest.raises(SystemExit):
                cli_main.main(["--version"])
        validate_mock.assert_not_called()

    def test_empty_argv_no_side_effects_and_no_secrets_load(self) -> None:
        """Empty argv: acceptable either as `0` (help printed) or `2` (usage error).

        The critical invariant is NO capability lookup / credential load
        / validate() call, per T-1-03-04. Argparse's own behavior may
        differ between versions; both `0` and `2` are documented as
        acceptable in the plan Oracle.
        """
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate"
        ) as validate_mock, mock.patch(
            "bithumb_bot.secrets.settings.BithumbSecrets"
        ) as secrets_ctor:
            # main() may exit via SystemExit or return the code directly.
            try:
                rc = cli_main.main([])
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 2
        assert rc in (0, 2)
        validate_mock.assert_not_called()
        secrets_ctor.assert_not_called()

    def test_help_via_subprocess_env_with_sentinel_creds_does_not_leak(
        self, tmp_path: Path
    ) -> None:
        """`bt --help` MUST NOT print a sentinel credential value (T-1-03-04)."""
        sentinel = "supersecretsentinel1234567890"
        env = os.environ.copy()
        env["BITHUMB_ACCOUNT_READ_ACCESS_KEY"] = sentinel
        env["BITHUMB_ACCOUNT_READ_SECRET_KEY"] = sentinel
        env["PYTHONIOENCODING"] = "utf-8"
        # Prevent inherited trade creds from tripping the trade-cred check.
        env.pop("BITHUMB_TRADE_ACCESS_KEY", None)
        env.pop("BITHUMB_TRADE_SECRET_KEY", None)
        result = subprocess.run(
            [sys.executable, "-m", "bithumb_bot.cli.main", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        assert result.returncode == 0
        assert sentinel not in result.stdout
        assert sentinel not in result.stderr


# ---------------------------------------------------------------------------
# Unknown verb — fail hard (D-89)
# ---------------------------------------------------------------------------


class TestUnknownVerbFailsHard:
    def test_bogus_verb_returns_nonzero_and_stderr_mentions_unknown(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            try:
                rc = cli_main.main(["bogus", "verb"])
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
        assert rc != 0, "unknown verb must fail non-zero"
        assert "unknown" in stderr.getvalue().lower() or "invalid" in stderr.getvalue().lower()


# ---------------------------------------------------------------------------
# Validate-before-dispatch sequence (T-1-03-02)
# ---------------------------------------------------------------------------


def _passing_result() -> ValidationResult:
    return ValidationResult(ok=True)


def _failing_result() -> ValidationResult:
    return ValidationResult(
        ok=False, missing=("gate1",), reason="fixture-fail"
    )


class TestValidateBeforeDispatch:
    def test_valid_capability_calls_validate_and_then_handler(self) -> None:
        """`main(['m0','selfcheck'])` calls validate first, then handler exactly once."""
        # Order-recording mock — validate MUST be called before handler.
        call_log: list[str] = []
        fake_handler = mock.Mock(
            side_effect=lambda args: call_log.append("handler") or 0
        )

        def fake_validate(cap: tuple[str, str]) -> ValidationResult:
            call_log.append("validate")
            return _passing_result()

        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate", side_effect=fake_validate
        ) as validate_mock, mock.patch.dict(
            dispatcher.HANDLER_MAP,
            {("m0", "selfcheck"): fake_handler},
            clear=False,
        ):
            rc = cli_main.main(["m0", "selfcheck"])
        assert rc == 0
        assert call_log == ["validate", "handler"], call_log
        validate_mock.assert_called_once_with(("m0", "selfcheck"))
        fake_handler.assert_called_once()

    def test_validate_ok_false_handler_never_runs_and_stderr_contains_refusal(
        self,
    ) -> None:
        stderr = io.StringIO()
        fake_handler = mock.Mock(return_value=0)
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            return_value=_failing_result(),
        ), mock.patch.dict(
            dispatcher.HANDLER_MAP,
            {("m0", "selfcheck"): fake_handler},
            clear=False,
        ), redirect_stderr(stderr):
            rc = cli_main.main(["m0", "selfcheck"])
        assert rc == 1, "refusal must exit 1"
        assert "refusal" in stderr.getvalue().lower()
        fake_handler.assert_not_called()

    def test_config_validate_recognizes_through_gate1_flag(self) -> None:
        """`bt config validate --through gate1` must argparse-parse cleanly."""
        seen_args: dict[str, Any] = {}

        def capture(args: Any) -> int:
            seen_args["through"] = getattr(args, "through", None)
            return 0

        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            return_value=_passing_result(),
        ), mock.patch.dict(
            dispatcher.HANDLER_MAP,
            {("config", "validate"): capture},
            clear=False,
        ):
            rc = cli_main.main(["config", "validate", "--through", "gate1"])
        assert rc == 0
        assert seen_args.get("through") == "gate1"


# ---------------------------------------------------------------------------
# Named-error translation — exception → clean stderr + exit 1
# ---------------------------------------------------------------------------


class TestNamedErrorsTranslated:
    def test_unknown_capability_error_from_validate_produces_clean_exit(
        self,
    ) -> None:
        """A capability accepted by argparse but not in REGISTRY MUST fail cleanly.

        This is defense in depth for a future case where argparse allows a
        subverb the registry does not have (e.g., a stale wiring). The
        dispatcher must NOT emit a raw traceback for a documented refusal.
        """
        from bithumb_bot.errors import UnknownCapabilityError

        stderr = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            side_effect=UnknownCapabilityError("fixture-unknown"),
        ), mock.patch.dict(
            dispatcher.HANDLER_MAP,
            {("m0", "selfcheck"): mock.Mock(return_value=0)},
            clear=False,
        ), redirect_stderr(stderr):
            rc = cli_main.main(["m0", "selfcheck"])
        assert rc == 1
        assert "unknown" in stderr.getvalue().lower() or "fixture-unknown" in stderr.getvalue()

    def test_gate1_load_error_translated(self) -> None:
        from bithumb_bot.errors import Gate1LoadError

        stderr = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            side_effect=Gate1LoadError("fixture-gate1-broken"),
        ), mock.patch.dict(
            dispatcher.HANDLER_MAP,
            {("m0", "selfcheck"): mock.Mock(return_value=0)},
            clear=False,
        ), redirect_stderr(stderr):
            rc = cli_main.main(["m0", "selfcheck"])
        assert rc == 1
        assert "gate1" in stderr.getvalue().lower() or "fixture-gate1-broken" in stderr.getvalue()


# ---------------------------------------------------------------------------
# Handler map surface — every Phase-1 D-86 verb IS wired
# ---------------------------------------------------------------------------


class TestHandlerMapSurface:
    def test_phase1_verbs_are_all_wired(self) -> None:
        expected: set[tuple[str, str]] = {
            ("config", "validate"),
            ("m0", "selfcheck"),
            ("m1", "fetch-spec"),
            ("m1", "verify-facts"),
            ("m1", "verify-snapshot"),
        }
        assert expected.issubset(dispatcher.HANDLER_MAP.keys())

    def test_no_m6b_or_live_verbs_in_handler_map(self) -> None:
        for verb, subverb in dispatcher.HANDLER_MAP:
            # D-69: no withdrawal path exists in this project — asserting
            # 'withdraw' cannot appear as a verb name is belt-and-suspenders.
            assert verb not in ("m6b", "live", "withdraw"), (verb, subverb)
