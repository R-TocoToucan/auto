"""Focused tests for the Phase-2 KRW-BTC candle fetch pipeline.

Every HTTP request flows through `httpx.MockTransport` — the test suite
NEVER touches a real Bithumb endpoint. A test-scope TokenBucket
(capacity=1000, refill=1000/s) is injected so rate limiting doesn't
introduce wall-clock delays.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

import httpx
import pytest

from bithumb_bot.core.money import Money, Qty
from bithumb_bot.errors import (
    CandleValidationError,
    PublicRestErrorResponseError,
    PublicRestNotVerifiedError,
)
from bithumb_bot.market_data.candles import (
    Candle,
    fetch_candles,
    normalize_and_validate_page,
)
from bithumb_bot.rate_limit.token_bucket import TokenBucket


UNIT = 240


def _fast_bucket() -> TokenBucket:
    return TokenBucket(capacity=1000, refill_rate=1000.0)


def _row(
    open_time_utc: datetime,
    *,
    market: str = "KRW-BTC",
    unit_minutes: int = UNIT,
    o: str = "100",
    h: str = "110",
    lo: str = "90",
    c: str = "105",
    v: str = "1.5",
    qv: str = "150",
) -> dict[str, Any]:
    """Build a raw response row shaped like Bithumb's candles response."""
    kst = open_time_utc + timedelta(hours=9)
    return {
        "market": market,
        "candle_date_time_utc": open_time_utc.replace(tzinfo=None).isoformat(
            timespec="seconds"
        ),
        "candle_date_time_kst": kst.replace(tzinfo=None).isoformat(timespec="seconds"),
        # JSON numbers, parsed via `parse_float=Decimal` in production.
        "opening_price": Decimal(o),
        "high_price": Decimal(h),
        "low_price": Decimal(lo),
        "trade_price": Decimal(c),
        "timestamp": int(open_time_utc.timestamp() * 1000),
        "candle_acc_trade_price": Decimal(qv),
        "candle_acc_trade_volume": Decimal(v),
        "unit": unit_minutes,
    }


