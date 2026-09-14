"""Translate strategy signals into idempotent Broker.submit calls.

Deliberately narrow layer between ``strategy/`` (pure signal generation)
and ``broker/`` (venue-agnostic order lifecycle). Does no strategy math
and no engine accounting; the only side effect is at most one
``Broker.submit``.

Two entry points live here:

* :func:`bithumb_bot.order_flow.breakout.dispatch_breakout_signal` —
  candle-close strategy signal → buy/sell ``OrderIntent``.
* :func:`bithumb_bot.order_flow.protective.dispatch_protective_stop` —
  observed live trigger → protective-sell ``OrderIntent``, persisting
  the first valid trigger before any broker mutation so a restart
  reconstructs the same deterministic order.
"""
