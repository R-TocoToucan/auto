# Bithumb Autotrader — Claude Instructions

## Project
Personal-use Python Bithumb KRW-BTC spot autotrader.

## Work Rules
- Implement only the exact bounded code change requested by the user.
- Inspect only files directly relevant to that change.
- Do not use GSD, planning files, roadmaps, milestones, skills, subagents, or agent teams.
- Do not perform research, strategy optimization, broad code review, or speculative improvements.
- Do not add strategies, CLI commands, schemas, frameworks, or safety features unless explicitly requested.
- Do not write or run tests, mypy, lint, benchmarks, or full validation suites. Testing is handled externally.
- After implementation, make one local commit containing only the requested code and stop.
- Never push.
- Preserve unrelated tracked, untracked, operator-local, dataset, report, and verification files.

## Invariants
- Never implement withdrawal functionality.
- Never print credentials, JWTs, authorization headers, or raw private responses.
- Use Decimal constructed from strings for money, price, quantity, fee, and PnL. Never convert through float.
- Do not change strategy, fee, slippage, stop, cap, accounting, or live-order behavior unless explicitly requested.
- Real orders require explicit approval in the current prompt.
- Preserve deterministic order identifiers and duplicate-order prevention.

## Final Response
Report only:
- commit hash;
- exact files changed;
- implemented behavior;
- any genuine blocker.
