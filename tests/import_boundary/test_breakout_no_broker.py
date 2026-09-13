"""Shadow breakout candidate must never import broker/live-order code.

Three independent checks (mirrors ``tests/import_boundary/
test_paper_no_broker.py`` in shape):

1. AST scan of every ``.py`` file that implements the shadow candidate
   (:mod:`bithumb_bot.strategy.breakout`,
   :mod:`bithumb_bot.paper.breakout_runner`,
   :mod:`bithumb_bot.cli.handlers.paper_breakout_run`).
2. Subprocess import of ``bithumb_bot.paper.breakout_runner`` and
   ``bithumb_bot.strategy.breakout`` never lands a ``bithumb_bot.broker*``
   module in :data:`sys.modules`.
3. The existing ``import-linter`` contract "Paper must not import
   broker" continues to hold (the shadow runner sits inside
   :mod:`bithumb_bot.paper` and is covered by the same contract).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHADOW_FILES = [
    REPO_ROOT / "src" / "bithumb_bot" / "strategy" / "breakout.py",
    REPO_ROOT / "src" / "bithumb_bot" / "paper" / "breakout_runner.py",
    REPO_ROOT / "src" / "bithumb_bot" / "cli" / "handlers" / "paper_breakout_run.py",
]


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


class TestAstScanForbidsBrokerImport:
    def test_no_shadow_module_imports_broker(self) -> None:
        offenders: list[tuple[Path, str]] = []
        for path in SHADOW_FILES:
            assert path.is_file(), f"expected shadow file at {path!s}"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for name in _imported_module_names(tree):
                if name == "bithumb_bot.broker" or name.startswith(
                    "bithumb_bot.broker."
                ):
                    offenders.append((path, name))
        assert offenders == [], (
            "found forbidden bithumb_bot.broker import(s) in the shadow "
            f"candidate: {offenders!r}"
        )


class TestSubprocessImportNeverLoadsBroker:
    def test_importing_shadow_modules_does_not_load_broker(self) -> None:
        snippet = textwrap.dedent(
            """
            import sys
            import bithumb_bot.strategy.breakout  # noqa: F401
            import bithumb_bot.paper.breakout_runner  # noqa: F401
            import bithumb_bot.cli.handlers.paper_breakout_run  # noqa: F401
            leaked = [m for m in sys.modules if m.startswith("bithumb_bot.broker")]
            assert leaked == [], f"broker modules leaked: {leaked!r}"
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
            f"subprocess check failed.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "OK" in result.stdout


class TestImportLinterContractPaperNoBrokerStillHolds:
    def test_paper_no_broker_contract_kept(self) -> None:
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
