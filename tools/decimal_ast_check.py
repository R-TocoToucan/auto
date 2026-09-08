"""Repo-owned Python-AST checker: reject ``Decimal(<float-literal>)``.

Enforces **D-49** (``Decimal`` never from ``float``) and **D-72**
(repo-owned AST checker rather than a Ruff plugin) as a pre-commit hook
and CI job (**D-73**). Wired into ``.pre-commit-config.yaml`` by plan
01-02 task 01-02-03.

CLI
---
    python -m tools.decimal_ast_check <path>...

Walks each ``<path>`` recursively for ``*.py`` files. Prints each finding
on its own line as::

    <path>:<lineno>:<col_offset>: DECIMAL_FROM_FLOAT

Exits:

* ``0`` — no findings
* ``1`` — at least one finding
* ``2`` — one or more input files failed to parse (syntax error)

Detection strategy
------------------
The checker tracks module and name aliases across the file::

    import decimal                    → {"decimal": "decimal"}
    import decimal as dec             → {"dec": "decimal"}
    from decimal import Decimal       → {"Decimal": "decimal.Decimal"}
    from decimal import Decimal as D  → {"D": "decimal.Decimal"}

Every ``Call`` whose callable resolves through the alias table to
``decimal.Decimal`` is inspected. The first positional argument is
unwrapped ONE level of ``UnaryOp(UAdd | USub, ...)`` and, if the inner
node is a ``Constant`` whose ``.value`` is ``float`` or ``complex``, the
call is flagged.

Explicit non-goals (deliberate false-negatives)
-----------------------------------------------
This is an **explicit non-goal** list per D-72 scope + planning
Finding 2. The checker DOES NOT try to detect:

* Re-binding an alias mid-file (``Decimal = something_else``).
* Cross-file / cross-module inference.
* Dynamic-argument type inference — ``Decimal(x)`` where ``x`` is a
  ``Name``, ``Call``, ``Attribute``, or any non-``Constant``
  expression is NOT flagged.
* Star imports (``from decimal import *``).
* ``Decimal.from_float(...)`` — that constructor makes the
  float-to-Decimal conversion *explicit* and callers who reach for
  it are doing so with intent.
* Locally-defined callables named ``Decimal`` (or ``D``) that never
  come from stdlib ``decimal``.

The checker is dependency-free (stdlib only) so it can run from a fresh
clone before ``uv sync``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

_STDLIB_DECIMAL_MODULE = "decimal"
_DECIMAL_FQN = "decimal.Decimal"
_FINDING_TAG = "DECIMAL_FROM_FLOAT"


class _AliasVisitor(ast.NodeVisitor):
    """Collect module and name aliases for ``decimal`` / ``Decimal`` in a file.

    Only stdlib ``decimal`` and its ``Decimal`` symbol are tracked; every
    other import is intentionally ignored.
    """

    def __init__(self) -> None:
        # Local-module-alias -> the (only) tracked FQN "decimal".
        self.module_aliases: dict[str, str] = {}
        # Local-name-alias -> the (only) tracked FQN "decimal.Decimal".
        self.name_aliases: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == _STDLIB_DECIMAL_MODULE:
                local = alias.asname or alias.name
                self.module_aliases[local] = _STDLIB_DECIMAL_MODULE
        # Do not recurse — Import has no relevant children.

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module != _STDLIB_DECIMAL_MODULE:
            return
        for alias in node.names:
            if alias.name == "Decimal":
                local = alias.asname or alias.name
                self.name_aliases[local] = _DECIMAL_FQN


class _CallVisitor(ast.NodeVisitor):
    """Walk ``Call`` nodes and flag ``Decimal(<float-or-complex-literal>)``.

    Uses the alias tables produced by :class:`_AliasVisitor` to determine
    whether a callable resolves to stdlib ``decimal.Decimal``.
    """

    def __init__(
        self,
        module_aliases: dict[str, str],
        name_aliases: dict[str, str],
    ) -> None:
        self._module_aliases = module_aliases
        self._name_aliases = name_aliases
        self.findings: list[tuple[int, int]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if self._resolves_to_decimal(node.func) and node.args:
            first = node.args[0]
            # Unwrap ONE level of unary +/- to catch Decimal(-0.1)/Decimal(+0.1).
            inner: ast.AST = first
            if isinstance(first, ast.UnaryOp) and isinstance(
                first.op, (ast.UAdd, ast.USub)
            ):
                inner = first.operand
            if isinstance(inner, ast.Constant) and isinstance(
                inner.value, (float, complex)
            ):
                self.findings.append((node.lineno, node.col_offset))
        self.generic_visit(node)

    def _resolves_to_decimal(self, func: ast.expr) -> bool:
        # `Decimal(...)` / `D(...)` — a bare Name.
        if isinstance(func, ast.Name):
            return self._name_aliases.get(func.id) == _DECIMAL_FQN
        # `decimal.Decimal(...)` / `dec.Decimal(...)` — Attribute on a Name.
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.attr != "Decimal":
                return False
            return (
                self._module_aliases.get(func.value.id) == _STDLIB_DECIMAL_MODULE
            )
        return False


def _iter_py_files(paths: Iterable[Path]) -> Iterator[Path]:
    """Yield every ``*.py`` file under ``paths`` (recursive on directories).

    Files are de-duplicated by their resolved absolute path so a directory
    plus one of its files, or the same directory listed twice, yields each
    file only once. Iteration order is deterministic (``rglob`` sorted).
    """
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*.py")):
                resolved = child.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield child
        elif path.is_file() and path.suffix == ".py":
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path
        # Non-.py files / non-existent paths are silently skipped; the caller
        # (pre-commit / CI) is responsible for passing a meaningful path list.


def _check_file(path: Path) -> tuple[list[tuple[int, int]], str | None]:
    """Scan a single file.

    Returns:
        A pair ``(findings, syntax_error_message)`` where exactly one is
        non-empty. On successful parse ``findings`` is a possibly-empty
        list of ``(lineno, col_offset)`` pairs and ``syntax_error_message``
        is ``None``. On a syntax error ``findings`` is empty and
        ``syntax_error_message`` is a short human-readable string.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return (
            [],
            f"{type(exc).__name__}: {exc.msg} (line {exc.lineno}, col {exc.offset})",
        )
    alias_v = _AliasVisitor()
    alias_v.visit(tree)
    call_v = _CallVisitor(alias_v.module_aliases, alias_v.name_aliases)
    call_v.visit(tree)
    return call_v.findings, None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.decimal_ast_check",
        description=(
            "Reject `Decimal(<float-literal>)` at authoring time via "
            "alias-aware AST inspection. Enforces D-49 (Decimal never from "
            "float) and D-72 (repo-owned checker)."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Files or directories to scan (recursive on directories).",
    )
    args = parser.parse_args(argv)

    exit_code = 0
    for path in _iter_py_files(args.paths):
        findings, syntax_err = _check_file(path)
        if syntax_err is not None:
            print(f"{path}: SYNTAX_ERROR: {syntax_err}", file=sys.stderr)
            if exit_code < 2:
                exit_code = 2
            continue
        for lineno, col in findings:
            print(f"{path}:{lineno}:{col}: {_FINDING_TAG}")
            if exit_code < 1:
                exit_code = 1
    return exit_code


if __name__ == "__main__":  # pragma: no cover — module CLI entry
    raise SystemExit(main())
