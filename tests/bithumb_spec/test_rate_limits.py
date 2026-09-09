"""Tests for :mod:`bithumb_bot.bithumb_spec.rate_limits` — values + citations.

Values were rotated from the initial `capacity=1.0 / refill_rate=0.5`
sentinels to the operator-frozen conservative operational limits on
2026-09-09, deliberately set FAR below Bithumb's documented ceilings
(150 rps public / 140 rps private / 10 conn-s WS per IP, source
apidocs.bithumb.com/docs/api-요청-수-제한-안내, page last-updated
2026-08-26).
"""

from __future__ import annotations

from bithumb_bot.bithumb_spec import rate_limits
from bithumb_bot.rate_limit.token_bucket import TokenBucket


class TestThreeDistinctInstances:
    def test_three_buckets_exist(self) -> None:
        assert isinstance(rate_limits.public_rest, TokenBucket)
        assert isinstance(rate_limits.private_rest, TokenBucket)
        assert isinstance(rate_limits.public_ws, TokenBucket)

    def test_three_distinct_instances(self) -> None:
        assert rate_limits.public_rest is not rate_limits.private_rest
        assert rate_limits.private_rest is not rate_limits.public_ws
        assert rate_limits.public_rest is not rate_limits.public_ws


class TestConservativeOperationalValues:
    """Values are project operational limits, NOT the Bithumb ceiling."""

    def test_public_rest_five_per_second(self) -> None:
        assert rate_limits.public_rest.capacity == 5.0
        assert rate_limits.public_rest.refill_rate == 5.0

    def test_private_rest_two_per_second(self) -> None:
        assert rate_limits.private_rest.capacity == 2.0
        assert rate_limits.private_rest.refill_rate == 2.0

    def test_public_ws_one_per_second(self) -> None:
        assert rate_limits.public_ws.capacity == 1.0
        assert rate_limits.public_ws.refill_rate == 1.0

    def test_all_operational_caps_strictly_below_documented_ceilings(self) -> None:
        # Documented ceilings 2026-09-09: 150 rps public / 140 rps
        # private / 10 conn-s WS per IP per category. Every operational
        # cap must sit at least an order of magnitude below the ceiling
        # so an unannounced downward revision does not immediately
        # invalidate the project's operating point.
        assert rate_limits.public_rest.refill_rate * 10 <= 150.0
        assert rate_limits.private_rest.refill_rate * 10 <= 140.0
        assert rate_limits.public_ws.refill_rate * 10 <= 10.0


class TestDocstringCitations:
    def test_module_docstring_cites_open_verification_item_3(self) -> None:
        assert "Open Verification Item #3" in (rate_limits.__doc__ or "")

    def test_module_docstring_cites_d40_separation(self) -> None:
        assert "D-40" in (rate_limits.__doc__ or "")

    def test_module_docstring_cites_finding_7(self) -> None:
        assert "Finding 7" in (rate_limits.__doc__ or "")

    def test_module_docstring_records_source_url(self) -> None:
        # The apidocs page URL that anchors the documented ceilings.
        assert "apidocs.bithumb.com" in (rate_limits.__doc__ or "")

    def test_module_docstring_records_observation_date(self) -> None:
        # Provenance: the date we fetched the ceiling numbers.
        assert "2026-09-09" in (rate_limits.__doc__ or "")

    def test_module_docstring_records_documented_ceilings(self) -> None:
        # The three ceiling numbers must be recorded so a future reader
        # can see how much headroom the operational caps hold.
        doc = rate_limits.__doc__ or ""
        assert "150 req/s" in doc
        assert "140 req/s" in doc
        assert "10 conn/s" in doc

    def test_module_docstring_calls_out_ws_message_vs_connection_caveat(self) -> None:
        # Caveat #2: `public_ws` limits CONNECTION attempts, not messages.
        doc = rate_limits.__doc__ or ""
        assert "CONNECTION ATTEMPTS" in doc
        assert "not the rate of\n   incoming WebSocket messages" in doc

    def test_module_docstring_calls_out_orders_chance_inference(self) -> None:
        # Caveat #3: /v1/orders/chance classification is inferred.
        doc = rate_limits.__doc__ or ""
        assert "/v1/orders/chance" in doc
        assert "inference" in doc

    def test_module_docstring_calls_out_429_body_still_unresolved(self) -> None:
        # Caveat #4: response 429 body/header shape is not invented.
        doc = rate_limits.__doc__ or ""
        assert "429" in doc
        assert "Do NOT invent" in doc

    def test_module_docstring_calls_out_bithumb_may_lower_limits(self) -> None:
        # Caveat #1: exchange may lower published limits without notice.
        doc = rate_limits.__doc__ or ""
        assert "lower published limits without notice" in doc
