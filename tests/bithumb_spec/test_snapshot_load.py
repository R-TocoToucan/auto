"""Tests for :func:`bithumb_bot.bithumb_spec.snapshot.load_snapshot` + guard."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import sidecar_line
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    build_snapshot,
    load_snapshot,
    serialize_snapshot,
    write_snapshot_with_sidecar,
)
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.errors import (
    SidecarHashMismatchError,
    SnapshotAlreadyConsumedError,
    SnapshotValidationError,
)

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
    / "example_20260908T012345Z.json"
)


@pytest.fixture()
def _validate_ok(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure `validate(('m1', 'verify-snapshot'))` succeeds in the isolated tmp_path."""
    # tmp_gate1_toml writes to tmp_path/config/decisions/gate1.toml.
    # Point the validator at that same tmp_path.
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))


def _build_snapshot() -> tuple[Path, ...]:
    parsed = OrdersChanceResponse.model_validate(
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    )
    snapshot = build_snapshot(parsed, market="KRW-BTC", fixture_paths=[FIXTURE_PATH])
    return snapshot, FIXTURE_PATH  # type: ignore[return-value]


class TestLoadSnapshot:
    def test_round_trip(self, tmp_path: Path, _validate_ok: None) -> None:
        snapshot, _ = _build_snapshot()
        target = tmp_path / "snap.json"
        write_snapshot_with_sidecar(snapshot, target)
        loaded = load_snapshot(target)
        assert loaded.market == "KRW-BTC"
        assert loaded.fee_rates.bid == Decimal("0.0025")

    def test_sidecar_mismatch_raises(self, tmp_path: Path, _validate_ok: None) -> None:
        snapshot, _ = _build_snapshot()
        target = tmp_path / "snap.json"
        write_snapshot_with_sidecar(snapshot, target)
        # Mutate a byte in the snapshot file (sidecar unchanged).
        original = target.read_bytes()
        target.write_bytes(original[:-1] + b"?")
        with pytest.raises(SidecarHashMismatchError):
            load_snapshot(target)

    def test_missing_sidecar_raises(self, tmp_path: Path, _validate_ok: None) -> None:
        snapshot, _ = _build_snapshot()
        target = tmp_path / "snap.json"
        write_snapshot_with_sidecar(snapshot, target)
        # Delete the sidecar.
        target.with_name(target.name + ".sha256").unlink()
        with pytest.raises(SidecarHashMismatchError):
            load_snapshot(target)

    def test_missing_target_raises_validation(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        target = tmp_path / "nope.json"
        with pytest.raises(SnapshotValidationError):
            load_snapshot(target)

    def test_missing_required_verification_status_key_rejected(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        """D-80: a snapshot missing a required verification_status key is refused."""
        snapshot, _ = _build_snapshot()
        # Serialize the snapshot, then mutate the JSON to drop a key,
        # then rewrite atomically WITH a matching sidecar (bypassing the
        # pydantic constructor's own check).
        raw = json.loads(serialize_snapshot(snapshot))
        del raw["verification_status"]["rounding_rejection_behavior"]
        bad_bytes = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        target = tmp_path / "snap.json"
        target.write_bytes(bad_bytes)
        # Write sidecar that matches the corrupted bytes so we exercise
        # the model-validate rejection, not the hash rejection.
        import hashlib

        hex_digest = hashlib.sha256(bad_bytes).hexdigest()
        target.with_name(target.name + ".sha256").write_text(
            sidecar_line(hex_digest, target.name), encoding="utf-8"
        )
        with pytest.raises(SnapshotValidationError):
            load_snapshot(target)


class TestWriteSnapshotGuard:
    def test_second_write_refused(self, tmp_path: Path) -> None:
        snapshot, _ = _build_snapshot()
        target = tmp_path / "snap.json"
        write_snapshot_with_sidecar(snapshot, target)
        with pytest.raises(SnapshotAlreadyConsumedError):
            write_snapshot_with_sidecar(snapshot, target)
