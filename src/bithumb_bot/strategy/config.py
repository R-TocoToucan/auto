"""Frozen configuration for the one preregistered baseline strategy.

Every field has NO default value. The caller — production code path
or test fixture — MUST supply each parameter explicitly. This is
deliberate: the engineering research candidate holds
``lookback_candles=1200``, ``ma_type="SMA"``, ``warmup_candles=1200``,
``unit_minutes=240``, ``market="KRW-BTC"``, and a cost-derived
``hysteresis_bps=75`` band; default-shaped configuration values would
silently drift the semantics if someone edited the field defaults.
Anyone reaching for production values goes through
:meth:`BaselineStrategyConfig.production`, which returns the frozen
tuple exactly once.

The ``hysteresis_bps=75`` band is a **cost-derived** parameter, not an
optimized parameter: the modeled one-way execution cost of 75 bps
(50 bps slippage + 25 bps fee) is used directly to bound state-
transition churn. It is an engineering research candidate — Gate 2
has not frozen a final baseline rule.

Tests may construct :class:`BaselineStrategyConfig` directly with a
smaller ``lookback_candles`` (and matching ``warmup_candles``) so that
hand-verified fixtures remain readable. Production code MUST call
:meth:`production` — the class constants below are the only place the
1,200 literal and the 75 bps hysteresis appear.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# 1 unit = 1 bp; the state-transition rule uses the exact integer form
# ``close * lookback * 10000 (>|<) sum * (10000 (±) hysteresis_bps)``,
# so division never touches Decimal.
_BPS_DENOMINATOR: Decimal = Decimal("10000")


class BaselineStrategyConfig(BaseModel):
    """Immutable configuration for the ``price_over_sma`` baseline.

    All fields are required (no defaults). ``warmup_candles`` must
    equal ``lookback_candles`` — the baseline's warm-up length is the
    same window the SMA needs, and separating them would create a
    surface for silent misconfiguration.

    ``hysteresis_bps`` is a required cost-derived band width in basis
    points (1 bp = 1/10,000). ``0`` is a valid engineering value —
    it collapses the band to the zero-width crossover. The upper
    boundary is ``0 <= hysteresis_bps < 10000``: a band of 100% would
    make the exit boundary non-positive.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    # ------------------------------------------------------------------
    # Frozen production values (engineering research candidate).
    # Reserved for :meth:`production` — do NOT reuse as field defaults.
    # ------------------------------------------------------------------
    PRODUCTION_RULE_ID: ClassVar[Literal["price_over_sma"]] = "price_over_sma"
    PRODUCTION_MA_TYPE: ClassVar[Literal["SMA"]] = "SMA"
    PRODUCTION_LOOKBACK_CANDLES: ClassVar[int] = 1_200
    PRODUCTION_WARMUP_CANDLES: ClassVar[int] = 1_200
    PRODUCTION_UNIT_MINUTES: ClassVar[int] = 240
    PRODUCTION_MARKET: ClassVar[str] = "KRW-BTC"
    #: 75 bps = 50 bps modeled slippage + 25 bps fee (one-way execution
    #: cost). Cost-derived, not optimized. Engineering research
    #: candidate; not a Gate-2 freeze.
    PRODUCTION_HYSTERESIS_BPS: ClassVar[Decimal] = Decimal("75")

    # ------------------------------------------------------------------
    # Instance fields — every one required, no defaults.
    # ------------------------------------------------------------------
    rule_id: Literal["price_over_sma"]
    ma_type: Literal["SMA"]
    lookback_candles: int = Field(..., ge=1)
    warmup_candles: int = Field(..., ge=1)
    unit_minutes: int = Field(..., ge=1)
    market: str = Field(..., min_length=1)
    hysteresis_bps: Decimal

    @field_validator("hysteresis_bps", mode="before")
    @classmethod
    def _reject_non_decimal_hysteresis(cls, value: object) -> Decimal:
        """Reject float / int / str / bool construction (D-49).

        The only accepted input is a bare ``Decimal`` instance
        constructed from a string. The TOML loader wraps its string
        value in ``Decimal(...)`` before reaching pydantic, so
        legitimate producers pass through untouched.
        """
        if isinstance(value, bool):
            raise ValueError(
                "hysteresis_bps must be Decimal (D-49); rejecting bool "
                f"({value!r}). Construct with Decimal('75')."
            )
        if isinstance(value, Decimal):
            return value
        raise ValueError(
            "hysteresis_bps must be Decimal (D-49); rejecting "
            f"{type(value).__name__!r}. Construct with Decimal('75') — "
            "never a float, int, or bare string."
        )

    @model_validator(mode="after")
    def _check_warmup_matches_lookback(self) -> BaselineStrategyConfig:
        if self.warmup_candles != self.lookback_candles:
            raise ValueError(
                f"warmup_candles ({self.warmup_candles}) must equal "
                f"lookback_candles ({self.lookback_candles}) — the "
                "baseline's warm-up window IS the SMA window"
            )
        return self

    @model_validator(mode="after")
    def _check_hysteresis_range(self) -> BaselineStrategyConfig:
        if not (Decimal("0") <= self.hysteresis_bps < _BPS_DENOMINATOR):
            raise ValueError(
                "hysteresis_bps must satisfy 0 <= x < 10000, got "
                f"{self.hysteresis_bps}"
            )
        return self

    @classmethod
    def production(cls) -> BaselineStrategyConfig:
        """Return the engineering research candidate configuration.

        The only place the engineering-candidate numbers (1,200
        candles, 240-minute native interval, KRW-BTC, 75 bps
        cost-derived hysteresis) are wired into a ready-to-use
        instance. Callers that need the research configuration MUST
        use this method — do not construct the class directly with
        the candidate numbers in a production code path.
        """
        return cls(
            rule_id=cls.PRODUCTION_RULE_ID,
            ma_type=cls.PRODUCTION_MA_TYPE,
            lookback_candles=cls.PRODUCTION_LOOKBACK_CANDLES,
            warmup_candles=cls.PRODUCTION_WARMUP_CANDLES,
            unit_minutes=cls.PRODUCTION_UNIT_MINUTES,
            market=cls.PRODUCTION_MARKET,
            hysteresis_bps=cls.PRODUCTION_HYSTERESIS_BPS,
        )


__all__ = ["BaselineStrategyConfig"]
