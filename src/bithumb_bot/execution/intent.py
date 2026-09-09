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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from bithumb_bot.core.money import Money, Qty
from bithumb_bot.market_data.candles import Candle

Side = Literal["buy", "sell"]


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
        signal_ts_utc:            ``source_open_time_utc + unit_minutes``
                                  (candle ``t``'s close boundary).
        requested_notional_krw:   Strategy's pre-fee KRW request (buy
                                  side only). ``None`` for sells. This is
                                  the "market-buy request amount" — a
                                  distinct concept from
                                  ``order_notional_krw`` (which is what
                                  the engine actually spends, possibly
                                  reduced by qty-step flooring) and from
                                  ``net_acquired_coin``.
        requested_qty:            Strategy's requested coin quantity
                                  (sell side only). ``None`` for buys.
                                  Distinct from ``filled_qty``, which may
                                  be reduced by qty-step flooring.
    """

    side: Side
    source_open_time_utc: datetime
    unit_minutes: int
    signal_ts_utc: datetime
    requested_notional_krw: Money | None
    requested_qty: Qty | None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        expected_signal = self.source_open_time_utc + timedelta(minutes=self.unit_minutes)
        if self.signal_ts_utc != expected_signal:
            raise ValueError(
                f"signal_ts_utc={self.signal_ts_utc.isoformat()} must equal "
                f"source_open_time_utc + unit_minutes = {expected_signal.isoformat()}"
            )
        if self.side == "buy":
            if self.requested_notional_krw is None:
                raise ValueError("buy intent requires requested_notional_krw")
            if self.requested_qty is not None:
                raise ValueError("buy intent must not carry requested_qty")
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


__all__ = ["OrderIntent", "Side"]