def _mock_paged_transport(
    pages: dict[str, list[dict[str, Any]]],
    *,
    captured_requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Return a MockTransport that serves ``pages`` keyed by the ``to`` param.

    If the request's ``to`` is missing from ``pages``, an empty list is
    returned (matches Bithumb's behavior when the cursor precedes any
    available history).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured_requests is not None:
            captured_requests.append(request)
        assert request.url.path.startswith("/v1/candles/minutes/")
        to = request.url.params.get("to", "")
        # Encode rows as text so parse_float=Decimal path is exercised.
        rows = pages.get(to, [])
        # httpx's default JSON encoder handles Decimal via str fallback? No —
        # so pre-encode with a Decimal-aware encoder.
        import json as _json

        text = _json.dumps(
            rows,
            default=lambda o: str(o) if isinstance(o, Decimal) else None,
        )
        return httpx.Response(
            200, content=text.encode("utf-8"), headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler)


def _fixed_now(instant: datetime) -> Callable[[], datetime]:
    return lambda: instant


# ---------------------------------------------------------------------------
# normalize_and_validate_page
# ---------------------------------------------------------------------------


class TestNormalizeAndValidatePage:
    def test_happy_path_descending(self) -> None:
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        rows = [_row(t2), _row(t1), _row(t0)]
        candles = normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)
        assert [c.open_time_utc for c in candles] == [t2, t1, t0]
        assert candles[0].open.value == Decimal("100")

    def test_rejects_non_descending_page(self) -> None:
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        rows = [_row(t0), _row(t1)]  # ascending — invalid
        with pytest.raises(CandleValidationError, match="not strictly descending"):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_duplicate_within_page(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t), _row(t)]  # equal — not strictly descending
        with pytest.raises(CandleValidationError, match="not strictly descending"):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_wrong_market(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, market="KRW-ETH")]
        with pytest.raises(CandleValidationError, match="market"):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_wrong_unit(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, unit_minutes=60)]
        with pytest.raises(CandleValidationError, match="unit"):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_off_boundary_timestamp(self) -> None:
        # 4h units → 02:00 UTC is NOT a boundary.
        t = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        rows = [_row(t)]
        with pytest.raises(CandleValidationError, match="boundary"):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_impossible_ohlc_low_gt_high(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, h="90", lo="100")]  # low > high
        with pytest.raises(CandleValidationError, match="low="):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_open_above_high(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, o="200", h="110")]
        with pytest.raises(CandleValidationError, match="open="):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_close_below_low(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, c="80", lo="90")]
        with pytest.raises(CandleValidationError, match="close="):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_rejects_negative_volume(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, v="-0.5")]
        with pytest.raises(CandleValidationError, match="negative volume"):
            normalize_and_validate_page(rows, market="KRW-BTC", unit_minutes=UNIT)

    def test_utc_naive_iso_is_tagged_utc(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        [candle] = normalize_and_validate_page(
            [_row(t)], market="KRW-BTC", unit_minutes=UNIT
        )
        assert candle.open_time_utc.tzinfo is not None
        assert candle.open_time_utc.utcoffset() == timedelta(0)

    def test_decimal_preserved_end_to_end(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        rows = [_row(t, o="12345.6789012345", h="12345.6789012345",
                     lo="12345.6789012345", c="12345.6789012345")]
        [candle] = normalize_and_validate_page(
            rows, market="KRW-BTC", unit_minutes=UNIT
        )
        assert candle.open.value == Decimal("12345.6789012345")


# ---------------------------------------------------------------------------
# fetch_candles — pagination and cursor mechanics
# ---------------------------------------------------------------------------


class TestFetchCandlesPagination:
    def test_fail_closed_when_transport_missing(self) -> None:
        with pytest.raises(PublicRestNotVerifiedError, match="fail-closed|verified"):
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                    end_utc=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                    bucket=_fast_bucket(),
                    transport=None,
                    now_utc=_fixed_now(datetime(2026, 1, 3, 0, 0, tzinfo=UTC)),
                )
            )

    def test_off_boundary_start_refused(self) -> None:
        with pytest.raises(ValueError, match="boundary"):
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=datetime(2026, 1, 1, 2, 0, tzinfo=UTC),  # not a 4h mark
                    end_utc=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                    bucket=_fast_bucket(),
                    transport=_mock_paged_transport({}),
                    now_utc=_fixed_now(datetime(2026, 1, 3, 0, 0, tzinfo=UTC)),
                )
            )

    def test_single_page_ascending_output(self) -> None:
        # Request 24h → 6 candles.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        opens = [start + i * timedelta(hours=4) for i in range(6)]
        # Page returned descending, `to = end` KST-formatted.
        to_kst = "2026-01-02T09:00:00"
        pages = {to_kst: [_row(t) for t in reversed(opens)]}
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(datetime(2026, 1, 3, 0, 0, tzinfo=UTC)),
            )
        )
        assert [c.open_time_utc for c in result.candles] == opens
        assert result.missing_intervals_utc == ()
        assert result.pages_fetched == 1

    def test_pagination_advances_by_oldest_kst(self) -> None:
        # 800 candles requested → 4 pages of 200. Cursor advances by the
        # oldest returned candle's KST.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        # Choose end so that (end - start) / 4h = 800 candles.
        end = start + 800 * timedelta(hours=4)
        opens = [start + i * timedelta(hours=4) for i in range(800)]
        # Bithumb serves at most 200 rows descending. Split into 4 pages.
        pages: dict[str, list[dict[str, Any]]] = {}
        cursor = end
        remaining = list(reversed(opens))  # descending
        while remaining:
            key = (cursor + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S")
            chunk = remaining[:200]
            pages[key] = [_row(t) for t in chunk]
            remaining = remaining[200:]
            if not remaining:
                break
            cursor = chunk[-1]  # oldest in chunk, exclusive next `to`
        captured: list[httpx.Request] = []
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages, captured_requests=captured),
                now_utc=_fixed_now(end + timedelta(hours=4)),
            )
        )
        assert len(result.candles) == 800
        assert result.candles[0].open_time_utc == opens[0]
        assert result.candles[-1].open_time_utc == opens[-1]
        assert result.pages_fetched == 4
        assert result.missing_intervals_utc == ()

    def test_incomplete_current_candle_excluded(self) -> None:
        # end_utc requests future; effective_end must be floored to the
        # last COMPLETED Bithumb-grid candle opening (KST 4h ticks =
        # UTC 03/07/11/15/19/23). now is 05:15 UTC → floor to 03:00
        # UTC (= 12:00 KST). effective_end = 03:00.
        start = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)  # goes past incomplete candle
        now = datetime(2026, 1, 2, 5, 15, tzinfo=UTC)
        opens = [
            datetime(2026, 1, 1, 20, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        ]
        # Effective end is 03:00 UTC = 12:00 KST (naive `to` cursor).
        to_kst = "2026-01-02T12:00:00"
        pages = {to_kst: [_row(t) for t in reversed(opens)]}
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(now),
            )
        )
        assert result.effective_end_utc == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
        assert [c.open_time_utc for c in result.candles] == opens

    def test_uses_kst_cursor_naive_no_offset(self) -> None:
        # Regression: Bithumb's `/v1/candles/minutes/{unit}` REJECTS
        # explicit-offset forms (`+09:00`, `Z`) and returns HTTP 200
        # with `{"error": {...}}`. Only the NAIVE KST form is
        # accepted. The `to` param MUST NOT contain any offset suffix.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        opens = [start]
        to_kst = "2026-01-01T13:00:00"  # 04:00 UTC = 13:00 KST, naive
        pages = {to_kst: [_row(t) for t in reversed(opens)]}
        captured: list[httpx.Request] = []
        asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages, captured_requests=captured),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        [req] = captured
        assert req.url.params["to"] == to_kst
        # No offset suffix — naive KST only.
        assert "+" not in req.url.params["to"]
        assert not req.url.params["to"].endswith("Z")

    def test_missing_intervals_reported_not_fabricated(self) -> None:
        # Request 24h (6 candles) but only 3 come back. The other 3 land
        # in missing_intervals_utc and NEVER as fabricated candles.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        returned_opens = [
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 0, tzinfo=UTC),  # 04:00 missing
            datetime(2026, 1, 1, 16, 0, tzinfo=UTC),  # 12:00, 20:00 missing
        ]
        expected_missing = [
            datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 20, 0, tzinfo=UTC),
        ]
        to_kst = "2026-01-02T09:00:00"
        pages = {to_kst: [_row(t) for t in reversed(returned_opens)]}
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(datetime(2026, 1, 3, 0, 0, tzinfo=UTC)),
            )
        )
        assert [c.open_time_utc for c in result.candles] == returned_opens
        assert list(result.missing_intervals_utc) == expected_missing

    def test_cross_page_duplicate_field_equivalent_accepted(self) -> None:
        # Simulate a benign cross-page repeat. Page 1 is short (server
        # truncated to one row) so the cursor advances but doesn't hit
        # start; page 2 (correctly excluding its `to` candle) then
        # coincidentally happens to re-emit an already-seen row —
        # this is a server-buggy scenario the pipeline must survive
        # without either dropping or misresolving.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        page1_to = "2026-01-01T17:00:00"  # 08:00 UTC
        page2_to = "2026-01-01T13:00:00"  # 04:00 UTC
        pages = {
            page1_to: [_row(t1)],  # server returned only 1 row
            page2_to: [_row(t1), _row(t0)],  # re-emits t1 identically + t0
        }
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        assert [c.open_time_utc for c in result.candles] == [t0, t1]

    def test_cross_page_duplicate_with_conflict_raises(self) -> None:
        # Same shape as the "field-equivalent accepted" test, but page 2
        # returns a CONFLICTING value for the already-seen t1 candle.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        t1 = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        page1_to = "2026-01-01T17:00:00"
        page2_to = "2026-01-01T13:00:00"
        pages = {
            page1_to: [_row(t1, c="105")],
            # `c` still inside [low=90, high=110] so per-row OHLC passes;
            # only the cross-page duplicate check fires.
            page2_to: [_row(t1, c="108"), _row(t0)],
        }
        with pytest.raises(CandleValidationError, match="CONFLICTING"):
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=start,
                    end_utc=end,
                    bucket=_fast_bucket(),
                    transport=_mock_paged_transport(pages),
                    now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
                )
            )

    def test_no_float_conversion_anywhere(self) -> None:
        # Prices with more digits than a float64 can represent survive
        # exactly to the Candle model.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        precise = "12345.678901234567890123"
        to_kst = "2026-01-01T13:00:00"
        pages = {
            to_kst: [
                _row(start, o=precise, h=precise, lo=precise, c=precise, v=precise)
            ]
        }
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        [candle] = result.candles
        assert candle.open.value == Decimal(precise)
        assert candle.volume.value == Decimal(precise)


