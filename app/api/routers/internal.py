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

Evidence registration: transaction-semantics decision (PID §20)
------------------------------------------------------------------
``POST /internal/evidence`` must call ``EvidenceObjectStore.put()``
(object storage) and ``BagmanCanonicalAPI.register_evidence()``
(metadata persistence) as two separate systems that cannot be made
atomic with each other. The order chosen here is: **store bytes
first, then register metadata** — because ``put()`` is itself
idempotent/immutable-safe (a retry that lands on the same content
always resolves to the same key), so a partially-completed attempt can
always be safely retried from scratch without risking silent
corruption or overwrite.

This creates exactly one possible partial-failure state, which PID §20
requires this module to state plainly rather than paper over: **if
``put()`` succeeds but the subsequent ``register_evidence()`` call
fails** (e.g. a ``ValidationError``, or a genuine
``DuplicateExternalReferenceError`` if an ``external_reference`` hint
was supplied and conflicts with a different existing canonical
object), the bytes just stored are now an *orphaned* object with no
canonical ``EvidenceItem`` row pointing at it.

The behaviour deliberately chosen for that case: **do nothing further
— the orphaned object is left in place.** No best-effort delete is
attempted, for a concrete reason beyond convenience:
``persistence.objects.store.EvidenceObjectStore`` has no ``delete()``
method in its contract at all (by design — PID §17's immutability
doctrine), so "best-effort delete" is not even an operation available
to this router without altering that protected abstraction, which is
out of this work item's authority. This is safe rather than merely
convenient: the object is content-addressed
(``evidence/<key-id>/<sha256>``) and inert — nothing else in BAGMAN
ever resolves to it without a canonical ``storage_reference`` pointing
there first, so it cannot be mistaken for real evidence, and it is
naturally eligible for a future garbage-collection or manual re-link
pass (a later work item's concern, not this one's). No claim of
cross-system atomicity is made anywhere in this module — PID §20 is
explicit that pretending otherwise would be worse than stating the gap.

Object-key id vs. canonical evidence_id (PID §19)
-----------------------------------------------------
``EvidenceObjectStore.put(evidence_id, content_hash, data)`` needs an
id to build its ``evidence/<evidence_id>/<hash>`` key *before* storing
anything — but ``BagmanCanonicalAPI.register_evidence()`` always mints
its own fresh canonical ``evidence_id`` internally and has no parameter
to accept a caller-supplied one (true of both the in-memory and
PostgreSQL repository implementations; this router does not modify
either). Consequently this handler pre-generates a fresh id via
``core.identity.generate_id()`` purely to namespace the storage key —
call it the *storage-key id* — which will differ from the canonical
``EvidenceItem.evidence_id`` the domain layer mints moments later. The
canonical record's own ``storage_reference`` field is the durable,
always-present link between the two; nothing downstream ever needs to
derive one id from the other by string-parsing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError as PydanticValidationError

from core import identity
from persistence.objects.store import compute_sha256
from app.api.composition import get_composition

router = APIRouter(prefix="/internal")


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


class RegisterEvidenceMetadata(BaseModel):
    """The JSON metadata part of the ``POST /internal/evidence``
    multipart request (the ``file`` part carries the raw bytes
    alongside it)."""

    evidence_type: str
    source_id: str
    observed_at: datetime
    received_at: datetime
    mime_type: str
    actor_type: str
    actor_id: str
    entity_id: Optional[str] = None
    original_name: Optional[str] = None
    status: str = "OBSERVED"
    metadata: Optional[dict[str, Any]] = None
    external_reference_provider: Optional[str] = None
    external_reference_resource_type: Optional[str] = None
    external_reference_external_id: Optional[str] = None
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


@router.post("/sources", status_code=201)
async def register_source(payload: RegisterSourceRequest) -> dict:
    composition = get_composition()
    source = composition.api.register_source(**payload.model_dump())
    return source.to_dict()


@router.post("/evidence", status_code=201)
async def register_evidence(
    metadata: str = Form(..., description="JSON-encoded RegisterEvidenceMetadata"),
    file: UploadFile = File(...),
) -> dict:
    try:
        meta = RegisterEvidenceMetadata.model_validate_json(metadata)
    except PydanticValidationError as exc:
        # A malformed request body, not a canonical BAGMAN domain error
        # — handled directly here (422) rather than via the centralised
        # BagmanError translation in main.py, since no core.errors.*
        # type applies to "the HTTP request itself was malformed".
        raise HTTPException(status_code=422, detail=f"invalid 'metadata' part: {exc}") from exc

    data = await file.read()

    composition = get_composition()

    # See this module's docstring: a fresh id used only to namespace
    # the object-storage key, distinct from the canonical evidence_id
    # register_evidence() will mint below.
    storage_key_id = identity.generate_id()
    content_hash = {"algorithm": "SHA-256", "value": compute_sha256(data)}

    storage_reference = composition.object_store.put(storage_key_id, content_hash, data)

    external_reference = None
    if (
        meta.external_reference_provider
        and meta.external_reference_resource_type
        and meta.external_reference_external_id
    ):
        external_reference = (
            meta.external_reference_provider,
            meta.external_reference_resource_type,
            meta.external_reference_external_id,
        )

    evidence = composition.api.register_evidence(
        entity_id=meta.entity_id,
        evidence_type=meta.evidence_type,
        source_id=meta.source_id,
        observed_at=meta.observed_at,
        received_at=meta.received_at,
        content_hash=content_hash,
        mime_type=meta.mime_type,
        size_bytes=len(data),
        actor_type=meta.actor_type,
        actor_id=meta.actor_id,
        original_name=meta.original_name or file.filename,
        storage_reference=storage_reference,
        status=meta.status,
        metadata=meta.metadata,
        external_reference=external_reference,
        correlation_id=meta.correlation_id,
        causation_id=meta.causation_id,
    )
    return evidence.to_dict()


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

    return Response(
        content=data,
        media_type=evidence.mime_type,
        headers={
            "X-Bagman-Evidence-Id": evidence.evidence_id,
            "X-Bagman-Content-Hash-Algorithm": content_hash.get("algorithm", ""),
            "X-Bagman-Content-Hash-Value": content_hash.get("value", ""),
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
