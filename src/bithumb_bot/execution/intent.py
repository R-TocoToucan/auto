"""Immutable order intent — the strategy's request to the execution core.

An ``OrderIntent`` is constructed from a completed candle ``t`` after
its close is observed. Two invariants are baked in:

1. ``signal_ts_utc == source_open_time_utc + unit_minutes`` — the
   moment the signal fired is candle ``t``'s CLOSE boundary. Equal to
   candle ``t+1``'s OPEN when adjacent candles exist.
2. ``source_open_time_utc`` is preserved so the engine can enforce the
   structural no-same-candle rule at fill time (``fill_candle.open_time_utc
   != intent.source_open_time_utc``) — the rule does not depend on a
   timestamp-comparison convention alone.

Exactly one of ``requested_notional_krw`` / ``requested_qty`` is
populated per side. Neither is the same thing as what actually gets
transacted — the engine may reduce either via qty-step flooring, and
the ledger keeps ``requested_*`` and the resulting ``order_notional_krw``
/ ``filled_qty`` / ``net_acquired_coin`` as three distinct concepts.

Protective-stop intents
-----------------------
A second class of intent exists for protective (long-side) stop exits
that do NOT ride the candle-close signal boundary:

* ``protective_stop_gap``      — fired at a candle's OPEN (gap-through
  of the stop level). ``signal_ts_utc == source_open_time_utc``.
* ``protective_stop_intrabar`` — fired strictly INSIDE the candle when
  the trigger level is crossed intrabar.
  ``source_open_time_utc < signal_ts_utc < source_open_time_utc + unit``.

Both carry a required ``trigger_price`` (Money, > 0), are always
sell-side, and must not fabricate the candle-close instant that the
strategy_signal reason enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from bithumb_bot.core.money import Money, Qty
from bithumb_bot.market_data.candles import Candle

Side = Literal["buy", "sell"]
Reason = Literal[
    "strategy_signal",
    "protective_stop_gap",
    "protective_stop_intrabar",
]

_PROTECTIVE_REASONS: frozenset[str] = frozenset(
    {"protective_stop_gap", "protective_stop_intrabar"}
)
_ALL_REASONS: frozenset[str] = frozenset(
    {"strategy_signal", "protective_stop_gap", "protective_stop_intrabar"}
)


def _require_tz_aware(dt: datetime, field: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field} must be timezone-aware, got {dt.isoformat()}")


@dataclass(frozen=True)
class OrderIntent:
    """Immutable request created from a completed candle ``t``.

    Attributes:
        side:                     ``"buy"`` (marketable KRW) or ``"sell"``
                                  (marketable coin).
        source_open_time_utc:     ``candle_t.open_time_utc`` — kept so the
                                  engine can assert the fill candle is
                                  NOT this same candle.
        unit_minutes:             The source candle's ``unit_minutes``.
        signal_ts_utc:            For ``strategy_signal``:
                                  ``source_open_time_utc + unit_minutes``
                                  (candle ``t``'s close boundary). For
                                  protective stops: the actual trigger
                                  instant (candle open for gap; strictly
                                  intrabar otherwise).
        requested_notional_krw:   Strategy's pre-fee KRW request (buy
                                  side only). ``None`` for sells.
        requested_qty:            Strategy's requested coin quantity
                                  (sell side only). ``None`` for buys.
        reason:                   ``"strategy_signal"`` (default),
                                  ``"protective_stop_gap"``, or
                                  ``"protective_stop_intrabar"``.
        trigger_price:            Required (positive, finite Money) for
                                  protective-stop reasons; must be
                                  ``None`` for ``strategy_signal``.
    """

    side: Side
    source_open_time_utc: datetime
    unit_minutes: int
    signal_ts_utc: datetime
    requested_notional_krw: Money | None
    requested_qty: Qty | None
    reason: Reason = "strategy_signal"
    trigger_price: Money | None = None

    def __post_init__(self) -> None:
        # Universal invariants (every reason, including strategy_signal):
        # tz-aware timestamps, integer + positive unit_minutes, and
        # finite/positive requested amounts. Non-finite Decimals are
        # rejected BEFORE any `<= 0` comparison so a NaN never leaks a
        # decimal.InvalidOperation to the caller.
        _require_tz_aware(self.source_open_time_utc, "source_open_time_utc")
        _require_tz_aware(self.signal_ts_utc, "signal_ts_utc")
        if isinstance(self.unit_minutes, bool) or not isinstance(
            self.unit_minutes, int
        ):
            raise ValueError(
                f"unit_minutes must be int (not bool), got "
                f"{type(self.unit_minutes).__name__}"
            )
        if self.unit_minutes <= 0:
            raise ValueError(
                f"unit_minutes must be > 0, got {self.unit_minutes}"
            )
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.reason not in _ALL_REASONS:
            raise ValueError(
                f"reason must be one of {sorted(_ALL_REASONS)}, got {self.reason!r}"
            )

        close_ts = self.source_open_time_utc + timedelta(minutes=self.unit_minutes)

        if self.reason == "strategy_signal":
            if self.signal_ts_utc != close_ts:
                raise ValueError(
                    f"signal_ts_utc={self.signal_ts_utc.isoformat()} must equal "
                    f"source_open_time_utc + unit_minutes = {close_ts.isoformat()}"
                )
            if self.trigger_price is not None:
                raise ValueError(
                    "strategy_signal intent must not carry trigger_price"
                )
        else:
            # Protective stops: sell-side only, positive finite
            # trigger_price, and reason-specific timing window.
            if self.side != "sell":
                raise ValueError(
                    f"reason={self.reason!r} requires side='sell', got {self.side!r}"
                )
            if self.trigger_price is None:
                raise ValueError(
                    f"reason={self.reason!r} requires trigger_price"
                )
            if not self.trigger_price.value.is_finite():
                raise ValueError(
                    f"trigger_price must be finite, got {self.trigger_price.value}"
                )
            if self.trigger_price.value <= 0:
                raise ValueError(
                    f"trigger_price must be > 0, got {self.trigger_price.value}"
                )
            if self.reason == "protective_stop_gap":
                if self.signal_ts_utc != self.source_open_time_utc:
                    raise ValueError(
                        f"protective_stop_gap requires signal_ts_utc == "
                        f"source_open_time_utc, got "
                        f"{self.signal_ts_utc.isoformat()} vs "
                        f"{self.source_open_time_utc.isoformat()}"
                    )
            else:  # protective_stop_intrabar
                if not (
                    self.source_open_time_utc < self.signal_ts_utc < close_ts
                ):
                    raise ValueError(
                        f"protective_stop_intrabar requires "
                        f"source_open_time_utc < signal_ts_utc < close, got "
                        f"open={self.source_open_time_utc.isoformat()} "
                        f"signal={self.signal_ts_utc.isoformat()} "
                        f"close={close_ts.isoformat()}"
                    )

        if self.side == "buy":
            if self.requested_notional_krw is None:
                raise ValueError("buy intent requires requested_notional_krw")
            if self.requested_qty is not None:
                raise ValueError("buy intent must not carry requested_qty")
            if not self.requested_notional_krw.value.is_finite():
                raise ValueError(
                    f"buy requested_notional_krw must be finite, got "
                    f"{self.requested_notional_krw.value}"
                )
            if self.requested_notional_krw.value <= 0:
                raise ValueError(
                    f"buy requested_notional_krw must be > 0, got "
                    f"{self.requested_notional_krw.value}"
                )
        else:  # sell
            if self.requested_qty is None:
                raise ValueError("sell intent requires requested_qty")
            if self.requested_notional_krw is not None:
                raise ValueError("sell intent must not carry requested_notional_krw")
            if not self.requested_qty.value.is_finite():
                raise ValueError(
                    f"sell requested_qty must be finite, got "
                    f"{self.requested_qty.value}"
                )
            if self.requested_qty.value <= 0:
                raise ValueError(
                    f"sell requested_qty must be > 0, got {self.requested_qty.value}"
                )

    @classmethod
    def buy_from_signal(cls, candle: Candle, notional_krw: Money) -> OrderIntent:
        """Build a buy intent from a completed candle ``t`` and a KRW request."""
        return cls(
            side="buy",
            source_open_time_utc=candle.open_time_utc,
            unit_minutes=candle.unit_minutes,
            signal_ts_utc=candle.open_time_utc
            + timedelta(minutes=candle.unit_minutes),
            requested_notional_krw=notional_krw,
            requested_qty=None,
        )

    @classmethod
    def sell_from_signal(cls, candle: Candle, qty: Qty) -> OrderIntent:
        """Build a sell intent from a completed candle ``t`` and a coin qty."""
        return cls(
            side="sell",
            source_open_time_utc=candle.open_time_utc,
            unit_minutes=candle.unit_minutes,
            signal_ts_utc=candle.open_time_utc
            + timedelta(minutes=candle.unit_minutes),
            requested_notional_krw=None,
            requested_qty=qty,
        )

    @classmethod
    def protective_sell(
        cls,
        *,
        reason: Reason,
        qty: Qty,
        trigger_ts_utc: datetime,
        trigger_price: Money,
        source_open_time_utc: datetime,
        unit_minutes: int,
    ) -> OrderIntent:
        """Build a protective sell intent from an observed stop trigger.

        ``reason`` must be one of the protective-stop values. The
        constructor validates every timing / trigger-price invariant
        via ``__post_init__``.
        """
        if reason not in _PROTECTIVE_REASONS:
            raise ValueError(
                f"protective_sell requires a protective_stop_* reason, "
                f"got {reason!r}"
            )
        return cls(
            side="sell",
            source_open_time_utc=source_open_time_utc,
            unit_minutes=unit_minutes,
            signal_ts_utc=trigger_ts_utc,
            requested_notional_krw=None,
            requested_qty=qty,
            reason=reason,
            trigger_price=trigger_price,
        )


__all__ = ["OrderIntent", "Reason", "Side"]
