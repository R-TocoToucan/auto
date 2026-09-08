# Negative fixture: `Decimal(1)` — int literals are exact and safe.
from decimal import Decimal

good = Decimal(1)
