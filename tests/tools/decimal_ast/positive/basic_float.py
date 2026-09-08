# Fixture: `Decimal(<float-literal>)` — MUST be flagged by tools.decimal_ast_check.
# This file is intentionally a D-49 violation; the pre-commit hook excludes
# `tests/tools/decimal_ast/positive/` from its scan (see plan 01-02 task 03).
from decimal import Decimal

x = Decimal(0.1)  # noqa: RUF100 — intentional violation for the AST-checker fixture
