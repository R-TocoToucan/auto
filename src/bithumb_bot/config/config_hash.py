"""`config_hash` manifest builder (D-62 / D-76 / Finding 3 / Finding 9).

Aggregates per-gate SHA-256 hashes into a canonical-JSON manifest, then
hashes THAT manifest's canonical bytes. This produces the single
behavior-affecting configuration digest that Phase 5 FRZ-02 will freeze
into the final artifact.

Phase 1 exercises this with a Gate-1-only manifest (`gate2_sha256=None`,
`gate3_sha256=None`). The function's OUTPUT is only meaningful for the
"final config_hash" purpose once all three gate hashes are present, but
the CANONICAL-BYTES-then-SHA-256 mechanism is frozen here and MUST NOT be
re-invented in Phase 3 / Phase 5.

Canonical bytes convention (Finding 9 — reused by every artifact we hash):
- `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
- UTF-8 encoded
- exactly one trailing newline (`b"\\n"`)

Hash the exact output of `_canonical_bytes` — never re-serialize later.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_bytes(obj: Any) -> bytes:
    """Serialize `obj` to the project's canonical JSON bytes (D-76).

    - `sort_keys=True` — key order is stable regardless of dict insertion order.
    - `separators=(",", ":")` — no insignificant whitespace.
    - `ensure_ascii=False` — keeps any non-ASCII bytes as literal UTF-8,
      giving a deterministic byte-for-byte round-trip if Korean-language
      metadata is ever added.
    - Exactly one trailing `b"\\n"` so `sha256sum -c` and every downstream
      cat/diff tool agrees on line boundaries.
    """
    return (
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
        + b"\n"
    )


def build_config_hash_manifest(
    gate1_sha256: str,
    gate2_sha256: str | None = None,
    gate3_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Assemble the config-hash manifest and its canonical SHA-256.

    Args:
        gate1_sha256: Hex SHA-256 of `config/decisions/gate1.toml`
                      (produced by `load_gate1`).
        gate2_sha256: Hex SHA-256 of `config/decisions/gate2.toml`;
                      `None` in Phase 1 (Gate 2 file does not exist yet).
        gate3_sha256: Hex SHA-256 of `config/decisions/gate3.toml`;
                      `None` in Phase 1 & Phase 3 (Gate 3 frozen at Phase 5).

    Returns:
        `(manifest_dict, hex_config_hash)`. The manifest is a plain
        JSON-safe `dict`; the hex hash is 64 lowercase hex chars.
    """
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "gate1_sha256": gate1_sha256,
        "gate2_sha256": gate2_sha256,
        "gate3_sha256": gate3_sha256,
    }
    hex_hash = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    return manifest, hex_hash


__all__ = ["build_config_hash_manifest"]
