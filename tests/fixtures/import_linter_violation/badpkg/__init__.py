"""Negative-fixture package for the Import Linter contract test (D-73).

Not imported by production code. Never mutated at test time — the fixture
tree is committed to prove the ``Core must not import broker`` contract
shape can *mechanically* fail on a real violation.

See ``tests/import_boundary/test_import_linter_contract.py``.
"""
