"""Fixture-driven tests for `tools.decimal_ast_check`.

Every positive fixture MUST produce at least one `DECIMAL_FROM_FLOAT`
finding (exit code 1). Every negative fixture MUST produce zero findings
(exit code 0). The syntax-error fixture MUST produce exit code 2 with a
`SYNTAX_ERROR:` line on stderr — never exit 0 or 1.

Runs the checker as a subprocess (`sys.executable -m tools.decimal_ast_check`)
so the CLI surface — argv parsing, exit codes, stdout / stderr channels —
is exercised end-to-end, not just the internal API.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tools.decimal_ast_check as decimal_ast_check


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT = Path(__file__).resolve().parent / "decimal_ast"
_POSITIVE_ROOT = _FIXTURE_ROOT / "positive"
_NEGATIVE_ROOT = _FIXTURE_ROOT / "negative"
_SYNTAX_ERROR_ROOT = _FIXTURE_ROOT / "syntax_error"


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_checker(*paths: Path) -> subprocess.CompletedProcess[str]:
    """Invoke `python -m tools.decimal_ast_check <paths>...` from repo root."""
    return subprocess.run(
        [sys.executable, "-m", "tools.decimal_ast_check", *[str(p) for p in paths]],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Positive fixtures — MUST be flagged
# ---------------------------------------------------------------------------


_POSITIVE_FIXTURES = sorted(_POSITIVE_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "fixture",
    _POSITIVE_FIXTURES,
    ids=[str(p.relative_to(_POSITIVE_ROOT)) for p in _POSITIVE_FIXTURES],
)
def test_positive_fixture_is_flagged(fixture: Path) -> None:
    proc = _run_checker(fixture)
    assert proc.returncode == 1, (
        f"expected exit 1 for {fixture}, got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert "DECIMAL_FROM_FLOAT" in proc.stdout, (
        f"expected DECIMAL_FROM_FLOAT finding in stdout for {fixture}, "
        f"got: {proc.stdout!r}"
    )
    # Finding line format: <path>:<lineno>:<col>: DECIMAL_FROM_FLOAT
    matching = [
        line
        for line in proc.stdout.splitlines()
        if line.endswith(": DECIMAL_FROM_FLOAT")
    ]
    assert matching, f"no correctly-formatted finding line: {proc.stdout!r}"
    # First finding line must name the fixture path.
    fixture_str = str(fixture)
    assert any(fixture_str in line or fixture.name in line for line in matching), (
        f"no finding line named the fixture path: {proc.stdout!r}"
    )


def test_positive_fixture_count() -> None:
    """Ensure the positive-fixture set covers every documented case."""
    names = {p.name for p in _POSITIVE_FIXTURES}
    required = {
        "basic_float.py",
        "signed_float.py",
        "module_alias.py",
        "decimal_module.py",
        "name_alias_D.py",
        "complex_literal.py",
        "violation.py",  # nested/deeper/
    }
    assert required.issubset(names), (
        f"missing positive fixtures: {required - names}"
    )


# ---------------------------------------------------------------------------
# Negative fixtures — MUST NOT be flagged
# ---------------------------------------------------------------------------


_NEGATIVE_FIXTURES = sorted(_NEGATIVE_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "fixture",
    _NEGATIVE_FIXTURES,
    ids=[str(p.relative_to(_NEGATIVE_ROOT)) for p in _NEGATIVE_FIXTURES],
)
def test_negative_fixture_is_not_flagged(fixture: Path) -> None:
    proc = _run_checker(fixture)
    assert proc.returncode == 0, (
        f"expected exit 0 for {fixture}, got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert "DECIMAL_FROM_FLOAT" not in proc.stdout, (
        f"unexpected finding for negative fixture {fixture}: {proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Syntax-error fixtures — exit 2, stderr contains SYNTAX_ERROR
# ---------------------------------------------------------------------------


def test_syntax_error_fixture_exits_2(tmp_path: Path) -> None:
    src = _SYNTAX_ERROR_ROOT / "broken.py.txt"
    target = tmp_path / "broken.py"
    shutil.copyfile(src, target)
    proc = _run_checker(target)
    assert proc.returncode == 2, (
        f"expected exit 2 on syntax error, got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert "SYNTAX_ERROR" in proc.stderr, (
        f"expected SYNTAX_ERROR on stderr, got: {proc.stderr!r}"
    )
    assert "DECIMAL_FROM_FLOAT" not in proc.stdout, (
        f"syntax-error file must not produce findings: {proc.stdout!r}"
    )


def test_syntax_error_does_not_mask_findings(tmp_path: Path) -> None:
    """If both a syntax-error file and a violating file are scanned, exit is
    still 2 (the most-severe outcome) but the violation is still reported."""
    broken = tmp_path / "broken.py"
    shutil.copyfile(_SYNTAX_ERROR_ROOT / "broken.py.txt", broken)
    good_violation = tmp_path / "violation.py"
    good_violation.write_text(
        "from decimal import Decimal\nx = Decimal(0.5)\n", encoding="utf-8"
    )
    proc = _run_checker(broken, good_violation)
    assert proc.returncode == 2
    assert "DECIMAL_FROM_FLOAT" in proc.stdout
    assert "SYNTAX_ERROR" in proc.stderr


# ---------------------------------------------------------------------------
# Directory-recursion behavior
# ---------------------------------------------------------------------------


def test_positive_directory_recursive_scan() -> None:
    """Scanning the whole `positive/` root discovers the nested fixture."""
    proc = _run_checker(_POSITIVE_ROOT)
    assert proc.returncode == 1
    # The nested-fixture path should appear in at least one finding line.
    assert "violation.py" in proc.stdout, (
        f"nested fixture (positive/nested/deeper/violation.py) not "
        f"discovered by recursive scan: {proc.stdout!r}"
    )


def test_negative_directory_scan_exits_clean() -> None:
    proc = _run_checker(_NEGATIVE_ROOT)
    assert proc.returncode == 0
    assert "DECIMAL_FROM_FLOAT" not in proc.stdout


# ---------------------------------------------------------------------------
# Finding-line format
# ---------------------------------------------------------------------------


def test_finding_line_format_has_line_and_col() -> None:
    fixture = _POSITIVE_ROOT / "basic_float.py"
    proc = _run_checker(fixture)
    assert proc.returncode == 1
    lines = [
        line for line in proc.stdout.splitlines() if "DECIMAL_FROM_FLOAT" in line
    ]
    assert lines, f"no finding lines: {proc.stdout!r}"
    # Each finding: <path>:<lineno>:<col_offset>: DECIMAL_FROM_FLOAT
    for line in lines:
        head, tag = line.rsplit(": ", 1)
        assert tag == "DECIMAL_FROM_FLOAT"
        parts = head.rsplit(":", 2)
        assert len(parts) == 3, f"malformed head: {head!r}"
        int(parts[1])  # lineno numeric
        int(parts[2])  # col_offset numeric


# ---------------------------------------------------------------------------
# Docstring pointer — enforce the non-goal note is documented in the module.
# ---------------------------------------------------------------------------


def test_module_docstring_declares_explicit_non_goals() -> None:
    """The checker's docstring must document its scope boundary (D-72)."""
    doc = decimal_ast_check.__doc__ or ""
    assert "explicit non-goal" in doc.lower(), (
        "module docstring must call out its 'explicit non-goal' scope note"
    )
