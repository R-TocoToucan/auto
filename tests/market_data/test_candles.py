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

from bithumb_bot.errors import (
    CandleValidationError,
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
        to_kst = "2026-01-02T09:00:00+09:00"
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
            key = (cursor + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S+09:00")
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
        # last CLOSED candle boundary. now is 05:15 → last closed 4h
        # candle opens at 00:00 (closes at 04:00). effective_end = 04:00.
        # Requested start = 20:00 previous day → expected candles:
        # 20:00, 00:00. The in-progress 04:00 candle is excluded.
        start = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
        end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)  # goes past incomplete candle
        now = datetime(2026, 1, 2, 5, 15, tzinfo=UTC)
        opens = [
            datetime(2026, 1, 1, 20, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        ]
        # Effective end is 04:00 UTC = 13:00 KST.
        to_kst = "2026-01-02T13:00:00+09:00"
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
        assert result.effective_end_utc == datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
        assert [c.open_time_utc for c in result.candles] == opens

    def test_uses_kst_cursor_not_utc(self) -> None:
        # Verify the `to` query param is KST-formatted with +09:00 suffix.
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
        opens = [start]
        to_kst = "2026-01-01T13:00:00+09:00"  # 04:00 UTC = 13:00 KST
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
        assert "+09:00" in req.url.params["to"]

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
        to_kst = "2026-01-02T09:00:00+09:00"
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
        page1_to = "2026-01-01T17:00:00+09:00"  # 08:00 UTC
        page2_to = "2026-01-01T13:00:00+09:00"  # 04:00 UTC
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
        page1_to = "2026-01-01T17:00:00+09:00"
        page2_to = "2026-01-01T13:00:00+09:00"
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
        to_kst = "2026-01-01T13:00:00+09:00"
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
        from bithumb_bot.core.money import Money, Qty

        assert isinstance(candle.open, Money)
        assert isinstance(candle.volume, Qty)
