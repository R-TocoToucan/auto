# Negative fixture: `Decimal("0.1")` — the canonical safe construction.
from decimal import Decimal

good_a = Decimal("0.1")
good_b = Decimal("-0.1")
