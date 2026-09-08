# Fixture: `Decimal(-0.1)` and `Decimal(+0.1)` — MUST both be flagged.
# Unary +/- must be unwrapped one level before the float-literal check.
from decimal import Decimal

neg = Decimal(-0.1)
pos = Decimal(+0.1)
