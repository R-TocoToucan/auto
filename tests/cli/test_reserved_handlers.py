"""Task 01-03-08 — D-87 reserved-verb handler tests.

Behavior contract (from plan 01-03 §Task 01-03-08):

* Shared ``reserved_handler(args)`` prints
  ``"reserved for a future phase — do not scaffold"`` to stderr and
  returns ``1``. No traceback.
* Every D-87 verb is registered in the argparse tree AND in
  ``HANDLER_MAP`` (bound to ``reserved_handler``).
* Argparse ``--help`` for each reserved subparser labels the subparser
  ``RESERVED — future phase, not implemented``.
* No side effect: no HTTP request, no file mutation. Verified by mocking
  ``httpx.AsyncClient`` and asserting it was never called.
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bithumb_bot.cli import dispatcher, main as cli_main
from bithumb_bot.cli.handlers import reserved as reserved_module
from bithumb_bot.config.capability_registry import RESERVED_KEYS
from bithumb_bot.config.validator import ValidationResult


REPO_ROOT = Path(__file__).resolve().parents[2]


def _passing() -> ValidationResult:
    return ValidationResult(ok=True)


RESERVED_PAIRS = sorted(RESERVED_KEYS)


# ---------------------------------------------------------------------------
# Reserved handler — unit test
# ---------------------------------------------------------------------------


class TestReservedHandlerReturnsRefusal:
    def test_reserved_handler_returns_1_and_prints_message(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = reserved_module.reserved_handler(SimpleNamespace(verb="m2", subverb="collect-observations"))
        assert rc == 1
        assert "reserved for a future phase" in stderr.getvalue().lower()

    def test_reserved_handler_never_raises(self) -> None:
        """Reserved handler MUST NOT raise NotImplementedError at runtime.

        Raising would emit an unclean traceback for a documented refusal.
        Return-code refusal is the D-90 discipline.
        """
        try:
            rc = reserved_module.reserved_handler(SimpleNamespace(verb="m4", subverb="evaluate-selection"))
        except NotImplementedError:
            pytest.fail("reserved_handler must NOT raise at runtime (D-90 discipline)")
        assert rc == 1


# ---------------------------------------------------------------------------
# Parametrized end-to-end: every D-87 pair refuses via main()
# ---------------------------------------------------------------------------


class TestEveryReservedVerbRefuses:
    @pytest.mark.parametrize(("verb", "subverb"), RESERVED_PAIRS)
    def test_reserved_verb_returns_nonzero_with_reserved_message(
        self, verb: str, subverb: str
    ) -> None:
        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            return_value=_passing(),
        ), redirect_stderr(stderr), redirect_stdout(stdout):
            rc = cli_main.main([verb, subverb])
        assert rc != 0, (
            f"reserved verb {verb} {subverb} unexpectedly returned 0"
        )
        combined = stderr.getvalue() + stdout.getvalue()
        assert "reserved for a future phase" in combined.lower(), (
            f"reserved-message missing for {verb} {subverb}. output:\n{combined}"
        )

    @pytest.mark.parametrize(("verb", "subverb"), RESERVED_PAIRS)
    def test_reserved_verb_wired_in_handler_map(
        self, verb: str, subverb: str
    ) -> None:
        assert (verb, subverb) in dispatcher.HANDLER_MAP


# ---------------------------------------------------------------------------
# No side effects — no HTTP, no file writes in tmp_path
# ---------------------------------------------------------------------------


class TestReservedVerbsHaveNoSideEffects:
    @pytest.mark.parametrize(("verb", "subverb"), RESERVED_PAIRS)
    def test_no_httpx_asyncclient_instantiated(
        self, verb: str, subverb: str, tmp_path: Path
    ) -> None:
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            return_value=_passing(),
        ), mock.patch("httpx.AsyncClient") as httpx_mock, redirect_stderr(
            io.StringIO()
        ), redirect_stdout(io.StringIO()):
            rc = cli_main.main([verb, subverb])
        assert rc != 0
        httpx_mock.assert_not_called()

    @pytest.mark.parametrize(("verb", "subverb"), RESERVED_PAIRS)
    def test_no_files_created_in_tmp_path(
        self, verb: str, subverb: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point cwd at tmp_path so any accidental relative-path write
        # would land there — then assert tmp_path stays empty.
        monkeypatch.chdir(tmp_path)
        with mock.patch(
            "bithumb_bot.cli.dispatcher.validate",
            return_value=_passing(),
        ), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli_main.main([verb, subverb])
        assert rc != 0
        # tmp_path should have no new files after the reserved-verb call.
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Argparse --help labels reserved subparsers RESERVED
# ---------------------------------------------------------------------------


class TestReservedSubparserHelpLabelling:
    @pytest.mark.parametrize(("verb", "subverb"), RESERVED_PAIRS)
    def test_reserved_help_contains_reserved_label(
        self, verb: str, subverb: str
    ) -> None:
        env = {"PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, "-m", "bithumb_bot.cli.main", verb, subverb, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**env, "PATH": ""},
            check=False,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        # `RESERVED` label is placed by _build_parser reserved subparser wiring.
        assert "RESERVED" in combined, (
            f"reserved label missing from --help for {verb} {subverb}:\n{combined}"
        )
