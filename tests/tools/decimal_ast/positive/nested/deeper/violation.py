# Fixture (nested): confirms `tools.decimal_ast_check` recursively walks
# directory paths. Must be discovered by scanning `.../positive/` root.
from decimal import Decimal

nested_violation = Decimal(2.5)
