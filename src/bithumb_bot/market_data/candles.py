"""Bithumb 240-minute candle fetch, normalization, and validation.

Endpoint (per apidocs.bithumb.com — verify at build time):
    GET /v1/candles/minutes/{unit}?market=<M>&count=<N>&to=<KST>

Pagination discipline:

* ``count`` is capped at 200 per request (Finding: hard server cap).
* ``to`` is documented in **KST** and the candle at exactly ``to`` is
  **excluded** from the response. We rely on that exclusive semantic
  when advancing the cursor: the next ``to`` is the oldest candle's
  KST open-time from the previous page.
* Requested UTC boundaries are converted to a fixed ``UTC+09:00``
  offset via :data:`_KST` and rendered as ISO-8601 with an explicit
  ``+09:00`` suffix — unambiguously KST regardless of which of
  Bithumb's two accepted forms the docs prefer.

Interval discipline:

* Public function contract: half-open ``[start_utc, end_utc)``.
* Both boundaries MUST land on a ``unit_minutes`` multiple of the UTC
  epoch — otherwise the fetch refuses (fail-closed against caller
  mistakes that would silently truncate or over-fetch).
* The effective end is ``min(end_utc, floor(now_utc, unit_minutes))``
  — the current in-progress candle is always excluded.

Rate limit / real network:

* The public REST rate limit is Open Verification Item #3 and its
  configured values are TEST-ONLY sentinels. Real network use is
  fail-closed here: if ``transport is None`` the fetch raises
  :class:`~bithumb_bot.errors.PublicRestNotVerifiedError` BEFORE
  opening any client. Tests always pass an ``httpx.MockTransport``.

Exactness:

* Response bodies are parsed with ``json.loads(text,
  parse_float=Decimal)`` so JSON-number price/volume literals reach
  :class:`~decimal.Decimal` without a ``float`` in between (D-49).
* Prices are wrapped in :class:`~bithumb_bot.core.money.Money`; the
  base-asset trade volume is wrapped in
  :class:`~bithumb_bot.core.money.Qty`; the KRW quote volume is
  :class:`~bithumb_bot.core.money.Money`. No arithmetic mixes units.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Callable

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

from bithumb_bot.artifact.timestamps import utc_now
from bithumb_bot.bithumb_spec.http_client import create_client, request_with_retry
from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import CandleValidationError, PublicRestNotVerifiedError
from bithumb_bot.rate_limit.token_bucket import TokenBucket

_DEFAULT_BASE_URL = "https://api.bithumb.com"
_MAX_COUNT_PER_PAGE = 200
_KST: timezone = timezone(timedelta(hours=9))

# D-40 retry semantics reused for the public REST path.
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 15.0
_MAX_ATTEMPTS = 3
_BACKOFF_INITIAL_MS = 500
_BACKOFF_CAP_MS = 5000


# ---------------------------------------------------------------------------
# Money / Qty pydantic field adapters
# ---------------------------------------------------------------------------


def _money_from_input(value: object) -> Money:
    if isinstance(value, Money):
        return value
    if isinstance(value, Decimal):
        return Money(value)
    if isinstance(value, str):
        return Money.from_str(value)
    raise ValueError(
        f"Money field must be str/Decimal/Money, got {type(value).__name__!r}"
    )


def _qty_from_input(value: object) -> Qty:
    if isinstance(value, Qty):
        return value
    if isinstance(value, Decimal):
        return Qty(value)
    if isinstance(value, str):
        return Qty.from_str(value)
    raise ValueError(
        f"Qty field must be str/Decimal/Qty, got {type(value).__name__!r}"
    )


def _money_to_str(m: Money) -> str:
    return str(m.value)


def _qty_to_str(q: Qty) -> str:
    return str(q.value)


MoneyField = Annotated[
    Money,
    BeforeValidator(_money_from_input),
    PlainSerializer(_money_to_str, return_type=str, when_used="json"),
]
QtyField = Annotated[
    Qty,
    BeforeValidator(_qty_from_input),
    PlainSerializer(_qty_to_str, return_type=str, when_used="json"),
]


def _datetime_from_input(value: object) -> datetime:
    """Accept a tz-aware datetime, or an ISO-8601 string (canonical-JSON
    load path). Naive strings without an offset are treated as UTC.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    raise ValueError(
        f"open_time_utc must be datetime or ISO-8601 str, got "
        f"{type(value).__name__!r}"
    )


def _datetime_to_iso(dt: datetime) -> str:
    return dt.isoformat()


DatetimeField = Annotated[
    datetime,
    BeforeValidator(_datetime_from_input),
    PlainSerializer(_datetime_to_iso, return_type=str, when_used="json"),
]


# ---------------------------------------------------------------------------
# Candle model
# ---------------------------------------------------------------------------


