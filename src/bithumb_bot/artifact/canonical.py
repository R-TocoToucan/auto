"""Canonical JSON, atomic write, SHA-256 sidecar, and overwrite guard (D-76).

Every immutable artifact this project writes — the M1 spec snapshot,
Phase 2's Parquet manifests, verification bundle manifests — flows
through these primitives so their disk representation is bit-for-bit
deterministic and tamper-detectable.

Rules encoded here (D-76 verbatim):

* Canonical JSON: UTF-8, ``sort_keys=True``, compact separators
  ``(",", ":")``, ``ensure_ascii=False`` (so non-ASCII strings encode as
  their literal UTF-8 bytes rather than ``\\uXXXX`` escapes), trailing
  ``\\n``. Two calls with equivalently-shaped input MUST return
  byte-identical output.
* SHA-256 hash of the exact canonical bytes.
* Sidecar format: ``"<64-char-hex>  <filename>\\n"`` — two spaces (POSIX
  ``sha256sum`` convention, so an operator can spot-check with
  ``sha256sum --check <sidecar>``).
* Atomic write: temp file in the SAME directory as the target
  (Windows same-filesystem correctness — cross-drive ``os.replace`` may
  raise ``OSError``), then ``os.replace(tmp, target)``.
* Guard against overwrite: refuse a write when the on-disk file's hash
  matches its sidecar (previously consumed snapshot — D-76). If the
  on-disk file's hash does NOT match its sidecar, rename the file to
  ``_corrupt_<ts>_<name>`` and raise ``CriticalCorruptionAlert``.

Neither :mod:`bithumb_bot.artifact.canonical` nor its callers may write
a monetary value through ``json.dumps`` before wrapping it as a string:
that would round-trip through ``float``. The Snapshot serializer (in
:mod:`bithumb_bot.bithumb_spec.snapshot`) uses ``model_dump(mode="json")``
so every ``Decimal`` field is stringified before it reaches
:func:`canonical_bytes`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bithumb_bot.artifact.timestamps import utc_timestamp
from bithumb_bot.errors import (
    CriticalCorruptionAlert,
    SnapshotAlreadyConsumedError,
)

# ---------------------------------------------------------------------------
# Canonical JSON + hashing
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to canonical JSON bytes per D-76.

    The output is:

    * UTF-8 encoded.
    * Keys sorted at every dict level (``sort_keys=True``).
    * Compact separators (``",", ":"``) — no insignificant whitespace.
    * ``ensure_ascii=False`` — non-ASCII strings are written as their
      literal UTF-8 bytes, not escape sequences.
    * Terminated by a single trailing ``\\n`` (POSIX text-file convention;
      makes ``sha256sum`` diffs across platforms match).

    Two calls with dicts that are equivalent under key-order permutation
    MUST return identical bytes; this is what makes the sidecar SHA-256
    stable across producers.

    Args:
        obj: Any JSON-serializable value. Nested ``Decimal`` values MUST
            be stringified by the caller (via pydantic
            ``model_dump(mode='json')`` or explicit ``str()``) BEFORE
            reaching this function — ``json.dumps`` would raise on a
            raw ``Decimal``, but even if it accepted one, round-tripping
            through the default encoder would silently pass through
            ``float`` for numeric leaves. D-49.

    Returns:
        The canonical serialized bytes ending in ``\\n``.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``.

    Args:
        data: The raw bytes to hash (typically the return value of
            :func:`canonical_bytes` or a file's ``read_bytes()``).

    Returns:
        A 64-character lowercase hex string.
    """
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Atomic write + sidecar
# ---------------------------------------------------------------------------


def atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically via a same-directory temp file.

    Implementation:

    1. Create ``tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")``
       in the SAME directory as ``target`` (Finding 9 / Windows same-
       filesystem correctness — a temp file on a different drive would
       make ``os.replace`` raise ``OSError`` on Windows).
    2. Write ``data`` to ``tmp`` with ``tmp.write_bytes(data)``.
    3. ``os.replace(tmp, target)`` — atomic rename on both POSIX and NT.

    The parent directory MUST exist; callers create it explicitly rather
    than having this primitive silently ``mkdir(parents=True)`` (that
    would hide bugs where the target path is malformed).

    Args:
        target: Absolute path of the final file.
        data:   Raw bytes to write.

    Raises:
        FileNotFoundError: ``target.parent`` does not exist.
        OSError:           the write or the rename failed.
    """
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def sidecar_line(hex_digest: str, filename: str) -> str:
    """Return the sha256sum-compatible sidecar line for ``(hex_digest, filename)``.

    Format: ``"{hex_digest}  {filename}\\n"`` — 64 hex chars, two
    spaces, filename, newline. Matches POSIX ``sha256sum`` output so an
    operator can spot-check with ``sha256sum --check <sidecar>``.
    """
    return f"{hex_digest}  {filename}\n"


def write_with_sidecar(target: Path, data: bytes) -> tuple[Path, Path]:
    """Write ``data`` to ``target`` and its ``.sha256`` sidecar atomically.

    Both files land via :func:`atomic_write` in the SAME directory as
    ``target`` — the sidecar's filename column contains ``target.name``
    only (not the absolute path), so ``sha256sum --check`` works when
    run from ``target.parent``.

    Args:
        target: Absolute path of the final file.
        data:   Raw bytes to write.

    Returns:
        ``(target, sidecar)`` — both are absolute ``Path`` objects.
    """
    sidecar = target.with_name(f"{target.name}.sha256")
    atomic_write(target, data)
    atomic_write(sidecar, sidecar_line(sha256_hex(data), target.name).encode("utf-8"))
    return target, sidecar


# ---------------------------------------------------------------------------
# Guard against silent overwrite (D-76)
# ---------------------------------------------------------------------------


def _read_sidecar_hex(sidecar: Path) -> str | None:
    """Extract the hex-digest column from a sidecar file.

    Returns ``None`` if the sidecar does not exist, is empty, or is
    structurally malformed. A sidecar we cannot parse is treated as
    "no attestation available" — caller then falls into the corruption
    branch, which is the safe behavior (never silently trust an
    unattested file).
    """
    if not sidecar.is_file():
        return None
    text = sidecar.read_text(encoding="utf-8").strip()
    if not text:
        return None
    hex_part = text.split()[0] if text else ""
    if len(hex_part) != 64:
        return None
    return hex_part


def guard_against_overwrite(target: Path, sidecar: Path) -> None:
    """Enforce D-76 "never overwrite a previously consumed snapshot".

    Branches:

    * ``target`` does not exist              — nothing to guard; return.
    * ``target`` exists, sidecar matches     — raise
      :class:`~bithumb_bot.errors.SnapshotAlreadyConsumedError`. This is
      the primary D-76 protection.
    * ``target`` exists, sidecar mismatches
      (or is missing / unreadable)          — rename the on-disk file
      to ``_corrupt_<utc_timestamp>_<name>`` in the same directory and
      raise :class:`~bithumb_bot.errors.CriticalCorruptionAlert` so the
      operator investigates before the write proceeds.

    Args:
        target:  Absolute path of the intended target.
        sidecar: Absolute path of the corresponding ``.sha256`` sidecar.

    Raises:
        SnapshotAlreadyConsumedError: on-disk file matches its sidecar
            — the previous snapshot is intact and MUST NOT be overwritten.
        CriticalCorruptionAlert:      on-disk file does NOT match its
            sidecar. The file has been renamed for forensic review.
    """
    if not target.exists():
        return
    on_disk = target.read_bytes()
    on_disk_hex = sha256_hex(on_disk)
    recorded_hex = _read_sidecar_hex(sidecar)
    if recorded_hex is not None and recorded_hex == on_disk_hex:
        raise SnapshotAlreadyConsumedError(target)
    # Mismatch OR missing/unreadable sidecar: quarantine + alert.
    corrupt = target.with_name(f"_corrupt_{utc_timestamp()}_{target.name}")
    target.rename(corrupt)
    raise CriticalCorruptionAlert(corrupt)


__all__ = [
    "atomic_write",
    "canonical_bytes",
    "guard_against_overwrite",
    "sha256_hex",
    "sidecar_line",
    "write_with_sidecar",
]
