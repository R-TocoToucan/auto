# Negative fixture: `Decimal(-1)` — `UnaryOp(USub, Constant(int))` after
# one-level unwrap has an `int` inner, which is safe.
from decimal import Decimal

good = Decimal(-1)
