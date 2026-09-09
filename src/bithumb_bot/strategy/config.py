"""Frozen configuration for the one preregistered baseline strategy.

Every field has NO default value. The caller — production code path
or test fixture — MUST supply each parameter explicitly. This is
deliberate: the Gate-2 baseline decision froze the exact values
(``lookback_candles=1200``, ``ma_type="SMA"``, ``warmup_candles=1200``,
``unit_minutes=240``, ``market="KRW-BTC"``), and default-shaped
configuration values would silently drift the semantics if someone
edited the field defaults. Anyone reaching for production values goes
through :meth:`BaselineStrategyConfig.production`, which returns the
frozen tuple exactly once.

Tests may construct :class:`BaselineStrategyConfig` directly with a
smaller ``lookback_candles`` (and matching ``warmup_candles``) so that
hand-verified fixtures remain readable. Production code MUST call
:meth:`production` — the class constants below are the only place the
1,200 literal appears.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BaselineStrategyConfig(BaseModel):
    """Immutable configuration for the ``price_over_sma`` baseline.

    All fields are required (no defaults). ``warmup_candles`` must
    equal ``lookback_candles`` — the baseline's warm-up length is the
    same window the SMA needs, and separating them would create a
    surface for silent misconfiguration.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    # ------------------------------------------------------------------
    # Frozen production values (Gate-2 baseline-definition subset).
    # Reserved for :meth:`production` — do NOT reuse as field defaults.
    # ------------------------------------------------------------------
    PRODUCTION_RULE_ID: ClassVar[Literal["price_over_sma"]] = "price_over_sma"
    PRODUCTION_MA_TYPE: ClassVar[Literal["SMA"]] = "SMA"
    PRODUCTION_LOOKBACK_CANDLES: ClassVar[int] = 1_200
    PRODUCTION_WARMUP_CANDLES: ClassVar[int] = 1_200
    PRODUCTION_UNIT_MINUTES: ClassVar[int] = 240
    PRODUCTION_MARKET: ClassVar[str] = "KRW-BTC"

    # ------------------------------------------------------------------
    # Instance fields — every one required, no defaults.
    # ------------------------------------------------------------------
    rule_id: Literal["price_over_sma"]
    ma_type: Literal["SMA"]
    lookback_candles: int = Field(..., ge=1)
    warmup_candles: int = Field(..., ge=1)
    unit_minutes: int = Field(..., ge=1)
    market: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_warmup_matches_lookback(self) -> BaselineStrategyConfig:
        if self.warmup_candles != self.lookback_candles:
            raise ValueError(
                f"warmup_candles ({self.warmup_candles}) must equal "
                f"lookback_candles ({self.lookback_candles}) — the "
                "baseline's warm-up window IS the SMA window"
            )
        return self

    @classmethod
    def production(cls) -> BaselineStrategyConfig:
        """Return the frozen production configuration.

        The only place the Gate-2 numbers (1,200 candles, 240-minute
        native interval, KRW-BTC) are wired into a ready-to-use
        instance. Callers that need a production strategy configuration
        MUST use this method — do not construct the class directly with
        the production numbers in a production code path.
        """
        return cls(
            rule_id=cls.PRODUCTION_RULE_ID,
            ma_type=cls.PRODUCTION_MA_TYPE,
            lookback_candles=cls.PRODUCTION_LOOKBACK_CANDLES,
            warmup_candles=cls.PRODUCTION_WARMUP_CANDLES,
            unit_minutes=cls.PRODUCTION_UNIT_MINUTES,
            market=cls.PRODUCTION_MARKET,
        )


__all__ = ["BaselineStrategyConfig"]
