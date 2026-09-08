"""Deliberate boundary violation — imports from `badpkg.broker`.

The Import Linter contract inside this fixture's own ``pyproject.toml``
forbids ``badpkg.core`` from importing ``badpkg.broker``; this file
exists to prove the contract mechanically fails on a real violation
(D-73 negative fixture).
"""

from badpkg.broker import x  # noqa: F401 — intentional boundary violation
