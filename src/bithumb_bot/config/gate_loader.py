"""Gate-1 TOML loader with hash-of-committed-bytes provenance.

D-58: parse TOML with stdlib `tomllib` in BINARY MODE. `tomllib.load(fp)`
requires an `io.BinaryIO`; a text-mode handle raises `TypeError` on
Windows because of implicit `open()` encoding differences. This module
uses `path.read_bytes()` + `tomllib.loads(bytes.decode("utf-8"))` — same
end result, single-code-path.

D-62 / Finding 3: the returned `sha256` hex is `sha256(<committed
bytes>).hexdigest()`. We hash the exact bytes git tracks, not a
re-serialization of the parsed model — TOML has no single canonical
byte form and hashing the file itself avoids inventing a second,
parallel canonicalization rule just for TOML. This hash feeds the
`config_hash` manifest (task 01-01-08) and, in Phase 5, FRZ-02.

D-60 fail-closed contract — every structural problem raises
`Gate1LoadError`:
  * FileNotFoundError                → Gate1LoadError (path in message)
  * tomllib.TOMLDecodeError          → Gate1LoadError (wrapped)
  * pydantic.ValidationError         → Gate1LoadError (wrapped)
  * status != "frozen"               → Gate1LoadError
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from pydantic import ValidationError

from bithumb_bot.config.gate1_model import Gate1Decisions
from bithumb_bot.errors import Gate1LoadError


def load_gate1(path: Path) -> tuple[Gate1Decisions, str]:
    """Load and validate the Gate-1 TOML at `path`.

    Returns:
        (Gate1Decisions, hex_sha256): parsed frozen model + hex-encoded
        SHA-256 of the file's exact bytes.

    Raises:
        Gate1LoadError: for any structural failure (missing file,
            malformed TOML, pydantic ValidationError, non-frozen status).
    """
    if not path.is_file():
        raise Gate1LoadError(
            f"gate1.toml not found at {path!s}. The file must exist and be a "
            "committed file inside `config/decisions/` (D-55, D-57).",
            path=path,
        )

    raw_bytes = path.read_bytes()
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise Gate1LoadError(
            f"gate1.toml at {path!s} is not valid TOML: {exc}",
            path=path,
        ) from exc
    except UnicodeDecodeError as exc:
        raise Gate1LoadError(
            f"gate1.toml at {path!s} is not valid UTF-8: {exc}",
            path=path,
        ) from exc

    # Precheck the status field BEFORE handing to pydantic so a non-frozen
    # file produces a specific, actionable message instead of a generic
    # `Literal["frozen"]` mismatch inside a pydantic error list.
    if parsed.get("status") != "frozen":
        raise Gate1LoadError(
            f"gate1.toml at {path!s} has status={parsed.get('status')!r}; "
            "only status=\"frozen\" is accepted (D-60 fail-closed).",
            path=path,
        )

    try:
        gate1 = Gate1Decisions(**parsed)
    except ValidationError as exc:
        raise Gate1LoadError(
            f"gate1.toml at {path!s} failed schema validation: {exc}",
            path=path,
        ) from exc

    return gate1, sha256_hex


__all__ = ["load_gate1"]
