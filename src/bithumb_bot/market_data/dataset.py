"""Deterministic candle-dataset persistence with a SHA-256 sidecar.

The dataset flows through the Phase-1 artifact primitives — canonical
JSON (D-76: sorted keys, compact separators, trailing ``\\n``, UTF-8),
a same-directory ``.sha256`` sidecar, and the overwrite/corruption
guard. Load re-hashes the on-disk bytes and refuses on any mismatch
via :class:`~bithumb_bot.errors.SidecarHashMismatchError` — the exact
mechanism the M1 spec snapshot already uses.

Bit-for-bit round-trip is the point: ``load_dataset(write_dataset(...))``
must return a model equal to the original AND the reserialized bytes
must equal the file on disk. The simulator later depends on this to
replay the same inputs identically across runs.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from bithumb_bot.artifact.canonical import (
    canonical_bytes,
    guard_against_overwrite,
    sha256_hex,
    write_with_sidecar,
)
from bithumb_bot.errors import (
    SidecarHashMismatchError,
    SnapshotValidationError,
)
from bithumb_bot.market_data.candles import Candle


class DatasetProvenance(BaseModel):
    """Everything a reader needs to explain how the dataset was fetched."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    source_endpoint: str
    base_url: str
    pages_fetched: int
    page_cursors_kst: list[str]
    effective_end_utc: str  # ISO-8601 UTC


class CandleDataset(BaseModel):
    """The persisted, replay-safe candle dataset.

    All timestamps are ISO-8601 UTC strings so canonical JSON preserves
    them byte-exactly across producers/platforms.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    venue: Literal["bithumb"]
    market: str
    unit_minutes: int
    requested_start_utc: str
    requested_end_utc: str
    fetched_at_utc: str
    candles: list[Candle]
    missing_intervals_utc: list[str]
    provenance: DatasetProvenance

    @field_validator("candles")
    @classmethod
    def _ascending_and_market_consistent(cls, v: list[Candle]) -> list[Candle]:
        prev: datetime | None = None
        for candle in v:
            if prev is not None and candle.open_time_utc <= prev:
                raise ValueError(
                    f"candles must be strictly ascending by open_time_utc; "
                    f"got {candle.open_time_utc.isoformat()} <= "
                    f"{prev.isoformat()}"
                )
            prev = candle.open_time_utc
        return v


# ---------------------------------------------------------------------------
# Serialize / write / load
# ---------------------------------------------------------------------------


def serialize_dataset(dataset: CandleDataset) -> bytes:
    """Canonical-JSON bytes of ``dataset`` (Money/Qty → strings via json mode)."""
    return canonical_bytes(dataset.model_dump(mode="json"))


def write_dataset_with_sidecar(
    dataset: CandleDataset, target: Path
) -> tuple[Path, Path]:
    """Write the dataset and its ``.sha256`` sidecar atomically.

    Refuses to silently replace a previously consumed dataset
    (:func:`~bithumb_bot.artifact.canonical.guard_against_overwrite`)
    and quarantines any pre-existing corrupt file.
    """
    sidecar = target.with_name(target.name + ".sha256")
    guard_against_overwrite(target, sidecar)
    return write_with_sidecar(target, serialize_dataset(dataset))


def load_dataset(path: Path) -> CandleDataset:
    """Load a persisted dataset, verifying the sidecar hash first.

    Raises:
        SidecarHashMismatchError: on-disk bytes disagree with the sidecar
            (tampered, truncated, or corrupted).
        SnapshotValidationError:  structural / schema validation failed.
    """
    if not path.is_file():
        raise SnapshotValidationError(f"dataset path does not exist: {path!r}")
    data = path.read_bytes()
    on_disk_hex = sha256_hex(data)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise SidecarHashMismatchError(path)
    recorded = sidecar.read_text(encoding="utf-8").strip().split()
    if len(recorded) < 1 or len(recorded[0]) != 64 or recorded[0] != on_disk_hex:
        raise SidecarHashMismatchError(path)
    try:
        parsed: Any = json.loads(data.decode("utf-8"))
        return CandleDataset.model_validate(parsed)
    except Exception as exc:  # pydantic ValidationError, json errors, etc.
        raise SnapshotValidationError(
            f"dataset at {path!r} failed structural validation: {exc}"
        ) from exc


__all__ = [
    "CandleDataset",
    "DatasetProvenance",
    "load_dataset",
    "serialize_dataset",
    "write_dataset_with_sidecar",
]
