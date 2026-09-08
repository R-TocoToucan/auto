"""Tests for :mod:`bithumb_bot.bithumb_spec.rate_limits` — sentinel + citation."""

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

    def test_sentinel_values(self) -> None:
        for bucket in (
            rate_limits.public_rest,
            rate_limits.private_rest,
            rate_limits.public_ws,
        ):
            assert bucket.capacity == 1.0
            assert bucket.refill_rate == 0.5


class TestDocstringCitations:
    def test_module_docstring_cites_open_verification_item_3(self) -> None:
        assert "Open Verification Item #3" in (rate_limits.__doc__ or "")

    def test_module_docstring_cites_d40_separation(self) -> None:
        assert "D-40" in (rate_limits.__doc__ or "")

    def test_module_docstring_cites_finding_7(self) -> None:
        assert "Finding 7" in (rate_limits.__doc__ or "")

    def test_module_docstring_has_todo_m1_verify_anchor(self) -> None:
        assert "TODO(M1-verify)" in (rate_limits.__doc__ or "")
