# Negative fixture: `Decimal(some_string_var)` — dynamic argument.
# Explicit non-goal per D-72 scope: the checker does not attempt to
# infer whether a Name-bound value is a float; it simply does not flag.
from decimal import Decimal

some_string_var = "0.1"
good = Decimal(some_string_var)
