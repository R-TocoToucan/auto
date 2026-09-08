"""Import Linter contract tests.

Two sides of the D-73 architectural-violation regression:

* Positive: the real ``bithumb_bot`` tree passes the ``Core must not
  import broker`` contract (``lint-imports`` exits 0).
* Negative: the committed ``tests/fixtures/import_linter_violation/``
  package fails the contract (``lint-imports`` exits non-zero AND
  stdout names the contract).

Together they prove the contract mechanically enforces the D-71
boundary — the pre-commit hook and CI both invoke ``lint-imports``.
"""
