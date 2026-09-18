"""``/internal/needs-you/*`` — the universal Needs You queue HTTP API
(CD-6 Slice 1, PID §98.2/§98.5).

Thin wrapper, no business logic (same "thin router" convention
``app/api/routers/internal.py``'s own module docstring establishes):
every handler here validates/parses the HTTP request, calls one
``services.needs_you.needs_you.NeedsYouRepository`` method (plus, for
resolution, one ``core.api.BagmanCanonicalAPI.record_audit_event`` call
threaded onto the item's own ``correlation_id`` — mirroring exactly how
``app/api/routers/intake.py`` threads its own audit chain), and renders
the returned domain object's ``to_dict()``.

Endpoints
---------
* ``GET /internal/needs-you`` — paginated listing, OPEN items first
  (see ``NeedsYouRepository.list_needs_you_items``'s own docstring for
  the exact ordering), filterable by ``status``/``item_type``/``domain``.
* ``GET /internal/needs-you/{item_id}`` — single item detail.
* ``POST /internal/needs-you/{item_id}/resolve`` — submit the answer/
  resolution (PID §98.3's Company/What/Why review interaction, for
  Slice 1's one real item type). See ``_resolve_needs_you_item``'s own
  docstring for the exact idempotent-double-submit contract this
  endpoint honours.

Where the ONE real Slice 1 trigger lives
-------------------------------------------
This router never itself creates a ``NeedsYouItem`` — Slice 1's only
producer is ``app/api/routers/intake.py``'s own documented hook (fired
the moment an intake attempt reaches ``ACCEPTED -> REGISTERED``), kept
there rather than here so the "one item per accepted intake, including
under idempotent replay" logic lives right next to the exact state
transition it depends on, instead of this router polling/re-deriving
it from a separate vantage point.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.errors import ConflictError
from app.api.composition import get_composition
from services.needs_you.needs_you import ITEM_TYPE_COMPANY_REQUIRED

router = APIRouter(prefix="/internal/needs-you")

#: Mirrors app/api/routers/intake.py's/internal.py's own identical
#: pagination constants (PID §98.5 — "no unbounded return everything"
#: doctrine applies here exactly as it does to every other list
#: endpoint in this codebase).
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


class ResolveNeedsYouItemRequest(BaseModel):
    """Request body for ``POST /internal/needs-you/{item_id}/resolve``.

    ``new_status`` defaults to ``"RESOLVED"`` (the ordinary "operator
    answered it" path) — a caller passes ``"DISMISSED"`` for the
    explicit "not applicable, decline to answer" path PID §98.5's own
    state-machine doctrine reserves as the other terminal outcome.
    ``resolution`` is free-form (validated only as "must be a non-empty
    object" here; PID §98.4's Xero-chart-of-accounts note in the GUI is
    a UX-layer decision, not a server-side schema this slice enforces —
    see the module docstring's own "no permanent accounting taxonomy"
    judgment call, recorded in ``services.needs_you.needs_you``) — for
    Slice 1's own ``COMPANY_WHAT_WHY`` action type, the GUI always
    submits ``{"entity_id": ..., "what": ..., "why": ...}``.
    """

    new_status: str = "RESOLVED"
    resolution: Optional[dict[str, Any]] = None
    actor_type: str
    actor_id: str


@router.get("")
async def list_needs_you_items(
    status: Optional[str] = None,
    item_type: Optional[str] = None,
    domain: Optional[str] = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    if limit <= 0 or limit > _MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_SIZE} (got {limit})"
        )
    if offset < 0:
        raise HTTPException(status_code=422, detail=f"offset must be >= 0 (got {offset})")

    composition = get_composition()
    items = composition.needs_you_repository.list_needs_you_items(
        status=status, item_type=item_type, domain=domain, limit=limit, offset=offset
    )
    open_count = len(
        composition.needs_you_repository.list_needs_you_items(status="OPEN", limit=_MAX_PAGE_SIZE)
    )
    return {
        "items": [i.to_dict() for i in items],
        "limit": limit,
        "offset": offset,
        "count": len(items),
        # Overview's own "N things need your attention" summary (PID
        # §98.2) needs the TOTAL open count, not merely this page's —
        # returned alongside every listing (regardless of the caller's
        # own status filter) so the GUI never needs a second round trip
        # just to render the greeting.
        "open_count": open_count,
    }


@router.get("/{item_id}")
async def get_needs_you_item(item_id: str) -> dict:
    composition = get_composition()
    item = composition.needs_you_repository.get_needs_you_item(item_id)
    return item.to_dict()


@router.post("/{item_id}/resolve")
async def resolve_needs_you_item(item_id: str, payload: ResolveNeedsYouItemRequest) -> dict:
    """Resolve (or dismiss) one Needs You item.

    Idempotent-safe against a genuine double-submit (PID §98.5's own
    "resolution... must persist across restart" acceptance requirement
    implies the more basic "a network retry of the same resolve must
    not itself error" property): if the item is ALREADY terminal
    (``RESOLVED``/``DISMISSED``) and the incoming request is byte-for-
    byte the same outcome (``new_status`` + ``resolution`` dict both
    equal) an earlier call already recorded, this returns that existing
    record instead of raising — a plain double-click/retry of the exact
    same submit succeeds quietly rather than surfacing a confusing
    409 to an operator who already saw success once. A genuine attempt
    to change an ALREADY-decided item to a DIFFERENT outcome (a real
    conflict, not a retry) still raises ``ConflictError`` -> HTTP 409.

    CD-6 GUI-operations-foundation follow-on WO (item E) — a narrow,
    ``COMPANY_REQUIRED``-specific branch lives directly in THIS generic
    handler (this router, ``app/api/routers/needs_you.py``, is the HTTP
    orchestration layer — it already legitimately depends on
    ``core.api``/``services.evidence`` exactly like ``app/api/routers
    /intake.py`` does; the pure domain module
    ``services/needs_you/needs_you.py`` is untouched and gains no such
    dependency). This endpoint is genuinely shared by BOTH the
    pre-existing Slice-1 manual-upload flow and the mailbox document-
    destination-review flow (both raise ``ITEM_TYPE_COMPANY_REQUIRED``/
    ``ALLOWED_ACTION_COMPANY_WHAT_WHY`` items) — for EITHER producer,
    resolving such an item to ``RESOLVED`` (never ``DISMISSED`` — a
    dismissal explicitly does not assign an entity) now actually assigns
    the referenced ``EvidenceItem``'s ``entity_id`` via
    ``core.api.BagmanCanonicalAPI.assign_evidence_entity`` — a confirmed,
    real, previously-unwired gap (that method already existed, with zero
    real callers anywhere) — BEFORE this item's own ``RESOLVED`` status
    is persisted, so a failed assignment can never leave the operator's
    question marked resolved while the evidence remains unassigned. A
    retry of the SAME resolve call after a partial failure (entity
    assigned, but the Needs You resolve-write itself then failed) safely
    completes on retry via ``assign_evidence_entity``'s own idempotent
    same-entity no-op.
    """
    composition = get_composition()
    current = composition.needs_you_repository.get_needs_you_item(item_id)

    if current.status != "OPEN":
        same_outcome = current.status == payload.new_status and (current.resolution or {}) == (
            payload.resolution or {}
        )
        if same_outcome:
            return current.to_dict()
        raise ConflictError(
            f"NeedsYouItem '{item_id}' is already '{current.status}' with a different "
            "resolution — refusing to silently change an already-decided item "
            "(PID §98.5); this is a genuine conflict, not an idempotent retry"
        )

    if current.item_type == ITEM_TYPE_COMPANY_REQUIRED and payload.new_status == "RESOLVED":
        resolution = payload.resolution or {}
        entity_id = resolution.get("entity_id")
        # A real entity_id is the ordinary, expected shape for a
        # COMPANY_WHAT_WHY resolution (Company is the whole point of
        # this item type) — but `resolution.entity_id` remains a caller-
        # supplied, optional field at the contract layer (unchanged by
        # this WO — no request/response shape change), so an omitted/
        # `None` entity_id is simply "nothing to assign yet", never a
        # hard validation failure here: this item still resolves exactly
        # as it always has. Only a REAL, truthy entity_id triggers the
        # new canonical assignment wiring below.
        # `source_object_reference` is likewise a genuine sanity/
        # consistency check, never a new hard requirement this branch
        # invents: a COMPANY_REQUIRED item this WO's own real producers
        # (app/api/routers/intake.py, services/mailbox/sweep.py) raise
        # ALWAYS anchors to a real evidence_id — but a caller-constructed
        # item with no such anchor (not a real evidence question at all)
        # simply has nothing for this branch to assign, exactly like the
        # missing-entity_id case above; never a validation failure that
        # would otherwise block an unrelated resolution.
        evidence_id = current.source_object_reference
        if entity_id and evidence_id:
            # Real, canonical entity assignment FIRST — before this
            # item's own RESOLVED status is persisted below (architect's
            # own explicit ordering requirement; see
            # `assign_evidence_entity`'s own docstring for the full
            # idempotent/conflict contract this enforces).
            composition.api.assign_evidence_entity(
                evidence_id=evidence_id,
                entity_id=entity_id,
                actor_type=payload.actor_type,
                actor_id=payload.actor_id,
                correlation_id=current.correlation_id,
            )

    updated = composition.needs_you_repository.resolve_needs_you_item(
        item_id,
        new_status=payload.new_status,
        resolution=payload.resolution,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
    )

    composition.api.record_audit_event(
        event_type="NEEDS_YOU_ITEM_RESOLVED" if updated.status == "RESOLVED" else "NEEDS_YOU_ITEM_DISMISSED",
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        subject_type="NeedsYouItem",
        subject_id=updated.item_id,
        correlation_id=updated.correlation_id,
        causation_id=None,
        payload={
            "item_type": updated.item_type,
            "source_object_reference": updated.source_object_reference,
            "resolution": dict(updated.resolution) if updated.resolution is not None else None,
        },
    )

    return updated.to_dict()
