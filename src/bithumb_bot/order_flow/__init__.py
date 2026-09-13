"""Translate strategy signals into idempotent Broker.submit calls.

Deliberately narrow layer between ``strategy/`` (pure signal generation)
and ``broker/`` (venue-agnostic order lifecycle). Does no strategy math
and no engine accounting; the only side effect is at most one
``Broker.submit``.
"""