class TestCandleModelTypes:
    def test_open_returns_money(self) -> None:
        t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        [candle] = normalize_and_validate_page(
            [_row(t)], market="KRW-BTC", unit_minutes=UNIT
        )
        # Money and Qty wrappers preserve their type-tagged identity.
        assert isinstance(candle.open, Money)
        assert isinstance(candle.volume, Qty)


# ---------------------------------------------------------------------------
# Real Bithumb JSON numeric shapes — integer OHLC, decimal volumes, mixed
# ---------------------------------------------------------------------------
#
# Bithumb's `/v1/candles/minutes/{unit}` response commonly serialises OHLC
# prices as UNQUOTED JSON INTEGERS (e.g. `"opening_price": 150000000`) and
# volumes as UNQUOTED JSON DECIMAL NUMBERS (e.g.
# `"candle_acc_trade_volume": 0.123`). These tests build response bodies as
# literal JSON text (NOT via `json.dumps(..., default=str)` which would
# quote every value and recreate the original bad fixture that hid this
# defect), and exercise the fetch path with `httpx.MockTransport` so the
# response bytes go through the real `json.loads(text, parse_float=Decimal)`
# code path.


def _int_ohlc_response_bytes(open_time_utc: datetime) -> bytes:
    """Real-shape Bithumb page: 1 row with INT OHLC + DECIMAL volume."""
    naive_utc = open_time_utc.replace(tzinfo=None).isoformat(timespec="seconds")
    kst_naive = (open_time_utc + timedelta(hours=9)).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")
    ts_ms = int(open_time_utc.timestamp() * 1000)
    text = (
        "["
        "{"
        '"market":"KRW-BTC",'
        f'"candle_date_time_utc":"{naive_utc}",'
        f'"candle_date_time_kst":"{kst_naive}",'
        '"opening_price":150000000,'
        '"high_price":150000005,'
        '"low_price":149999995,'
        '"trade_price":150000001,'
        f'"timestamp":{ts_ms},'
        '"candle_acc_trade_price":150000001.5,'
        '"candle_acc_trade_volume":0.0000001,'
        f'"unit":{UNIT}'
        "}"
        "]"
    )
    return text.encode("utf-8")


