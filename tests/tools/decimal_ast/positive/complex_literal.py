# Fixture: `Decimal(1j)` — complex-literal argument MUST be flagged.
from decimal import Decimal

q = Decimal(1j)
