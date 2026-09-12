"""`bithumb_bot.paper` must never import `bithumb_bot.broker` — hard-requirement #7.

Three independent checks, per the plan:

1. An AST scan of every ``.py`` file under ``src/bithumb_bot/paper/``
   asserting no ``import bithumb_bot.broker`` / ``from bithumb_bot.broker``
   statement (or an aliased submodule import) exists.
2. A subprocess check that importing ``bithumb_bot.paper.runner`` never
   lands a ``bithumb_bot.broker*`` module in ``sys.modules``.
3. The dedicated ``import-linter`` contract "Paper must not import
   broker" (``pyproject.toml``), invoked the same way
   ``tests/import_boundary/test_import_linter_contract.py`` invokes the
   pre-existing "Core must not import broker" contract.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPER_SRC = REPO_ROOT / "src" / "bithumb_bot" / "paper"


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


class TestAstScanForbidsBrokerImport:
    def test_no_paper_module_imports_broker(self) -> None:
        assert PAPER_SRC.is_dir(), f"expected paper package at {PAPER_SRC!s}"
        py_files = sorted(PAPER_SRC.rglob("*.py"))
        assert py_files, "expected at least one .py file under bithumb_bot.paper"
        offenders: list[tuple[Path, str]] = []
        for path in py_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in _imported_module_names(tree):
                if name == "bithumb_bot.broker" or name.startswith(
                    "bithumb_bot.broker."
                ):
                    offenders.append((path, name))
        assert offenders == [], (
            f"found forbidden bithumb_bot.broker import(s) in the paper "
            f"package: {offenders!r}"
        )


class TestSubprocessImportNeverLoadsBroker:
    def test_importing_paper_runner_does_not_load_broker(self) -> None:
        snippet = textwrap.dedent(
            """
            import sys
            import bithumb_bot.paper.runner  # noqa: F401
            leaked = [m for m in sys.modules if m.startswith("bithumb_bot.broker")]
            assert leaked == [], f"broker modules leaked into sys.modules: {leaked!r}"
            print("OK")
            """
        ).strip()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, (
            f"subprocess import check failed.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "OK" in result.stdout


class TestImportLinterContractPaperNoBroker:
    def test_paper_no_broker_contract_kept(self) -> None:
        """Cross-references the "Paper must not import broker" contract by
        name (pyproject.toml) — a future change that introduces a broker
        import into bithumb_bot.paper fails the build via this contract,
        not just via the AST scan above."""
        snippet = textwrap.dedent(
            """
            import sys
            from importlinter.cli import lint_imports
            exit_code = lint_imports(config_filename=None, no_cache=False)
            sys.exit(exit_code)
            """
        ).strip()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, (
            f"lint-imports failed on the real tree.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "Paper must not import broker" in result.stdout