def _mixed_ohlc_response_bytes(open_time_utc: datetime) -> bytes:
    """Real-shape Bithumb page: INT prices + DECIMAL volumes, precise."""
    naive_utc = open_time_utc.replace(tzinfo=None).isoformat(timespec="seconds")
    kst_naive = (open_time_utc + timedelta(hours=9)).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")
    ts_ms = int(open_time_utc.timestamp() * 1000)
    text = (
        "["
        "{"
        '"market":"KRW-BTC",'
        f'"candle_date_time_utc":"{naive_utc}",'
        f'"candle_date_time_kst":"{kst_naive}",'
        '"opening_price":150000000,'
        '"high_price":150000005,'
        '"low_price":149999995,'
        '"trade_price":150000001,'
        f'"timestamp":{ts_ms},'
        '"candle_acc_trade_price":1234567.89012345,'
        '"candle_acc_trade_volume":0.00821234,'
        f'"unit":{UNIT}'
        "}"
        "]"
    )
    return text.encode("utf-8")


def _bool_ohlc_response_bytes(open_time_utc: datetime) -> bytes:
    """Malformed page: OHLC price is JSON `true` (hostile / buggy source)."""
    naive_utc = open_time_utc.replace(tzinfo=None).isoformat(timespec="seconds")
    kst_naive = (open_time_utc + timedelta(hours=9)).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")
    ts_ms = int(open_time_utc.timestamp() * 1000)
    text = (
        "["
        "{"
        '"market":"KRW-BTC",'
        f'"candle_date_time_utc":"{naive_utc}",'
        f'"candle_date_time_kst":"{kst_naive}",'
        '"opening_price":true,'
        '"high_price":150000005,'
        '"low_price":149999995,'
        '"trade_price":150000001,'
        f'"timestamp":{ts_ms},'
        '"candle_acc_trade_price":150000001,'
        '"candle_acc_trade_volume":1,'
        f'"unit":{UNIT}'
        "}"
        "]"
    )
    return text.encode("utf-8")


