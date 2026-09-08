"""Fixture root for the `tools.decimal_ast_check` test suite.

The `positive/`, `negative/`, `syntax_error/` subdirectories are
intentionally NOT Python packages — they are treated by the AST checker
as raw source-file inputs, not as importable modules. Keeping them out
of the package tree also keeps pytest's own collector from trying to
import fixture files that (deliberately) contain `Decimal(<float>)`.
"""
