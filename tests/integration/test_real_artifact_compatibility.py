"""End-to-end: does the real-shape sanitized snapshot drive the simulator?

Uses ONLY the committed CI-safe sanitized fixture at
``tests/fixtures/bithumb/sanitized/orders_chance/observed_krw_btc.json``
— NO dependency on any operator-local ``artifacts/spec_snapshots/**``
path.

Two tests:

* :class:`TestReadinessDiagnostic` — always runs. Reads the committed
  fixture, verifies its SHA-256 sidecar, drives the production
  normalization path (``sanitize_orders_chance`` →
  ``OrdersChanceResponse.model_validate`` → ``build_snapshot`` →
  ``write_snapshot_with_sidecar`` → ``load_snapshot``), and asserts:
  - ``artifact_integrity = valid`` (proved by ``load_snapshot``
    returning without raising);
  - ``execution_readiness = unresolved`` with the exact 5-entry
    ``missing_requirements`` set expected for the real observed shape.
  This is the "honest, specific unresolved venue-fact blocker" that
  Batch 1's completion clause names.
* :class:`TestExecutableCompatibility` — skips with a precise
  readiness reason until Batch 1B lands the missing facts. When
  ready, it will run buy → sell → backtest → evaluation on integer
  JSON candles with no monkey-patching of tick/step/fees/min/order
  types. A test that expects refusal is NOT proof of compatibility.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bithumb_bot.artifact.canonical import sha256_hex
from bithumb_bot.bithumb_spec.sanitize import sanitize_orders_chance
from bithumb_bot.bithumb_spec.schemas import OrdersChanceResponse
from bithumb_bot.bithumb_spec.snapshot import (
    build_snapshot,
    load_snapshot,
    write_snapshot_with_sidecar,
)
from bithumb_bot.config.validator import REPO_ROOT_ENV
from bithumb_bot.execution.readiness import check_execution_readiness


FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "fixtures"
    / "bithumb"
    / "sanitized"
    / "orders_chance"
)
OBSERVED_JSON = FIXTURE_DIR / "observed_krw_btc.json"
OBSERVED_SIDECAR = FIXTURE_DIR / "observed_krw_btc.json.sha256"


@pytest.fixture()
def _validate_ok(
    tmp_path: Path,
    tmp_gate1_toml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load_snapshot` calls `validate(('m1', 'verify-snapshot'))`; point
    the validator at the tmp gate1.toml so the check passes offline."""
    monkeypatch.setenv(REPO_ROOT_ENV, str(tmp_gate1_toml.parent.parent.parent))


def _assert_no_forbidden_fields(sanitized: dict[str, object]) -> None:
    """Belt-and-suspenders: the committed fixture must contain none of
    the D-77 forbidden top-level keys (already enforced by the
    allowlist sanitizer at build time; we re-check at test time so a
    future fixture tweak cannot silently ship a private field)."""
    forbidden = {
        "access_key",
        "secret_key",
        "Authorization",
        "Set-Cookie",
        "nonce",
        "signature",
        "account_id",
        "balance",
        "avg_buy_price",
        "locked_balance",
    }
    leaked = forbidden.intersection(sanitized.keys())
    assert not leaked, f"forbidden fields leaked into fixture: {sorted(leaked)!r}"


