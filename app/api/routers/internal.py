"""``/internal/*`` — thin HTTP wrappers around
``core.api.BagmanCanonicalAPI`` (PID §24, §33, §47-48).

Every handler in this module does exactly three things: (1) validate/
parse the HTTP request into plain Python values, (2) call one
``BagmanCanonicalAPI`` method, (3) render the returned domain object's
own ``to_dict()`` (already contract-shaped) as the JSON response body.
No domain/business rule is implemented here — every canonical
invariant (immutability, idempotency, hash verification, provenance
validity, ...) is enforced by ``core``/``services``/``persistence``,
not by this router.

These are internal-only operations (PID §24): no authentication/
authorization is added here (out of CD-3 scope), and nothing here
implies or introduces external-provider connectivity — see PID §40's
forbidden-provider list, none of which is referenced anywhere in this
module.

Error translation
------------------
Every ``core.errors.BagmanError`` (and any unexpected exception) raised
by a call below propagates out of these handlers uncaught — it is
translated to the correct HTTP status by the centralised exception
handlers registered in ``app/api/main.py``, not by a per-route
``try``/``except`` here. That keeps the status-code mapping in exactly
one place rather than duplicated across every handler.

Direct-upload bypass CLOSED (CD-4 WI-3, PID §26/§27)
--------------------------------------------------------
CD-3 originally exposed a byte-accepting ``POST /internal/evidence``
(``file: UploadFile`` + freeform ``metadata`` JSON) that registered
canonical evidence directly from untrusted uploaded bytes with none of
CD-4's governance (no size/filename/MIME/archive/executable policy, no
malware scanning, no intake identity/state, no quarantine). PID §27 is
explicit that this "must not remain a bypass around governance" once
Evidence Intake exists. That route (and its request model,
``RegisterEvidenceMetadata``) has been REMOVED entirely — not
deprecated, not internally delegated — for a concrete reason beyond
"the PID said so": there is no way to internally delegate it to
``POST /internal/intake/evidence`` (``app/api/routers/intake.py``)
without silently reintroducing exactly the bypass PID §27 forbids,
because the intake endpoint's entire safety property comes from
validating/scanning/staging bytes BEFORE canonical registration, on its
own explicit ``IntakeRecord`` identity/state machine — collapsing that
into a same-request internal call from this route would just be the
same governed endpoint wearing this route's name, with none of this
route's original (already-informally-relied-upon) simpler contract
preserved either. Removing it outright is the only choice that avoids
either outcome. ``tests/acceptance/_lib.py``'s ``register_evidence()``
helper (CD-3's own acceptance-script producer of evidence, since it was
the only one that existed at the time) has been updated to call the new
governed endpoint instead (CD-4 WI-3) — see that module.
``GET /internal/evidence/{evidence_id}``,
``GET /internal/evidence/{evidence_id}/content``, and
``GET /internal/provenance/...`` remain exactly as they were: reads,
never the bypass.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.composition import ensure_seed_entities, get_composition
from app.api.http_headers import safe_content_disposition_header

router = APIRouter(prefix="/internal")

#: PID §45 — no unbounded "return everything" endpoint (mirrors
#: app/api/routers/intake.py's own identical constants for
#: GET /internal/intake).
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------


class RegisterEntityRequest(BaseModel):
    entity_type: str
    canonical_name: str
    display_name: str
    status: str
    actor_type: str
    actor_id: str
    metadata: Optional[dict[str, Any]] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None


class RegisterSourceRequest(BaseModel):
    source_type: str
    provider: str
    status: str
    actor_type: str
    actor_id: str
    external_source_ref: Optional[str] = None
    governed_entity_hint: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None


# ---------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------


@router.post("/entities", status_code=201)
async def register_entity(payload: RegisterEntityRequest) -> dict:
    composition = get_composition()
    entity = composition.api.register_entity(**payload.model_dump())
    return entity.to_dict()


def _xero_status_for(composition, entity_id: str) -> dict[str, Any]:
    """Accounting-connection status for one entity (CD-6 Slice 2, PID
    §98.4, architect spec §1) — a real ``LEFT JOIN``-style enrichment
    over ``services.xero.connection.XeroConnectionRepository``, NEVER a
    second/competing "is this company connected" source of truth (the
    one real ``XeroConnection`` row per entity, or its absence, IS the
    answer). An entity with no ``XeroConnection`` row at all (the
    ordinary case for a brand-new/never-attempted company) renders
    honestly as ``connected: false`` — never a fake/placeholder "fine"
    state (architect spec §9's own doctrine, applied here to the
    company selector itself, one layer up from the account dropdown it
    already applies to).
    """
    connection = composition.xero_connection_repository.get_by_entity(entity_id)
    if connection is None:
        return {"connected": False, "status": None, "tenant_name": None, "last_successful_sync_at": None}
    return {
        "connected": connection.status == "CONNECTED",
        "status": connection.status,
        "tenant_name": connection.tenant_name,
        "last_successful_sync_at": (
            connection.last_successful_sync_at.isoformat() if connection.last_successful_sync_at else None
        ),
    }


@router.get("/entities")
async def list_entities() -> dict[str, Any]:
    """List every canonical ``GovernedEntity`` (CD-6 Slice 1, PID
    §98.3) — the GUI's Company dropdown resolves through THIS list's
    real ``entity_id`` values, never a hardcoded label (see
    ``app/api/composition.py``'s own "Stable canonical entity seed
    lifecycle" section for the idempotent seed this call also
    triggers, lazily, the first time it runs in this process). Small,
    unbounded list deliberately (unlike ``GET /internal/intake``/
    ``GET /internal/evidence``'s own paginated contract) — the set of
    governed entities is operator-curated and expected to remain small
    (PID §98.3 names exactly three today); a future delivery adds
    pagination here if that assumption ever stops holding.

    CD-6 Slice 2 (PID §98.4, architect spec §1) addition: each entity's
    ``to_dict()`` is enriched with an ``xero_connection`` sub-object —
    the real accounting-connection status the company selector needs,
    resolved via a live per-entity lookup against
    ``services.xero.connection.XeroConnectionRepository`` (see
    :func:`_xero_status_for`). A purpose-built second endpoint was
    considered and rejected: this list is already exactly "every
    canonical company, for the selector", and Xero-connection status is
    a property OF a company, not a separate resource — a second
    endpoint would either duplicate this list's own entity iteration or
    force the GUI into two round trips for one screen.
    """
    composition = get_composition()
    ensure_seed_entities(composition)
    entities = composition.api.entity_repository.list_entities()
    items = []
    for e in entities:
        rendered = e.to_dict()
        rendered["xero_connection"] = _xero_status_for(composition, e.entity_id)
        items.append(rendered)
    return {"items": items, "count": len(items)}


@router.post("/sources", status_code=201)
async def register_source(payload: RegisterSourceRequest) -> dict:
    composition = get_composition()
    source = composition.api.register_source(**payload.model_dump())
    return source.to_dict()


@router.get("/evidence")
async def list_evidence(
    entity_id: Optional[str] = None,
    evidence_type: Optional[str] = None,
    received_at_from: Optional[datetime] = None,
    received_at_to: Optional[datetime] = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated ``EvidenceItem`` listing (PID §44-46, CD-4 WI-3 — CD-3
    never added this; only ``get_evidence`` (single) existed). Same
    ``received_at DESC`` + deterministic-tie-breaker ordering doctrine
    as ``GET /internal/intake`` (``app/api/routers/intake.py``)."""
    if limit <= 0 or limit > _MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_SIZE} (got {limit})"
        )
    if offset < 0:
        raise HTTPException(status_code=422, detail=f"offset must be >= 0 (got {offset})")

    composition = get_composition()
    items = composition.api.evidence_repository.list_evidence(
        entity_id=entity_id,
        evidence_type=evidence_type,
        received_at_from=received_at_from,
        received_at_to=received_at_to,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [i.to_dict() for i in items],
        "limit": limit,
        "offset": offset,
        "count": len(items),
    }


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str) -> dict:
    composition = get_composition()
    evidence = composition.api.get_evidence(evidence_id)
    return evidence.to_dict()


