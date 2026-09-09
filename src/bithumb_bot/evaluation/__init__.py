"""Minimal backtest performance evaluation + machine-readable report.

Scope-locked to Research MVP §4 of ``docs/IMPLEMENTATION_SCOPE.md``.
Consumes an existing :class:`~bithumb_bot.backtest.BacktestResult`
and produces a frozen :class:`PerformanceReport`. **Does not re-run,
alter, or optimize the strategy.**

Deferred (do NOT add here): Sortino, Calmar, DSR, PSR, PBO, bootstrap,
Reality Check, SPA, purged CV, plotting, HTML, notebooks, databases,
CLI handlers, dashboards, generalized reporting frameworks, or a
second strategy simulation.
"""

from bithumb_bot.evaluation.report import (
    PERIODS_PER_YEAR,
    RISK_FREE_RATE,
    EquityPoint,
    PerformanceReport,
    evaluate_backtest,
)

__all__ = [
    "PERIODS_PER_YEAR",
    "RISK_FREE_RATE",
    "EquityPoint",
    "PerformanceReport",
    "evaluate_backtest",
]
