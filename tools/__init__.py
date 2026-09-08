"""Repo-owned developer tools that ship with the project.

Currently the only tool is `tools.decimal_ast_check` — the Python-AST
checker that rejects `Decimal(<float-literal>)` at authoring time
(D-49 / D-72). See that module's docstring for CLI usage.

This package is intentionally dependency-free so it can run from a fresh
clone before `uv sync` if needed (D-73 pre-commit / CI requirement).
"""
