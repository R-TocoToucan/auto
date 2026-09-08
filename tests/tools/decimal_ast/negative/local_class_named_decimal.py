# Negative fixture: a locally-defined callable named `Decimal` that is
# NEVER imported from stdlib `decimal`. The checker MUST NOT flag it —
# the alias table has no entry for `Decimal` in this file.
class Decimal:
    def __init__(self, value: object) -> None:
        self.value = value


ok = Decimal(0.1)  # not stdlib Decimal → intentionally not flagged
