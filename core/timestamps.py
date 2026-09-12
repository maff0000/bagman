"""Canonical UTC time primitives (PID §16).

All canonical BAGMAN timestamps are UTC and timezone-aware. No naive
(timezone-unaware) datetime may appear in canonical BAGMAN domain
state. Original local/provider timestamps may be preserved separately
for evidential reasons, but canonical event time always goes through
this module.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Validate that ``dt`` is a timezone-aware UTC datetime.

    Returns ``dt`` unchanged if it is valid.

    Raises:
        ValueError: if ``dt`` is naive (no ``tzinfo``), or its UTC
            offset is anything other than zero.
    """
    if not isinstance(dt, datetime):
        raise ValueError(f"expected a datetime, got {type(dt).__name__}")

    offset = dt.utcoffset()
    if offset is None:
        raise ValueError(
            "naive datetime is not permitted as canonical BAGMAN time (PID §16); "
            "use core.timestamps.utc_now() or attach tzinfo=timezone.utc"
        )
    if offset.total_seconds() != 0:
        raise ValueError(
            f"canonical BAGMAN time must be UTC (zero offset); got offset {offset}"
        )
    return dt


def to_contract_string(dt: datetime) -> str:
    """Render ``dt`` as a contract-conformant UTC timestamp string.

    Validates ``dt`` with :func:`ensure_utc` first, then serialises it
    to the RFC 3339 / ``format: date-time`` string form used throughout
    ``contracts/`` (offset expressed as ``Z``), e.g.
    ``"2026-09-12T09:41:07.123456Z"``.

    This is a small shared serialisation helper (not part of the
    literal WI-2 module spec) added here because every domain module's
    ``to_dict()`` needs to render its UTC datetime fields the same way;
    duplicating this in six places would be worse than one function in
    the module that already owns UTC time rules.
    """
    ensure_utc(dt)
    return dt.isoformat().replace("+00:00", "Z")
