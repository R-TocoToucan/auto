"""Windows-safe UTC timestamps (D-74).

The spec-snapshot artifact tree (D-74) uses timestamp-named directories
and files. Colons are illegal in filenames on Windows, so we render the
timestamp as ``YYYYMMDDTHHMMSSZ`` (no colons, no fractional seconds).

Both helpers are trivially injectable — the test suite's
``frozen_utc_now`` fixture (in ``tests/conftest.py``) monkey-patches
:func:`utc_now` to a fixed instant so any test needing a stable
timestamp gets bit-for-bit reproducible output.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current UTC instant.

    Returns:
        A ``datetime`` with ``tzinfo=UTC``. Callers MUST treat the
        returned value as opaque and format it via :func:`utc_timestamp`
        when a filename-safe string is needed.
    """
    return datetime.now(UTC)


def utc_timestamp() -> str:
    """Return the current UTC instant as a colon-free string.

    Format: ``YYYYMMDDTHHMMSSZ`` — e.g. ``20260908T012345Z``. This form
    is safe as a Windows filename component (D-74) and sorts
    lexicographically the same as chronologically.

    Returns:
        A 16-character ASCII string matching ``^\\d{8}T\\d{6}Z$``.
    """
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


__all__ = ["utc_now", "utc_timestamp"]