def _real_bytes_transport(payload: bytes) -> httpx.MockTransport:
    """MockTransport that serves ``payload`` verbatim for any candles request."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/v1/candles/minutes/")
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(_handler)


class TestRealJsonNumericShapes:
    def test_integer_ohlc_json_loads(self) -> None:
        # ONE 4h candle, INT prices, decimal volume. Assert exact Decimal.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        payload = _int_ohlc_response_bytes(start)
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_real_bytes_transport(payload),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        [candle] = result.candles
        assert candle.open.value == Decimal("150000000")
        assert candle.high.value == Decimal("150000005")
        assert candle.low.value == Decimal("149999995")
        assert candle.close.value == Decimal("150000001")
        assert candle.volume.value == Decimal("0.0000001")

    def test_mixed_int_prices_decimal_volumes(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        payload = _mixed_ohlc_response_bytes(start)
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_real_bytes_transport(payload),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        [candle] = result.candles
        # Prices arrived as JSON ints → exact int Decimals.
        assert candle.open.value == Decimal("150000000")
        assert candle.close.value == Decimal("150000001")
        # Volume arrived as JSON decimal → parsed via parse_float=Decimal
        # so no float ever appeared in the chain.
        assert candle.volume.value == Decimal("0.00821234")
        assert candle.quote_volume.value == Decimal("1234567.89012345")

    def test_boolean_numeric_value_fails(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        payload = _bool_ohlc_response_bytes(start)
        with pytest.raises(Exception, match="bool|Money"):
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=start,
                    end_utc=end,
                    bucket=_fast_bucket(),
                    transport=_real_bytes_transport(payload),
                    now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
                )
            )

    def test_direct_float_construction_still_fails(self) -> None:
        # Wrapper-level D-49 rule: Money/Qty do not accept a float
        # positional argument. This test is independent of the fetch
        # path — it protects the class-level invariant so a future
        # refactor of the adapters cannot silently open the door to
        # float construction.
        with pytest.raises(TypeError):
            Money(0.1)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Qty(0.1)  # type: ignore[arg-type]

    def test_21_digit_decimal_string_preserved_end_to_end(self) -> None:
        # More digits than a float64 can represent, arriving as a
        # QUOTED JSON string. `parse_float=Decimal` isn't invoked for
        # strings — the adapter's `_money_from_input(str)` path is.
        # Exact Decimal must land on the Candle.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        precise = "12345.678901234567890123"  # 21 significant digits total
        naive_utc = start.replace(tzinfo=None).isoformat(timespec="seconds")
        kst_naive = (start + timedelta(hours=9)).replace(
            tzinfo=None
        ).isoformat(timespec="seconds")
        ts_ms = int(start.timestamp() * 1000)
        text = (
            "["
            "{"
            '"market":"KRW-BTC",'
            f'"candle_date_time_utc":"{naive_utc}",'
            f'"candle_date_time_kst":"{kst_naive}",'
            f'"opening_price":"{precise}",'
            f'"high_price":"{precise}",'
            f'"low_price":"{precise}",'
            f'"trade_price":"{precise}",'
            f'"timestamp":{ts_ms},'
            f'"candle_acc_trade_price":"{precise}",'
            f'"candle_acc_trade_volume":"{precise}",'
            f'"unit":{UNIT}'
            "}"
            "]"
        )
        payload = text.encode("utf-8")
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_real_bytes_transport(payload),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        [candle] = result.candles
        assert candle.open.value == Decimal(precise)
        assert candle.volume.value == Decimal(precise)


# ---------------------------------------------------------------------------
# Regression suite for the real-endpoint wiring exposed 2026-09-11
# ---------------------------------------------------------------------------
#
# Root causes proven with the live public endpoint:
#   1. `to` cursor MUST be naive KST (no `+09:00`/`Z`); the offset
#      forms return HTTP 200 with a `{"error": {...}}` envelope.
#   2. Error envelopes must raise a dedicated error carrying only
#      HTTP status + sanitized error name — NEVER get misreported as
#      "candles response must be a JSON array, got dict".


def _error_response_bytes(*, name: "int | str" = 40001, message: str = "bad") -> bytes:
    """Bithumb-shaped error envelope payload (as empirically observed)."""
    import json as _json

    return _json.dumps({"error": {"name": name, "message": message}}).encode("utf-8")


def _error_transport(
    status: int = 200,
    *,
    name: "int | str" = 40001,
) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=_error_response_bytes(name=name),
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(_handler)


class TestOutgoingRequestShape:
    def test_exact_path_and_query_parameters(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        opens = [start]
        to_kst = "2026-01-01T13:00:00"
        pages = {to_kst: [_row(t) for t in reversed(opens)]}
        captured: list[httpx.Request] = []
        asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages, captured_requests=captured),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        [req] = captured
        assert req.method == "GET"
        assert req.url.path == "/v1/candles/minutes/240"
        assert req.url.params["market"] == "KRW-BTC"
        assert req.url.params["count"] == "200"
        assert req.url.params["to"] == to_kst
        assert "authorization" not in {k.lower() for k in req.headers.keys()}

    def test_to_cursor_is_utc_to_kst_conversion(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        opens = [start]
        pages = {"2026-01-01T13:00:00": [_row(t) for t in reversed(opens)]}
        captured: list[httpx.Request] = []
        asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages, captured_requests=captured),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        sent = captured[0].url.params["to"]
        assert sent == "2026-01-01T13:00:00"
        assert "+09:00" not in sent and "Z" not in sent


class TestErrorEnvelopeRefusal:
    def test_http_200_dict_envelope_raises_dedicated_error(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        with pytest.raises(PublicRestErrorResponseError) as exc_info:
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=start,
                    end_utc=end,
                    bucket=_fast_bucket(),
                    transport=_error_transport(status=200, name=40001),
                    now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
                )
            )
        err = exc_info.value
        assert err.status == 200
        assert err.error_name == "40001"
        assert err.endpoint == "/v1/candles/minutes/240"

    def test_http_4xx_error_object_raises_dedicated_error(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        with pytest.raises(PublicRestErrorResponseError) as exc_info:
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=start,
                    end_utc=end,
                    bucket=_fast_bucket(),
                    transport=_error_transport(status=400, name="jwt_verification"),
                    now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
                )
            )
        assert exc_info.value.status == 400
        assert exc_info.value.error_name == "jwt_verification"

    def test_hostile_error_name_collapses_to_unknown(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        with pytest.raises(PublicRestErrorResponseError) as exc_info:
            asyncio.run(
                fetch_candles(
                    "KRW-BTC",
                    unit_minutes=UNIT,
                    start_utc=start,
                    end_utc=end,
                    bucket=_fast_bucket(),
                    transport=_error_transport(status=200, name="bad\nname"),
                    now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
                )
            )
        assert exc_info.value.error_name == "unknown"

    def test_array_success_still_returns_candles(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        opens = [start]
        pages = {"2026-01-01T13:00:00": [_row(t) for t in reversed(opens)]}
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        assert len(result.candles) == 1
        assert result.candles[0].open_time_utc == start


class TestPaginationBackwardWithoutLoop:
    def test_pagination_moves_strictly_backward_no_duplicate_pages(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
        opens_desc_page1 = [
            datetime(2026, 1, 2, 4, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        ]
        opens_desc_page2 = [
            datetime(2026, 1, 1, 20, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 16, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        ]
        page1_to = "2026-01-02T17:00:00"  # end 08:00 UTC = 17:00 KST
        page2_to = "2026-01-02T09:00:00"  # oldest page1 = 00:00 UTC = 09:00 KST
        pages = {
            page1_to: [_row(t) for t in opens_desc_page1],
            page2_to: [_row(t) for t in opens_desc_page2],
        }
        captured: list[httpx.Request] = []
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages, captured_requests=captured),
                now_utc=_fixed_now(datetime(2026, 1, 3, 0, 0, tzinfo=UTC)),
            )
        )
        sent_cursors = [req.url.params["to"] for req in captured]
        assert sent_cursors == sorted(sent_cursors, reverse=True)
        assert len(sent_cursors) == len(set(sent_cursors))
        for c in result.candles:
            assert start <= c.open_time_utc < end

    def test_server_repeating_same_page_breaks_out(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        pages = {
            "2026-01-01T17:00:00": [_row(datetime(2026, 1, 1, 4, 0, tzinfo=UTC))],
        }
        result = asyncio.run(
            fetch_candles(
                "KRW-BTC",
                unit_minutes=UNIT,
                start_utc=start,
                end_utc=end,
                bucket=_fast_bucket(),
                transport=_mock_paged_transport(pages),
                now_utc=_fixed_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
            )
        )
        assert len(result.candles) == 1