class Candle(BaseModel):
    """One completed 240-minute (or other unit_minutes) candle.

    ``open_time_utc`` is the candle's UTC open instant, always
    tz-aware. ``open_time_utc + timedelta(minutes=unit_minutes)`` is
    the exclusive close instant.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
    )

    market: str
    unit_minutes: int
    open_time_utc: DatetimeField
    open: MoneyField
    high: MoneyField
    low: MoneyField
    close: MoneyField
    volume: QtyField
    quote_volume: MoneyField


# ---------------------------------------------------------------------------
# Public results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a single :func:`fetch_candles` invocation.

    ``candles`` is strictly ascending by ``open_time_utc``.
    ``missing_intervals_utc`` is a strictly ascending list of expected
    ``open_time_utc`` instants that were NOT returned by the API — the
    caller decides whether to reject, retry, or record; the pipeline
    NEVER fabricates a candle to fill a gap.
    """

    candles: tuple[Candle, ...]
    missing_intervals_utc: tuple[datetime, ...]
    pages_fetched: int
    effective_end_utc: datetime
    page_cursors_kst: tuple[str, ...]


# ---------------------------------------------------------------------------
# Boundary helpers
# ---------------------------------------------------------------------------


def _require_utc(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(
            f"{name} must be a tz-aware UTC datetime "
            f"(tzinfo=UTC / offset 0), got {value!r}"
        )
    return value


def _floor_to_unit(instant: datetime, unit_minutes: int) -> datetime:
    """Floor ``instant`` down to the previous ``unit_minutes`` UTC boundary."""
    seconds = int(instant.replace(tzinfo=UTC).timestamp())
    unit_s = unit_minutes * 60
    floored = (seconds // unit_s) * unit_s
    return datetime.fromtimestamp(floored, tz=UTC)


def _is_on_boundary(instant: datetime, unit_minutes: int) -> bool:
    return _floor_to_unit(instant, unit_minutes) == instant


def _to_kst_cursor(utc_boundary: datetime) -> str:
    """Render ``utc_boundary`` as an ISO-8601 KST cursor for the ``to`` param.

    Bithumb documents ``to`` as KST and excludes the candle at that
    timestamp. We render as ``YYYY-MM-DDTHH:MM:SS+09:00`` — explicit
    ``+09:00`` offset removes any ambiguity between the two forms the
    docs accept.
    """
    kst = utc_boundary.astimezone(_KST)
    return kst.strftime("%Y-%m-%dT%H:%M:%S+09:00")


# ---------------------------------------------------------------------------
# Page-level normalization + validation
# ---------------------------------------------------------------------------


def _parse_open_time_utc(raw: str) -> datetime:
    """Bithumb sends ``candle_date_time_utc`` as naive ISO. Tag as UTC."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return _require_utc("candle_date_time_utc", parsed)


def _one_candle_from_row(
    row: dict[str, Any], *, market: str, unit_minutes: int
) -> Candle:
    """Build one Candle from a raw response row. Enforces field basics."""
    for required in (
        "market",
        "unit",
        "candle_date_time_utc",
        "opening_price",
        "high_price",
        "low_price",
        "trade_price",
        "candle_acc_trade_volume",
        "candle_acc_trade_price",
    ):
        if required not in row:
            raise CandleValidationError(
                f"raw candle row missing required key {required!r}: {row!r}"
            )
    if row["market"] != market:
        raise CandleValidationError(
            f"raw candle market {row['market']!r} != requested {market!r}"
        )
    if int(row["unit"]) != unit_minutes:
        raise CandleValidationError(
            f"raw candle unit {row['unit']!r} != requested {unit_minutes!r}"
        )
    open_time = _parse_open_time_utc(row["candle_date_time_utc"])
    if not _is_on_boundary(open_time, unit_minutes):
        raise CandleValidationError(
            f"candle_date_time_utc {open_time.isoformat()} is not on a "
            f"{unit_minutes}-minute boundary"
        )
    return Candle(
        market=market,
        unit_minutes=unit_minutes,
        open_time_utc=open_time,
        open=row["opening_price"],
        high=row["high_price"],
        low=row["low_price"],
        close=row["trade_price"],
        volume=row["candle_acc_trade_volume"],
        quote_volume=row["candle_acc_trade_price"],
    )


def _validate_ohlc(candle: Candle) -> None:
    if candle.low > candle.high:
        raise CandleValidationError(
            f"impossible OHLC at {candle.open_time_utc.isoformat()}: "
            f"low={candle.low.value} > high={candle.high.value}"
        )
    if candle.open < candle.low or candle.open > candle.high:
        raise CandleValidationError(
            f"impossible OHLC at {candle.open_time_utc.isoformat()}: "
            f"open={candle.open.value} outside [low={candle.low.value}, "
            f"high={candle.high.value}]"
        )
    if candle.close < candle.low or candle.close > candle.high:
        raise CandleValidationError(
            f"impossible OHLC at {candle.open_time_utc.isoformat()}: "
            f"close={candle.close.value} outside [low={candle.low.value}, "
            f"high={candle.high.value}]"
        )
    if candle.volume.value < Decimal("0"):
        raise CandleValidationError(
            f"negative volume at {candle.open_time_utc.isoformat()}: "
            f"{candle.volume.value}"
        )
    if candle.quote_volume.value < Decimal("0"):
        raise CandleValidationError(
            f"negative quote_volume at {candle.open_time_utc.isoformat()}: "
            f"{candle.quote_volume.value}"
        )


def _candle_field_equivalent(a: Candle, b: Candle) -> bool:
    """Two candles are considered duplicates iff all fields are equal.

    Used to accept a benign repeat across page boundaries. Compares
    ``Decimal`` values exactly — ``Decimal("100") == Decimal("100.0")``
    is True in Python, so trailing-zero differences from the wire do
    NOT trigger a false-conflict rejection.
    """
    return (
        a.market == b.market
        and a.unit_minutes == b.unit_minutes
        and a.open_time_utc == b.open_time_utc
        and a.open.value == b.open.value
        and a.high.value == b.high.value
        and a.low.value == b.low.value
        and a.close.value == b.close.value
        and a.volume.value == b.volume.value
        and a.quote_volume.value == b.quote_volume.value
    )


def normalize_and_validate_page(
    rows: list[dict[str, Any]],
    *,
    market: str,
    unit_minutes: int,
) -> list[Candle]:
    """Convert one raw page to :class:`Candle`s, enforcing per-page rules.

    Enforces (fail-closed):

    * Every row's ``market`` and ``unit`` match the request.
    * Every ``candle_date_time_utc`` is a UTC boundary of ``unit_minutes``.
    * Rows are strictly descending by ``open_time_utc`` — Bithumb's
      documented response order. Equal or ascending pairs raise.
    * OHLC values are self-consistent (``low ≤ open|close ≤ high``,
      non-negative volume + quote_volume).

    Cross-page dedup / conflict handling is the caller's responsibility
    (see :func:`fetch_candles`).
    """
    out: list[Candle] = []
    prev_time: datetime | None = None
    for row in rows:
        candle = _one_candle_from_row(row, market=market, unit_minutes=unit_minutes)
        if prev_time is not None and candle.open_time_utc >= prev_time:
            raise CandleValidationError(
                f"page not strictly descending: {candle.open_time_utc.isoformat()} "
                f">= previous {prev_time.isoformat()}"
            )
        prev_time = candle.open_time_utc
        _validate_ohlc(candle)
        out.append(candle)
    return out


# ---------------------------------------------------------------------------
# Fetch orchestrator
# ---------------------------------------------------------------------------


async def fetch_candles(
    market: str,
    *,
    unit_minutes: int,
    start_utc: datetime,
    end_utc: datetime,
    bucket: TokenBucket,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = _DEFAULT_BASE_URL,
    now_utc: Callable[[], datetime] = utc_now,
) -> FetchResult:
    """Fetch native Bithumb candles for ``[start_utc, end_utc)``.

    Behavior contract:

    * ``bucket`` is REQUIRED — the caller injects the rate-limiter
      instance. Tests pass a fast fake; production callers must pass a
      verified bucket. This module NEVER touches the module-level
      sentinel in :mod:`bithumb_bot.bithumb_spec.rate_limits`.
    * ``transport`` MUST be a mock in every current caller — real
      network use raises
      :class:`~bithumb_bot.errors.PublicRestNotVerifiedError` BEFORE
      opening any client.
    * ``start_utc`` and ``end_utc`` MUST be tz-aware UTC and MUST land
      on ``unit_minutes``-minute boundaries.
    * Requested interval is half-open ``[start_utc, end_utc)``. The
      incomplete current candle is always excluded via
      ``min(end_utc, floor(now_utc, unit_minutes))``.
    * Pagination cursor is the previous page's oldest candle's KST
      timestamp, sent as ``to=<KST>``; the candle at that KST is
      excluded from the next page per Bithumb docs.
    * Cross-page duplicates are accepted iff every field is equal;
      value conflicts raise :class:`~bithumb_bot.errors.
      CandleValidationError`. Duplicates are never silently resolved.
    * Missing intervals inside the effective range are REPORTED via
      :attr:`FetchResult.missing_intervals_utc`; no candle is ever
      fabricated to fill a gap.
    """
    _require_utc("start_utc", start_utc)
    _require_utc("end_utc", end_utc)
    if start_utc >= end_utc:
        raise ValueError(f"start_utc={start_utc!r} must be < end_utc={end_utc!r}")
    if not _is_on_boundary(start_utc, unit_minutes):
        raise ValueError(
            f"start_utc={start_utc.isoformat()} not on a "
            f"{unit_minutes}-minute UTC boundary"
        )
    if not _is_on_boundary(end_utc, unit_minutes):
        raise ValueError(
            f"end_utc={end_utc.isoformat()} not on a "
            f"{unit_minutes}-minute UTC boundary"
        )
    if transport is None:
        raise PublicRestNotVerifiedError(
            "fetch_candles refuses real network calls until the Bithumb "
            "public REST rate limit is a frozen M1 verification item. "
            "Tests inject httpx.MockTransport; production callers must "
            "provide a verified transport AND rate-limit bucket."
        )

    current_boundary = _floor_to_unit(now_utc(), unit_minutes)
    effective_end = min(end_utc, current_boundary)
    if effective_end <= start_utc:
        return FetchResult(
            candles=(),
            missing_intervals_utc=(),
            pages_fetched=0,
            effective_end_utc=effective_end,
            page_cursors_kst=(),
        )

    endpoint = f"/v1/candles/minutes/{unit_minutes}"
    unit_delta = timedelta(minutes=unit_minutes)

    # Keyed by open_time_utc so cross-page duplicates land in the same slot.
    seen: dict[datetime, Candle] = {}
    cursors: list[str] = []

    cursor_utc: datetime = effective_end  # exclusive per Bithumb docs
    async with create_client(
        base_url=base_url,
        connect_timeout_s=_CONNECT_TIMEOUT_S,
        read_timeout_s=_READ_TIMEOUT_S,
        transport=transport,
    ) as client:
        while cursor_utc > start_utc:
            to_kst = _to_kst_cursor(cursor_utc)
            cursors.append(to_kst)
            response = await request_with_retry(
                client,
                "GET",
                endpoint,
                params={
                    "market": market,
                    "count": _MAX_COUNT_PER_PAGE,
                    "to": to_kst,
                },
                max_attempts=_MAX_ATTEMPTS,
                backoff_initial_ms=_BACKOFF_INITIAL_MS,
                backoff_cap_ms=_BACKOFF_CAP_MS,
                bucket=bucket,
            )
            response.raise_for_status()
            # Parse with parse_float=Decimal so JSON-number prices/volumes
            # reach Decimal without a float in between (D-49).
            raw_rows: Any = json.loads(response.text, parse_float=Decimal)
            if not isinstance(raw_rows, list):
                raise CandleValidationError(
                    f"candles response must be a JSON array, got "
                    f"{type(raw_rows).__name__}"
                )
            if not raw_rows:
                break
            page = normalize_and_validate_page(
                raw_rows, market=market, unit_minutes=unit_minutes
            )
            for candle in page:
                if candle.open_time_utc < start_utc:
                    continue
                if candle.open_time_utc >= effective_end:
                    # Server returned a row past the exclusive cursor —
                    # skip; do NOT include past-effective-end candles.
                    continue
                existing = seen.get(candle.open_time_utc)
                if existing is None:
                    seen[candle.open_time_utc] = candle
                elif not _candle_field_equivalent(existing, candle):
                    raise CandleValidationError(
                        f"duplicate candle at {candle.open_time_utc.isoformat()} "
                        "with CONFLICTING OHLCV values across pages — refusing "
                        "to silently pick one"
                    )
                # Byte/field-equivalent duplicate → keep the first; no-op.
            oldest = page[-1].open_time_utc  # page is strictly descending
            # Rely on the documented exclusive `to` semantics: sending the
            # oldest row's KST timestamp as the next cursor excludes it.
            next_cursor = oldest
            if next_cursor >= cursor_utc:
                # Defensive: server misbehaviour or we already covered the
                # boundary. Break to avoid an infinite loop.
                break
            cursor_utc = next_cursor
            if oldest <= start_utc:
                break

    candles = tuple(sorted(seen.values(), key=lambda c: c.open_time_utc))

    # Missing-interval report. Expected opens = [start, effective_end) step unit.
    expected: list[datetime] = []
    t = start_utc
    while t < effective_end:
        expected.append(t)
        t = t + unit_delta
    observed = {c.open_time_utc for c in candles}
    missing = tuple(t for t in expected if t not in observed)

    return FetchResult(
        candles=candles,
        missing_intervals_utc=missing,
        pages_fetched=len(cursors),
        effective_end_utc=effective_end,
        page_cursors_kst=tuple(cursors),
    )


__all__ = [
    "Candle",
    "FetchResult",
    "MoneyField",
    "QtyField",
    "fetch_candles",
    "normalize_and_validate_page",
]
