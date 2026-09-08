"""Focused tests for the Phase-2 CandleDataset persistence layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import sha256_hex
from bithumb_bot.errors import (
    SidecarHashMismatchError,
    SnapshotAlreadyConsumedError,
    SnapshotValidationError,
)
from bithumb_bot.market_data.candles import Candle
from bithumb_bot.market_data.dataset import (
    CandleDataset,
    DatasetProvenance,
    load_dataset,
    serialize_dataset,
    write_dataset_with_sidecar,
)


UNIT = 240


def _sample_candles(n: int = 3) -> list[Candle]:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return [
        Candle(
            market="KRW-BTC",
            unit_minutes=UNIT,
            open_time_utc=start + i * timedelta(hours=4),
            open="100",
            high="110",
            low="90",
            close="105",
            volume="1.5",
            quote_volume="150",
        )
        for i in range(n)
    ]


def _sample_dataset(candles: list[Candle] | None = None) -> CandleDataset:
    candles = candles if candles is not None else _sample_candles()
    end = candles[-1].open_time_utc + timedelta(hours=4)
    return CandleDataset(
        schema_version=1,
        venue="bithumb",
        market="KRW-BTC",
        unit_minutes=UNIT,
        requested_start_utc=candles[0].open_time_utc.isoformat(),
        requested_end_utc=end.isoformat(),
        fetched_at_utc="2026-09-08T12:34:56+00:00",
        candles=candles,
        missing_intervals_utc=[],
        provenance=DatasetProvenance(
            source_endpoint="/v1/candles/minutes/240",
            base_url="https://api.bithumb.com",
            pages_fetched=1,
            page_cursors_kst=["2026-01-01T21:00:00+09:00"],
            effective_end_utc=end.isoformat(),
        ),
    )


class TestSerializationDeterminism:
    def test_serialize_bytes_identical_across_calls(self) -> None:
        d = _sample_dataset()
        assert serialize_dataset(d) == serialize_dataset(d)

    def test_serialize_ends_with_newline(self) -> None:
        d = _sample_dataset()
        assert serialize_dataset(d).endswith(b"\n")

    def test_ascending_order_enforced_by_model(self) -> None:
        candles = _sample_candles()
        with pytest.raises(Exception):  # pydantic ValidationError
            CandleDataset(
                schema_version=1,
                venue="bithumb",
                market="KRW-BTC",
                unit_minutes=UNIT,
                requested_start_utc="2026-01-01T00:00:00+00:00",
                requested_end_utc="2026-01-02T00:00:00+00:00",
                fetched_at_utc="2026-09-08T12:34:56+00:00",
                candles=list(reversed(candles)),  # descending — invalid
                missing_intervals_utc=[],
                provenance=DatasetProvenance(
                    source_endpoint="/v1/candles/minutes/240",
                    base_url="https://api.bithumb.com",
                    pages_fetched=1,
                    page_cursors_kst=[],
                    effective_end_utc="2026-01-02T00:00:00+00:00",
                ),
            )


class TestWriteAndLoadRoundTrip:
    def test_bytes_match_serialization(self, tmp_path: Path) -> None:
        d = _sample_dataset()
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        assert target.read_bytes() == serialize_dataset(d)

    def test_sidecar_records_correct_hash(self, tmp_path: Path) -> None:
        d = _sample_dataset()
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        sidecar = target.with_name(target.name + ".sha256")
        recorded_hex = sidecar.read_text(encoding="utf-8").strip().split()[0]
        assert recorded_hex == sha256_hex(target.read_bytes())

    def test_load_returns_equal_model(self, tmp_path: Path) -> None:
        d = _sample_dataset()
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        loaded = load_dataset(target)
        assert loaded == d
        # And the reserialized bytes match the file on disk exactly.
        assert serialize_dataset(loaded) == target.read_bytes()

    def test_load_preserves_decimal_exactness(self, tmp_path: Path) -> None:
        precise = "12345.678901234567890123"
        candle = Candle(
            market="KRW-BTC",
            unit_minutes=UNIT,
            open_time_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            open=precise,
            high=precise,
            low=precise,
            close=precise,
            volume=precise,
            quote_volume=precise,
        )
        d = _sample_dataset(candles=[candle])
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        loaded = load_dataset(target)
        assert loaded.candles[0].open.value == Decimal(precise)
        assert loaded.candles[0].volume.value == Decimal(precise)


class TestIntegrityAndGuards:
    def test_tampered_bytes_raise_sidecar_mismatch(self, tmp_path: Path) -> None:
        d = _sample_dataset()
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        # Flip one byte in the middle without updating the sidecar.
        tampered = bytearray(target.read_bytes())
        tampered[10] = tampered[10] ^ 0x01
        target.write_bytes(bytes(tampered))
        with pytest.raises(SidecarHashMismatchError):
            load_dataset(target)

    def test_missing_sidecar_raises_mismatch(self, tmp_path: Path) -> None:
        d = _sample_dataset()
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        target.with_name(target.name + ".sha256").unlink()
        with pytest.raises(SidecarHashMismatchError):
            load_dataset(target)

    def test_missing_file_raises_validation_error(self, tmp_path: Path) -> None:
        with pytest.raises(SnapshotValidationError):
            load_dataset(tmp_path / "nope.json")

    def test_second_identical_write_refused(self, tmp_path: Path) -> None:
        d = _sample_dataset()
        target = tmp_path / "dataset.json"
        write_dataset_with_sidecar(d, target)
        with pytest.raises(SnapshotAlreadyConsumedError):
            write_dataset_with_sidecar(d, target)
