"""Shared pytest fixtures for the whole test suite.

Fixtures declared here per VALIDATION.md Wave-0 requirement #2:

- `tmp_gate1_toml`         — writes a minimal valid Gate-1 TOML into a
                             pytest tmp directory; tests copy-and-mutate it.
- `sanitized_fixture_loader` — loads a JSON fixture from
                             `tests/fixtures/bithumb/sanitized/<endpoint>/<ts>.json`;
                             parametrizable path.
- `fake_monotonic_clock`   — a callable returning `float` that only advances
                             when the test explicitly calls `.tick(dt_seconds)`.
                             Used by token-bucket tests in plan 01-04.
                             Never calls `time.monotonic()`.
- `frozen_utc_now`         — monkey-patches `bithumb_bot.artifact.timestamps.utc_now`
                             (added in plan 01-04) to a fixed `datetime`. Safely
                             no-ops if that module has not yet been created.

All fixtures MUST load without ImportError even before later-plan modules exist
(defensive imports — see `frozen_utc_now` in particular).
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pytest


# -----------------------------------------------------------------------------
# tmp_gate1_toml
# -----------------------------------------------------------------------------

_MINIMAL_GATE1_TOML = """\
schema_version = 1
status = "frozen"
approved_at_utc = "2026-09-08T00:00:00Z"
source_commit = "0000000000000000000000000000000000000000"
research_spec_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
execution_spec_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

# D-40 — M1 read-only HTTP behavior (frozen Gate-1)
m1_spec_http_connect_timeout_ms = 5000
m1_spec_http_read_timeout_ms = 15000
m1_spec_http_max_attempts = 3
m1_spec_http_backoff_initial_ms = 500
m1_spec_http_backoff_cap_ms = 5000
m1_spec_http_jitter = "full"

# D-09 / D-43 — cap fields
# TOML 1.0.0 has no null literal; value-deferred fields use an inline-table
# sentinel { value = "unset", frozen_at = "gateN", frozen_at_phase = N } that
# the pydantic Gate1Decisions loader normalises to Python None.
max_validated_notional_krw = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
provisional_engineering_notional_krw = "100000"

# D-41 — Gate-2 schema-frozen fields (value-deferred to Phase 3)
max_received_trade_delivery_lag_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
public_ws_transport_liveness_timeout_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
fallback_rest_poll_interval_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
trigger_rest_connect_timeout_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
trigger_rest_read_timeout_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
max_unverified_interval_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
ws_recovery_stability_window_ms = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }
ws_recovery_min_valid_events = { value = "unset", frozen_at = "gate2", frozen_at_phase = 3 }

# D-42 — Gate-3 schema-frozen fields (value-deferred to Phase 5)
watchdog_heartbeat_interval_ms = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
watchdog_lease_ttl_ms = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
ws_reconnect_backoff_initial_ms = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
ws_reconnect_backoff_cap_ms = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
ws_reconnect_jitter_policy = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
per_reconnect_cycle_attempt_limit = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
reconnect_circuit_breaker_window_ms = { value = "unset", frozen_at = "gate3", frozen_at_phase = 5 }
"""


@pytest.fixture()
def tmp_gate1_toml(tmp_path: Path) -> Path:
    """Write a minimal valid Gate-1 TOML into `tmp_path/config/decisions/gate1.toml`.

    Tests may read + rewrite this file to inject mutations (extra keys, unquoted
    decimals, unfrozen status, etc.) without touching the committed real TOML.
    """
    target = tmp_path / "config" / "decisions" / "gate1.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINIMAL_GATE1_TOML, encoding="utf-8")
    return target


# -----------------------------------------------------------------------------
# sanitized_fixture_loader
# -----------------------------------------------------------------------------


@pytest.fixture()
def sanitized_fixture_loader() -> Callable[[str, str], Any]:
    """Load a sanitized Bithumb fixture from `tests/fixtures/bithumb/sanitized/`.

    Signature: `loader(endpoint: str, timestamp: str) -> parsed_json`.
    Resolves to `tests/fixtures/bithumb/sanitized/<endpoint>/<timestamp>.json`
    relative to the repository root. Raises FileNotFoundError with a clear
    message if the fixture is missing.
    """
    fixtures_root = Path(__file__).parent / "fixtures" / "bithumb" / "sanitized"

    def _load(endpoint: str, timestamp: str) -> Any:
        path = fixtures_root / endpoint / f"{timestamp}.json"
        if not path.is_file():
            raise FileNotFoundError(f"sanitized fixture not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    return _load


# -----------------------------------------------------------------------------
# fake_monotonic_clock
# -----------------------------------------------------------------------------


@dataclass
class FakeMonotonicClock:
    """Deterministic monotonic-clock stand-in for the token-bucket tests.

    - `clock()` returns the current fake time in seconds (float).
    - `clock.tick(dt_seconds)` advances the clock; negative deltas are rejected.
    - The clock NEVER calls `time.monotonic()` — every test run is bit-for-bit
      reproducible.
    """

    _now: float = field(default=0.0)

    def __call__(self) -> float:
        return self._now

    def tick(self, dt_seconds: float) -> None:
        if dt_seconds < 0:
            raise ValueError(
                f"FakeMonotonicClock.tick() requires dt_seconds >= 0, got {dt_seconds!r}"
            )
        self._now += dt_seconds


@pytest.fixture()
def fake_monotonic_clock() -> FakeMonotonicClock:
    """Return a fresh FakeMonotonicClock. See docstring on the class."""
    return FakeMonotonicClock()


# -----------------------------------------------------------------------------
# frozen_utc_now
# -----------------------------------------------------------------------------


@pytest.fixture()
def frozen_utc_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze `bithumb_bot.artifact.timestamps.utc_now` to a fixed value.

    The `bithumb_bot.artifact.timestamps` module is added by plan 01-04. Before
    it exists we must NOT raise — this fixture is meant to be usable from any
    test file regardless of which plan wave has landed. Behavior:

    - If `bithumb_bot.artifact.timestamps` importable: monkey-patch its
      `utc_now` symbol to return the frozen datetime.
    - Else: no-op (still returns the datetime so tests can use it as a value).
    """
    frozen = datetime(2026, 9, 8, 12, 34, 56, tzinfo=UTC)
    try:
        module = importlib.import_module("bithumb_bot.artifact.timestamps")
    except ImportError:
        return frozen
    if hasattr(module, "utc_now"):
        monkeypatch.setattr(module, "utc_now", lambda: frozen)
    return frozen
