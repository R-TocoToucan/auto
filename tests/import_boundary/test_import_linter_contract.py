"""D-73 Import Linter contract tests — positive + negative regression.

The positive side invokes the committed contract against the real repo
tree via ``lint-imports`` and asserts exit code 0. The negative side
invokes ``lint-imports --config <fixture>/pyproject.toml`` against the
committed intentional-violation fixture and asserts non-zero exit AND
that stdout names the contract (``Core must not import broker``).

Both subprocess invocations run through ``sys.executable`` invoking
:pyfunc:`importlinter.cli.lint_imports` directly — this avoids
platform-specific differences between the ``lint-imports.exe`` shim
on Windows and the ``lint-imports`` shell script on POSIX, and avoids
depending on the ``import-linter`` package installing a ``__main__``.

Windows ``cp949`` note: Rich (which import-linter uses for pretty output)
writes non-ASCII glyphs; ``PYTHONIOENCODING=utf-8`` is set on the child
environment to prevent ``UnicodeEncodeError`` on the default Windows
console codec.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "import_linter_violation"
FIXTURE_PYPROJECT = FIXTURE_DIR / "pyproject.toml"


def _run_lint(config: str | None, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke ``importlinter.cli.lint_imports`` via ``sys.executable``.

    Passing ``config=None`` runs against the pyproject.toml auto-discovered
    from ``cwd``. Passing a config path invokes the checker against that
    explicit configuration.
    """
    if config is None:
        config_repr = "None"
    else:
        config_repr = repr(config)
    snippet = textwrap.dedent(
        f"""
        import sys
        from importlinter.cli import lint_imports
        exit_code = lint_imports(config_filename={config_repr})
        sys.exit(exit_code)
        """
    ).strip()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


class TestImportLinterContract:
    def test_real_tree_passes(self) -> None:
        """`lint-imports` exits 0 against the real committed tree (D-71)."""
        result = _run_lint(config=None, cwd=REPO_ROOT)
        assert result.returncode == 0, (
            f"lint-imports failed on the real tree.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        # Sanity: the contract name appears in the output.
        assert "Core must not import broker" in result.stdout

    def test_negative_fixture_fails(self) -> None:
        """The committed intentional-violation fixture MUST fail (D-73)."""
        assert FIXTURE_PYPROJECT.is_file(), (
            f"fixture pyproject.toml missing at {FIXTURE_PYPROJECT!s}"
        )
        # Snapshot mtimes so we can prove the fixture files are never mutated
        # by the checker (belt-and-suspenders per the plan Oracle).
        tracked_fixture_files = sorted(FIXTURE_DIR.rglob("*"))
        mtimes_before = {
            p: p.stat().st_mtime for p in tracked_fixture_files if p.is_file()
        }

        result = _run_lint(config=str(FIXTURE_PYPROJECT), cwd=FIXTURE_DIR)
        assert result.returncode != 0, (
            f"lint-imports unexpectedly passed on the negative fixture.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "Core must not import broker" in result.stdout, (
            "negative fixture output did not name the contract; got:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        mtimes_after = {
            p: p.stat().st_mtime for p in tracked_fixture_files if p.is_file()
        }
        assert mtimes_before == mtimes_after, (
            "fixture files were mutated during the test run — the checker "
            "must never write into the fixture tree."
        )


@pytest.mark.slow
class TestImportLinterContractSlow:
    """Reserved slot for a `pytest.mark.slow` version if subprocess spin-up
    ever exceeds the 10s CI budget in `01-VALIDATION.md`. Not populated
    now — both invocations above complete in well under a second locally.
    """

    def test_placeholder_stays_green(self) -> None:
        assert True
