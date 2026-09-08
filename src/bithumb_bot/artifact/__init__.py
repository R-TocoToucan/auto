"""`bithumb_bot.artifact` — reusable immutable-artifact primitives.

Two small submodules:

* :mod:`bithumb_bot.artifact.canonical` — canonical JSON serialization,
  SHA-256 hashing, atomic write + sidecar, and the "never overwrite a
  previously consumed snapshot" guard (D-76).
* :mod:`bithumb_bot.artifact.timestamps` — Windows-safe UTC timestamps
  with no colons in the string form (D-74).

These modules are deliberately generic — Phase 2's Parquet candle store
will reuse the same primitives for its per-file integrity check. Neither
module imports anything from :mod:`bithumb_bot.bithumb_spec`.
"""

from __future__ import annotations

from bithumb_bot.artifact.canonical import (
    atomic_write,
    canonical_bytes,
    guard_against_overwrite,
    sha256_hex,
    sidecar_line,
    write_with_sidecar,
)
from bithumb_bot.artifact.timestamps import utc_now, utc_timestamp

__all__ = [
    "atomic_write",
    "canonical_bytes",
    "guard_against_overwrite",
    "sha256_hex",
    "sidecar_line",
    "utc_now",
    "utc_timestamp",
    "write_with_sidecar",
]
