"""Config subsystem — Gate-1 Decision Register loader + capability-scoped validator.

Modules in this package (added by tasks 01-01-03..01-01-08):
- `gate1_model`         — frozen pydantic v2 `Gate1Decisions` + `StrictDecimal`.
- `gate_loader`         — `load_gate1(path) -> (Gate1Decisions, sha256_hex)`.
- `capability_registry` — D-88 guard matrix encoded as data (D-85).
- `validator`           — `validate(capability) -> ValidationResult` (D-99).
- `config_hash`         — canonical-JSON manifest builder (D-62 / D-76).
"""
