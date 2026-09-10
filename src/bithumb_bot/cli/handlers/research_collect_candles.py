"""`bt research collect-candles` handler — public REST candle collection.

Public, credential-free candle fetch through the existing paginator +
validator pipeline. Writes a canonical :class:`CandleDataset` + SHA-256
sidecar. Refuses to silently overwrite a previously-consumed dataset
(D-76 via ``guard_against_overwrite``).

Only diagnostic output on success: dataset path, candle count,
missing-interval count, and the first 12 hex chars of the SHA-256.
Nothing else is printed — no response bodies, no headers, no cursors.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

from bithumb_bot.config.validator import validate

log = structlog.get_logger()


def _parse_utc(name: str, raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def handler(args: argparse.Namespace) -> int:
    """Entry point for ``bt research collect-candles``."""
    _result = validate(("research", "collect-candles"))
    if not _result.ok:
        print(
            f"bt research collect-candles: refusal: {_result.reason} "
            f"(missing: {', '.join(_result.missing) or 'unspecified'})",
            file=sys.stderr,
        )
        return 1

    try:
        market = args.market
        unit_minutes = int(args.unit_minutes)
        start_utc = _parse_utc("--start-utc", args.start_utc)
        end_utc = _parse_utc("--end-utc", args.end_utc)
        out_path = Path(args.out)
    except (AttributeError, ValueError) as exc:
        print(
            f"bt research collect-candles: invalid arguments ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    # Lazy imports so `bt --help` stays free of httpx/pyarrow.
    from bithumb_bot.artifact.canonical import sha256_hex
    from bithumb_bot.artifact.timestamps import utc_now
    from bithumb_bot.bithumb_spec import rate_limits
    from bithumb_bot.market_data.candles import fetch_candles
    from bithumb_bot.market_data.dataset import (
        CandleDataset,
        DatasetProvenance,
        write_dataset_with_sidecar,
    )

    try:
        result = asyncio.run(
            fetch_candles(
                market,
                unit_minutes=unit_minutes,
                start_utc=start_utc,
                end_utc=end_utc,
                bucket=rate_limits.public_rest,
                transport=getattr(args, "_transport", None),
            )
        )
    except Exception as exc:
        log.error(
            "research.collect-candles.fetch_failed",
            error_class=type(exc).__name__,
        )
        print(
            f"bt research collect-candles: fetch refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    dataset = CandleDataset(
        schema_version=1,
        venue="bithumb",
        market=market,
        unit_minutes=unit_minutes,
        requested_start_utc=start_utc.isoformat(),
        requested_end_utc=end_utc.isoformat(),
        fetched_at_utc=utc_now().isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        candles=list(result.candles),
        missing_intervals_utc=[
            t.isoformat() for t in result.missing_intervals_utc
        ],
        provenance=DatasetProvenance(
            source_endpoint=f"/v1/candles/minutes/{unit_minutes}",
            base_url="https://api.bithumb.com",
            pages_fetched=result.pages_fetched,
            page_cursors_kst=list(result.page_cursors_kst),
            effective_end_utc=result.effective_end_utc.isoformat(),
        ),
    )

    try:
        target, _sidecar = write_dataset_with_sidecar(dataset, out_path)
    except Exception as exc:
        log.error(
            "research.collect-candles.write_failed",
            error_class=type(exc).__name__,
        )
        print(
            f"bt research collect-candles: write refused ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    sha_prefix = sha256_hex(target.read_bytes())[:12]
    print(f"dataset:                {target}")
    print(f"candle_count:           {len(dataset.candles)}")
    print(f"missing_interval_count: {len(dataset.missing_intervals_utc)}")
    print(f"dataset_sha256[:12]:    {sha_prefix}")
    return 0


__all__ = ["handler"]
