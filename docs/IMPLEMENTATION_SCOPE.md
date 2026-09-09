# Implementation Scope

## Purpose

This document separates what must be implemented now from features that may be added later.

The immediate goal is a small, trustworthy **research and paper-trading bot**, not a complete institutional trading platform. `docs/EXECUTION.md` remains authoritative for safety and correctness; this file limits scope and prevents unnecessary expansion.

## Scope Rules

- Preserve the completed Phase 1 foundation. Do not rebuild it without a demonstrated defect.
- Implement the smallest end-to-end path that can produce credible results.
- Do not add abstractions, frameworks, CLI commands, extension points, research passes, or documentation for hypothetical future needs.
- Adaptive model selection may operate normally, but it must not expand functionality.
- No real order, trade credential, or final holdout use is authorized by this document.

## Already Complete — Reuse Only

Phase 1 already provides:

- frozen Gate 1 decisions and capability validation;
- Decimal-safe money and quantity types;
- credential and secret-loading safeguards;
- the `core/` to `broker/` import boundary;
- the read-only Bithumb specification adapter;
- hashed fee, tick, step, and minimum-order snapshots; and
- the existing CLI and related tests.

These components should be reused, not redesigned.

## Implement Now — Research MVP

### 1. KRW-BTC data pipeline

- Fetch native Bithumb 240-minute candles.
- Normalize timestamps to UTC.
- Reject malformed, duplicate, non-monotonic, and impossible OHLC records.
- Report missing intervals without fabricating candles.
- Persist a reproducible dataset with provenance and an integrity hash.

### 2. Conservative execution simulator

- Calculate signals only from completed candle `t`.
- Fill ordinary entries and signal exits no earlier than the next observable execution point.
- Support only marketable KRW buys and coin sells required by the baseline.
- Apply fees, conservative per-side slippage, tick/step rounding, minimum-order rules, and the validated-notional cap.
- Use Decimal arithmetic for every monetary and quantity calculation.
- Model the frozen client-side protective sell trigger, intrabar ambiguity, and adverse gaps conservatively.
- Maintain a complete cash, position, cost, fee, and P&L ledger.
- Produce identical results from identical inputs and configuration.

Required tests should focus on hand-calculated cases: next-observation fills, fee/slippage accounting, rounding, insufficient funds, minimum orders, stop triggers, adverse gaps, same-candle stop/target ambiguity, missing data, cap violations, and deterministic replay.

### 3. One baseline strategy

- One transparent long-or-cash trend or momentum baseline.
- KRW-BTC only.
- Native 240-minute candles only.
- Small and explicit parameter set.
- No machine learning, automatic optimization, or multiple strategy families.
- Every entry and exit must be explainable from stored inputs and state.

### 4. Minimal evaluation

Report:

- gross and net return;
- aligned buy-and-hold return;
- maximum drawdown and one clearly defined risk-adjusted metric;
- trade count, win rate, turnover, and average holding period;
- total fees and modeled slippage;
- rejected or unvalidated intents;
- equity curve; and
- machine-readable trade ledger.

Selection results and any locked holdout result must remain clearly separated. Opening the final holdout requires separate human authorization.

### 5. Paper operation

- Consume public/read-only market data.
- Calculate signals only on completed candles.
- Route hypothetical orders through the same accounting path as the simulator.
- Persist restart-safe paper state and an audit log.
- Never load trade credentials or call real-order endpoints.

## Deferred — Do Not Implement Yet

### Strategy expansion

- regime classifiers, volatility targeting, and mean reversion;
- strategy ensembles and online adaptation;
- machine-learning or LLM signals;
- automatic parameter optimization and Kelly sizing; and
- pairs, cross-sectional, or multi-asset strategies.

### Market and data expansion

- assets other than KRW-BTC;
- other exchanges, arbitrage, transfers, futures, leverage, or shorting;
- third-party historical feeds and persistent L2 archives;
- quantity-dependent impact models; and
- synthetic 6-hour or other additional strategy timeframes.

### Order and execution expansion

- resting limit, maker, Post-Only, `best`, and cancel-and-replace strategies;
- legacy `/trade/stop_limit` integration;
- smart order routing; and
- exchange-specific execution mechanisms not required by the baseline.

### Live trading and operations

- trade-enabled credentials and real orders;
- live partial-fill recovery and broker reconciliation;
- a live independent watchdog and runtime liquidity guard;
- capital escalation;
- unattended or multi-instance deployment;
- dashboards and notification integrations; and
- cloud or container orchestration.

### Advanced research and product features

- large strategy grids, PBO, Deflated Sharpe, and White's Reality Check;
- extensive bootstrap and publication-grade reporting;
- graphical or mobile interfaces;
- user accounts, tax reporting, and SaaS features; and
- news, social, or sentiment ingestion.

Deferred functionality may move into active scope only after the user explicitly approves the problem it solves, its smallest implementation, and the evidence required to retain it.

## Research MVP Definition of Done

The active implementation is complete when:

1. KRW-BTC 240-minute data can be fetched, validated, stored, and replayed.
2. The simulator has no same-candle signal fill and passes the focused accounting and stop tests.
3. One baseline strategy runs end to end with an auditable ledger and report.
4. Paper operation runs without trade credentials or real orders.
5. Relevant tests pass and no deferred feature was added for future flexibility.

Completion means the program can produce trustworthy evidence. It does **not** mean the strategy is profitable or ready for live trading.
