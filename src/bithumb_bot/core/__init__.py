"""Offline / deterministic core.

Import Linter forbidden-source (plan 01-03): NO module in `bithumb_bot.core` may
import from `bithumb_bot.broker`. This boundary is CI-enforced (D-71, D-73).
Violation is a build failure, not a warning.
"""