class TestReadinessDiagnostic:
    """Batch 1A completion oracle: the readiness diagnostic must return
    the exact unresolved-fact set for the real observed shape."""

    def test_committed_fixture_and_sidecar_are_tracked(self) -> None:
        assert OBSERVED_JSON.is_file(), (
            f"committed sanitized fixture missing: {OBSERVED_JSON}"
        )
        assert OBSERVED_SIDECAR.is_file(), (
            f"committed sanitized-fixture sidecar missing: {OBSERVED_SIDECAR}"
        )

    def test_fixture_sidecar_matches_bytes(self) -> None:
        # Defense-in-depth: verify the sidecar attestation matches the
        # on-disk fixture bytes. The sidecar format is `<64-hex>  <name>`
        # (sha256sum-compatible).
        recorded = OBSERVED_SIDECAR.read_text(encoding="utf-8").strip().split()
        assert len(recorded) >= 1 and len(recorded[0]) == 64
        assert recorded[0] == sha256_hex(OBSERVED_JSON.read_bytes())

    def test_fixture_contains_no_forbidden_fields(self) -> None:
        raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        _assert_no_forbidden_fields(raw)

    def test_readiness_reports_exact_unresolved_facts(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        # Production normalization path — no invented values.
        raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
        sanitized = sanitize_orders_chance(raw)
        parsed = OrdersChanceResponse.model_validate(sanitized)
        snapshot = build_snapshot(
            parsed, market="KRW-BTC", fixture_paths=[OBSERVED_JSON]
        )

        target = tmp_path / "snap.json"
        write_snapshot_with_sidecar(snapshot, target)

        # Artifact integrity: `load_snapshot` verifies the sidecar and
        # schema. If it returns, the artifact is valid.
        loaded = load_snapshot(target)
        assert loaded.market == "KRW-BTC"
        assert loaded.fee_rates.bid == Decimal("0.0025")
        assert loaded.fee_rates.ask == Decimal("0.0025")
        assert loaded.minimums.krw_min_total_bid == Decimal("5000")
        assert loaded.minimums.krw_min_total_ask == Decimal("5000")

        # Execution readiness: diagnostic-only, separate from artifact
        # integrity. Strict fee policy (``allow_provisional_fee_model=
        # False``) matches what a real strategy-evaluation run would
        # accept by default — passed inline, never fabricated.
        report = check_execution_readiness(loaded, allow_provisional_fee_model=False)
        assert report.execution_readiness == "unresolved"
        # Fixed documented order per bithumb_bot.execution.readiness:
        # 2. market_buy_fee_reservation  (provisional_documented; opt-in off)
        # 3. default_tick                 (bid.price_unit absent in response)
        # 4. default_step                 (never exposed by /v1/orders/chance)
        # 9. market_buy_price_support     (absent from verification_status)
        # 10. market_sell_market_support  (absent from verification_status)
        assert report.missing_requirements == (
            "market_buy_fee_reservation",
            "default_tick",
            "default_step",
            "market_buy_price_support",
            "market_sell_market_support",
        )


class TestExecutableCompatibility:
    """Runs buy → sell → backtest → evaluation only after every required
    fact is present. Until then, it SKIPs with a precise readiness
    reason — a skip is NOT execution compatibility, only a truthful
    surface of the unresolved facts.
    """

    def test_end_to_end_when_ready(
        self, tmp_path: Path, _validate_ok: None
    ) -> None:
        raw = json.loads(OBSERVED_JSON.read_text(encoding="utf-8"))
        sanitized = sanitize_orders_chance(raw)
        parsed = OrdersChanceResponse.model_validate(sanitized)
        snapshot = build_snapshot(
            parsed, market="KRW-BTC", fixture_paths=[OBSERVED_JSON]
        )
        target = tmp_path / "snap.json"
        write_snapshot_with_sidecar(snapshot, target)
        loaded = load_snapshot(target)
        report = check_execution_readiness(loaded, allow_provisional_fee_model=False)
        if report.execution_readiness != "ready":
            pytest.skip(
                "execution_readiness=unresolved; missing="
                + ", ".join(report.missing_requirements)
                + " — will run automatically when Batch 1B establishes the"
                " missing facts."
            )
        # When the skip lifts, the body below runs an actual buy → sell
        # → backtest → evaluation on real-shape integer JSON candles.
        # Deliberately unreachable in Batch 1A; the plan reserves the
        # concrete assertions for the batch that actually resolves the
        # facts, so no invented numbers are shipped here.
        pytest.fail(
            "Batch 1B has landed; extend this test with the deferred"
            " buy → sell → backtest → evaluation body."
        )
