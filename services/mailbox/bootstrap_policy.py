"""Mailbox-sweep historical-bootstrap POLICY — pure functions only (CD-6
architect amendment, second correction).

Why this lives here, not in ``core/`` or on ``GovernedEntity`` itself
------------------------------------------------------------------------
This is genuinely mailbox-SWEEP policy — "how far back should the
first-ever sweep of a mailbox reach into history" — not a canonical
domain fact about a ``core.entity.GovernedEntity``. ``core.entity``
owns exactly one real, canonical fact relevant here:
``fiscal_year_start_month_day`` (the entity's own RECURRING
accounting-period-start rule) and one optional, real-world CLAMP fact,
``historical_floor_override_at`` (see that field's own docstring). How
those two facts get turned into "the actual date a mailbox sweep
should bootstrap from" is a derivation this module owns, on purpose,
kept out of ``core/`` (PID §53: canonical domain models must never
depend on sweep-specific policy; the reverse — sweep policy reading a
canonical domain object — is exactly what this module does).

The bug this module fixes (architect finding, 2026-09-18)
------------------------------------------------------------------------
``core.entity.GovernedEntity`` used to carry a field named
``email_bootstrap_floor_at`` that was treated, everywhere it was read,
as "the definitive literal historical-bootstrap date" — a static,
stored literal that happened to look right on the day it was set. That
was wrong, and it was the SECOND time this exact conflation (canonical
accounting-period configuration vs. the historical-ingestion bootstrap
boundary) caused a real bug in this codebase. The real, required
boundary is a DERIVED value: the start of the PREVIOUS COMPLETED
accounting period (one full period back from the entity's own
recurring ``fiscal_year_start_month_day`` rule, evaluated against
"now"), clamped so it never predates a real commencement/incorporation
date when one exists (``GovernedEntity.historical_floor_override_at``,
renamed from ``email_bootstrap_floor_at`` for exactly this reason —
see that field's own docstring). This module is the one place that
derivation happens; nothing else in BAGMAN may compute it independently.

Worked examples (verified by hand against Companies House, "now" =
2026-09-18 — the exact three real canonical entities this delivery
seeds, see ``app/api/composition.py::SEED_ENTITIES``)
------------------------------------------------------------------------
* **Infosecurs Limited** — ``fiscal_year_start_month_day="11-01"``
  (accounts made up to 31 October). Current period:
  2025-11-01 -> 2026-10-31 (today falls inside it). Previous completed
  period start: 2024-11-01. No override needed (incorporated 2021,
  long before any of this matters) -> historical bootstrap =
  2024-11-01T00:00:00Z.
* **NoustAI Limited** — ``fiscal_year_start_month_day="01-01"``,
  incorporated 2025-12-05 (a real, verified Companies House fact).
  Naive previous-completed-period start (from the 01-01 rule alone,
  ignoring incorporation): 2025-01-01 — but the company did not exist
  then. ``historical_floor_override_at=2025-12-05`` acts as a CLAMP:
  final = max(2025-12-05, 2025-01-01) = 2025-12-05T00:00:00Z.
* **Matthew Scott Personal** — ``fiscal_year_start_month_day="04-06"``
  (UK personal tax year). Current period: 2026-04-06 -> 2027-04-05.
  Previous completed period start: 2025-04-06. No override applies to
  a person -> historical bootstrap = 2025-04-06T00:00:00Z.

Feb-29 handling
------------------
A ``fiscal_year_start_month_day`` of ``"02-29"`` landing on a
non-leap target year is handled defensively — clamped to 28 February
of that year rather than raising ``ValueError`` out of ``datetime()``.
None of the three real entities above hit this case, but a future
entity legitimately could (a company whose accounts are genuinely made
up to 29 February), so this module never crashes on it.
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from core.timestamps import ensure_utc

if TYPE_CHECKING:
    from core.entity import GovernedEntity

#: Matches the contract's own `fiscal_year_start_month_day` pattern
#: (`contracts/entity/bagman.entity.v1.schema.json`) — kept in sync
#: deliberately rather than re-deriving it from the schema at runtime.
_MONTH_DAY_PATTERN = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")

#: ``datetime.min`` given real UTC tzinfo — the identity element for
#: the override clamp's own `max()` when no override is set (a naive
#: `datetime.min` cannot be compared against an aware datetime).
_DATETIME_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)


def _parse_month_day(fiscal_year_start_month_day: str) -> tuple[int, int]:
    """Parse a contract-conformant ``"MM-DD"`` string. Raises
    ``ValueError`` for anything that does not match the same pattern
    the contract itself enforces — this module never silently accepts
    a malformed rule it was only handed because an earlier validation
    step should have already rejected it."""
    if not _MONTH_DAY_PATTERN.match(fiscal_year_start_month_day):
        raise ValueError(
            f"'{fiscal_year_start_month_day}' is not a valid fiscal_year_start_month_day "
            "('MM-DD') — cannot derive an accounting-period boundary from it"
        )
    month_str, day_str = fiscal_year_start_month_day.split("-")
    return int(month_str), int(day_str)


def _safe_date(year: int, month: int, day: int) -> datetime:
    """Construct a UTC midnight ``datetime`` for ``(year, month, day)``,
    clamping 29 February down to 28 February on a non-leap ``year``
    (see module docstring's "Feb-29 handling" section) rather than
    letting ``datetime()`` raise."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        day = 28
    return datetime(year, month, day, tzinfo=timezone.utc)


def compute_current_accounting_period_start(fiscal_year_start_month_day: str, *, now: datetime) -> datetime:
    """The start of the accounting period ``now`` currently falls
    within, given a RECURRING ``"MM-DD"`` rule — pure, Feb-29-safe.

    ::

        this_year_candidate = date(now.year, MM, DD)
        current_period_start = this_year_candidate if now >= this_year_candidate
                                else date(now.year - 1, MM, DD)
    """
    ensure_utc(now)
    month, day = _parse_month_day(fiscal_year_start_month_day)
    this_year_candidate = _safe_date(now.year, month, day)
    if now >= this_year_candidate:
        return this_year_candidate
    return _safe_date(now.year - 1, month, day)


def compute_previous_completed_accounting_period_start(fiscal_year_start_month_day: str, *, now: datetime) -> datetime:
    """One full accounting period before
    :func:`compute_current_accounting_period_start` — the NAIVE
    historical-bootstrap boundary before any
    ``historical_floor_override_at`` clamp is applied (see
    :func:`compute_entity_historical_bootstrap`). Pure, Feb-29-safe."""
    current_period_start = compute_current_accounting_period_start(fiscal_year_start_month_day, now=now)
    month, day = _parse_month_day(fiscal_year_start_month_day)
    return _safe_date(current_period_start.year - 1, month, day)


def compute_entity_historical_bootstrap(entity: "GovernedEntity", *, now: datetime) -> datetime:
    """The real, derived historical-ingestion bootstrap boundary for
    ``entity`` — the answer every caller (ultimately
    ``services.mailbox.sweep.compute_bootstrap_floor``) actually wants.
    Never reads a stored literal as-is; always re-derives from
    ``entity.fiscal_year_start_month_day`` + ``now``, then clamps by
    ``entity.historical_floor_override_at`` when that override is set
    (see ``core.entity.GovernedEntity.historical_floor_override_at``'s
    own docstring for the full "clamp, not the answer" doctrine).

    ``max(entity.historical_floor_override_at or datetime.min,
    compute_previous_completed_accounting_period_start(...))`` —
    exactly the architect's own worked formula.

    Raises:
        ValueError: ``entity.fiscal_year_start_month_day`` is ``None``
            — this entity's recurring accounting-period rule is not
            yet configured, so no derivation is possible. The caller
            (``services.mailbox.sweep.compute_bootstrap_floor``) is
            expected to catch this and translate it into
            ``core.errors.ConflictError`` — this module itself never
            invents a fallback and never raises BAGMAN's own canonical
            error vocabulary directly (it has no dependency on
            ``core.errors``, deliberately, to stay a small, dependency-
            light pure-function module).
    """
    if entity.fiscal_year_start_month_day is None:
        raise ValueError(
            f"GovernedEntity '{entity.entity_id}' ('{entity.canonical_name}') has no "
            "fiscal_year_start_month_day configured — cannot derive its historical-ingestion "
            "bootstrap boundary"
        )
    naive_previous_period_start = compute_previous_completed_accounting_period_start(
        entity.fiscal_year_start_month_day, now=now
    )
    override = entity.historical_floor_override_at if entity.historical_floor_override_at is not None else _DATETIME_MIN_UTC
    return max(override, naive_previous_period_start)