@router.get("/evidence/{evidence_id}/content")
async def get_evidence_content(evidence_id: str) -> Response:
    """Retrieve the original stored bytes for ``evidence_id`` (PID §47):
    locate canonical metadata, locate the stored object, retrieve
    bytes, and — via ``EvidenceObjectStore.get()``'s own contract
    (``persistence/objects/store.py``) — re-verify the content hash on
    the way out, raising ``core.errors.IntegrityError`` (translated to
    an HTTP error by ``main.py``) if the stored bytes no longer match
    the hash encoded in the storage reference.

    CD-4 PR #4 Architect delta (2026-09-13): the SERVER, not the GUI's
    client-side ``<a download>`` attribute (``app/api/static/app.js``),
    is now the actual safety boundary against this response being
    rendered/navigated in-page rather than saved. Every response
    carries a safely-encoded ``Content-Disposition: attachment`` header
    (via ``safe_content_disposition_header`` — see
    ``app/api/http_headers.py`` for why the encoding lives there,
    isolated and independently tested) built from
    ``evidence.original_name`` — untrusted, uploader-supplied input —
    plus ``X-Content-Type-Options: nosniff`` so a browser never
    MIME-sniffs the body against ``evidence.mime_type``. The bytes
    served and the existing ``X-Bagman-*`` hash headers are unchanged
    by this delta.
    """
    composition = get_composition()
    evidence = composition.api.get_evidence(evidence_id)
    if not evidence.storage_reference:
        raise HTTPException(
            status_code=404,
            detail=f"EvidenceItem '{evidence_id}' has no storage_reference recorded",
        )

    data = composition.object_store.get(evidence.storage_reference)
    content_hash = dict(evidence.content_hash)

    # Fallback used when no original_name was retained at all (or, per
    # safe_content_disposition_header's own contract, if every
    # character of one were somehow stripped as unsafe): a plain,
    # already-safe, deterministic label derived from evidence_id alone
    # — evidence_id is a BAGMAN-generated identifier, never
    # uploader-controlled, so no further encoding of it is required. No
    # attempt is made to guess a file extension from evidence.mime_type
    # here (e.g. via the stdlib ``mimetypes`` module) — that mapping is
    # inherently ambiguous/platform-dependent (several extensions can
    # map to one MIME type and vice versa) and guessing wrong would be
    # actively misleading; the client already receives the correct
    # ``media_type`` on this same response for that purpose.
    fallback_name = f"evidence-{evidence.evidence_id}"

    return Response(
        content=data,
        media_type=evidence.mime_type,
        headers={
            "X-Bagman-Evidence-Id": evidence.evidence_id,
            "X-Bagman-Content-Hash-Algorithm": content_hash.get("algorithm", ""),
            "X-Bagman-Content-Hash-Value": content_hash.get("value", ""),
            "Content-Disposition": safe_content_disposition_header(
                evidence.original_name,
                fallback=fallback_name,
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/provenance/{subject_type}/{subject_id}")
async def trace_provenance(subject_type: str, subject_id: str) -> list[dict]:
    composition = get_composition()
    lineage = composition.api.trace_provenance(subject_type=subject_type, subject_id=subject_id)
    return [
        {
            "provenance": edge["provenance"].to_dict(),
            "evidence": edge["evidence"].to_dict(),
            "source": edge["source"].to_dict(),
        }
        for edge in lineage
    ]
