"""``/internal/activity`` — the cross-BAGMAN activity/audit stream HTTP
API (CD-6 Slice 1, PID §98.8).

A single, thin read endpoint over
``core.audit.AuditRepository.list_recent`` (the general chronological
listing method this delivery adds — see that module's own docstring for
why neither of the two pre-existing query methods,
``list_by_correlation``/``list_by_subject``, can answer "what happened
recently, system-wide"). Deliberately its own file rather than folded
into ``needs_you.py``/``internal.py`` (a judgment call, documented per
the CD-6 dispatch's own "your call" instruction): the Activity view is
conceptually cross-cutting — PID §98.8 gives it to "every tab", not to
Needs You or evidence/intake specifically — so a reader looking for
"where does BAGMAN expose its audit trail" should find exactly one,
obviously-named file, not a route bolted onto a domain-specific router
it does not otherwise belong to.

This endpoint returns raw ``AuditEvent`` records as-is (PID §98.8's own
"clicking an event reveals full forensic detail... without cluttering
the normal GUI" — the GUI itself is responsible for the concise-vs-
drill-down rendering split; the server has no separate "concise" vs.
"forensic" projection to maintain, since every field ``AuditEvent``
already carries — ``correlation_id``/``causation_id``/``payload``/
``actor_*``/timestamps — IS the forensic detail PID §98.8 asks for).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from app.api.composition import get_composition

router = APIRouter(prefix="/internal/activity")

#: Mirrors every other list endpoint's own identical pagination
#: constants in this codebase (PID §98.5/§45 — "no unbounded return
#: everything" doctrine).
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


@router.get("")
async def list_activity(
    limit: int = _DEFAULT_PAGE_SIZE,
    before: Optional[datetime] = None,
    event_type_prefix: Optional[str] = None,
) -> dict[str, Any]:
    """Most-recent-first ``AuditEvent`` listing across every subject
    (PID §98.8's default concise activity stream). ``before`` pages
    backwards (pass the ``occurred_at`` of the oldest item already
    rendered to fetch the next older page — see
    ``AuditRepository.list_recent``'s own docstring for why this is a
    timestamp cursor rather than an offset). ``event_type_prefix`` lets
    a caller scope the stream (e.g. ``NEEDS_YOU_`` for just Needs You
    activity) without a second, bespoke endpoint.
    """
    if limit <= 0 or limit > _MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_SIZE} (got {limit})"
        )

    composition = get_composition()
    events = composition.api.audit_repository.list_recent(
        limit=limit, before=before, event_type_prefix=event_type_prefix
    )
    return {
        "items": [e.to_dict() for e in events],
        "limit": limit,
        "count": len(events),
    }
