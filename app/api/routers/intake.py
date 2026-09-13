"""``/internal/intake/*`` — the governed Evidence Intake HTTP API (CD-4
WI-3, PID §26-33, §44-54).

This is the ONE HTTP boundary through which external/user-originated
bytes may become canonical BAGMAN evidence (PID §27); the CD-3 direct
``POST /internal/evidence`` bypass has been REMOVED entirely — see
``app/api/routers/internal.py``'s module docstring for the full
rationale.

Endpoints
---------
* ``POST /internal/intake/evidence`` — accept an untrusted upload,
  drive it through ``services.evidence.intake.validation_pipeline
  .run_intake_validation``, and — only on ``ACCEPTED`` — register the
  resulting canonical ``EvidenceItem`` (the handoff WI-2's pipeline
  deliberately stops short of; see that module's own docstring).
* ``GET /internal/intake`` / ``GET /internal/intake/{intake_id}`` —
  paginated listing / single-record detail (PID §44-46).

``GET /internal/evidence`` (the evidence-side list endpoint PID §44
also asks for) lives in ``app/api/routers/internal.py`` instead, right
alongside CD-3's existing single-evidence reads
(``GET /internal/evidence/{evidence_id}``,
``GET /internal/evidence/{evidence_id}/content``) — it is evidence-
domain, not intake-domain, and keeps every ``/internal/evidence*``
route in one file.

Idempotent-replay / narrow in-flight-race design (PID §25/§52/§53)
-----------------------------------------------------------------------
``services.evidence.intake.intake.IntakeRepository.create_intake_record``
already durably resolves "same idempotency key" to either the existing
record (identical identifying tuple) or ``IdempotencyConflictError``
(different tuple) — see that module's own docstring for the exact
doctrine. This router must still decide, given whatever record comes
back, whether IT is the one responsible for running the
validation/registration pipeline, or whether that already happened (or
is happening) as part of an earlier request for the same key.

The signal used here is simple and unambiguous: this handler generates
a fresh ``correlation_id`` itself, before calling
``create_intake_record``, and passes it through as the CANDIDATE
correlation_id for a genuinely new record. If the record that comes
back carries that exact value, this request's call is what created it
— a genuinely new intake, full stop. If it carries a DIFFERENT
(pre-existing) correlation_id, this call resolved to an existing row
instead — an idempotent replay — REGARDLESS of what status that row
happens to be in. This sidesteps ever having to infer "new vs. replay"
from status alone, which is ambiguous exactly in the case that matters
most: a replay can itself land on a record still sitting in
``RECEIVED`` (a genuine, if narrow, in-flight race — two requests
racing on the same idempotency key inside one synchronous process).

Given that signal:

* **genuinely new** (or a replay that landed on a still-``RECEIVED``
  row — see below): emit ``INTAKE_RECEIVED`` (new only) ->
  ``INTAKE_VALIDATION_STARTED`` -> run the pipeline -> emit the
  outcome-dependent event(s) -> on ``ACCEPTED``, register evidence and
  emit the rest of the causal chain through ``INTAKE_COMPLETED``.
* **replay landing on a still-``RECEIVED`` row** (nothing else could
  possibly have progressed it further, since ``RECEIVED -> VALIDATING``
  remains a perfectly valid transition even the second time a request
  reaches it): re-attempt validation on it, but WITHOUT re-emitting
  ``INTAKE_RECEIVED`` a second time (that event already exists, written
  by whichever concurrent request actually created the row).

  **CD-4 WI-5 update (PID §54) — the genuine concurrent-race case,
  closed**: this handler does NOT simply assume it is free to enter
  the pipeline just because the row it read back was ``RECEIVED`` —
  another request, racing on the exact same idempotency key, may be
  doing the exact same thing at the exact same moment (both requests'
  own ``create_intake_record`` calls can each independently observe
  the row as still ``RECEIVED``, e.g. one created it and has not yet
  reached ``VALIDATING``, or both are replaying an equally-fresh row).
  This handler therefore itself explicitly claims the
  ``RECEIVED -> VALIDATING`` transition — via its own
  ``composition.intake_repository.transition_status(intake_id,
  "VALIDATING")`` call — BEFORE emitting ``INTAKE_VALIDATION_STARTED``
  and BEFORE calling ``run_intake_validation`` (which is then handed
  the already-``VALIDATING`` record via its ``record=`` parameter, so
  it does not re-attempt that same transition itself). If this
  request's own claim attempt raises ``InvalidStateTransitionError``,
  a concurrent request already won that race a moment earlier — this
  request lost, cleanly, with NOTHING audited (no
  ``INTAKE_VALIDATION_STARTED`` for a validation attempt that never
  actually started) — see the next bullet, whose behaviour this falls
  through to.

  (Prior to this fix, ``run_intake_validation`` performed this same
  claim internally, AFTER this handler had already unconditionally
  emitted ``INTAKE_VALIDATION_STARTED`` — so the losing request's
  ``InvalidStateTransitionError`` propagated out of
  ``run_intake_validation`` entirely uncaught, becoming an unhandled
  HTTP 500 with a spurious audit event already recorded. This was a
  real, reproduced bug — not a theoretical one — found and fixed during
  WI-5's own concurrency proof; see
  ``tests/acceptance/idempotency_and_concurrency_proof.py``'s module
  docstring and part (c.2) for the exact reproduction, and the CD-4
  evidence file for the full writeup.)
* **replay landing on ``VALIDATING``** (a genuinely concurrent request
  is mid-flight validating it RIGHT NOW — either because this handler
  read the row after it was already ``VALIDATING``, or because THIS
  handler's own claim attempt above just lost the race): ``VALIDATING``
  has no self-transition in ``ALLOWED_TRANSITIONS`` (see
  ``services.evidence.intake.intake``), so re-entering the pipeline is
  never attempted for this case. This handler simply returns the
  record's current (still in-flight) state with HTTP 202, emitting no
  new audit events.
* **replay landing on any OTHER state** (``ACCEPTED``, or any terminal
  state — ``QUARANTINED``/``REJECTED``/``REGISTERED``/``FAILED``): the
  pipeline/registration is NOT re-run at all — this resolves straight
  to that existing outcome (PID §25's "same valid key replay must
  resolve to the same IntakeRecord and final result"), with no new
  audit events. This deliberately includes a stale ``ACCEPTED`` record
  from an EARLIER, differently-failed request: automatically
  re-attempting registration from a bare replay request would let two
  concurrent replay requests both race to register the same staged
  bytes. Recovering a genuinely stuck ``ACCEPTED`` intake (PID §50) is
  an explicit operational action, not implicit replay behaviour — out
  of this WI's scope (WI-5).

Partial-failure / recovery story (PID §20/§50, "orphan-object" doctrine)
--------------------------------------------------------------------------
Once a NEW/narrow-replay request reaches ``ACCEPTED`` in THIS request
and begins registration, three things must happen: (1) the staged
bytes are re-read and re-stored under the canonical ``evidence/<id>/
<hash>`` key, (2) ``BagmanCanonicalAPI.register_evidence()`` persists
the metadata row, (3) the ``IntakeRecord`` is transitioned
``ACCEPTED -> REGISTERED``. If step (2) or (3) fails after step (1)
already succeeded, the just-stored canonical object is now an ORPHAN —
exactly the same class of gap ``app/api/routers/internal.py``'s
now-removed direct-upload route already documented and accepted (no
``delete()`` exists on ``EvidenceObjectStore`` by design — PID §17's
immutability doctrine — so no best-effort cleanup is even possible
here). The behaviour here: catch the failure, transition the
``IntakeRecord`` to ``FAILED`` with ``failure_code=
"EVIDENCE_REGISTRATION_FAILED"``, emit ``INTAKE_FAILED``, and
re-raise — a caller must never be told an ``ACCEPTED``-but-not-
``REGISTERED`` intake is a success. The orphaned object itself is safe
and inert (content-addressed; nothing resolves to it without a
canonical ``storage_reference`` pointing there first) and is left in
place, eligible for a future reconciliation pass (PID §50 — not built
here; WI-5/a later delivery's concern).

``EVIDENCE_REGISTERED`` vs. CD-2's own ``EVIDENCE_OBSERVED`` (PID §30)
--------------------------------------------------------------------------
``BagmanCanonicalAPI.register_evidence()`` already emits
``EVIDENCE_OBSERVED`` itself (CD-2) whenever ``external_reference`` is
not itself an idempotent replay — which is always true here, since this
router never supplies one. This handler ALSO emits a distinct
``EVIDENCE_REGISTERED`` event, deliberately: ``EVIDENCE_OBSERVED`` is
CD-2's generic "a canonical EvidenceItem now exists" event and carries
no intake linkage at all; ``EVIDENCE_REGISTERED`` is intake-workflow
vocabulary (PID §26/§30) whose payload explicitly names the
``intake_id`` that produced it, closing the causal loop PID §31
requires ("The resulting EvidenceItem must be traceable back to its
IntakeRecord") without a consumer having to cross-reference two
separate objects. This is judged a deliberate, bounded exception to "do
not flood audit with meaningless noise" (PID §30) — one extra event per
successful intake, carrying genuinely new information, not a duplicate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError as PydanticValidationError

from core import identity
from core.errors import BagmanError, InvalidStateTransitionError
from services.evidence.intake.validation_pipeline import run_intake_validation
from app.api.composition import get_composition, get_manual_upload_source_id

router = APIRouter(prefix="/internal/intake")

#: PID §45 — no unbounded "return everything" endpoint.
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------


class IntakeUploadMetadata(BaseModel):
    """The JSON metadata part of the ``POST /internal/intake/evidence``
    multipart request (PID §28-29/§33). ``evidence_type`` is an
    operator-asserted HINT (PID §29) — not intelligent classification;
    defaults to ``"UNKNOWN"`` when omitted. ``entity_hint`` may be
    ``None``/omitted (explicit UNRESOLVED ownership, PID §10) or an
    operator-supplied free-text label — never a ``GovernedEntity``
    UUID (see this repo's own entity-resolution decision, recorded in
    ``app/api/composition.py``/the delivery report: CD-4 always
    registers evidence with ``entity_id=None``)."""

    entity_hint: Optional[str] = None
    evidence_type: Optional[str] = None
    actor_type: str
    actor_id: str
    note: Optional[str] = None


# ---------------------------------------------------------------------
# POST /internal/intake/evidence
# ---------------------------------------------------------------------


def _emit(
    composition,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    subject_type: str,
    subject_id: str,
    correlation_id: str,
    causation_id: Optional[str],
    payload: Optional[dict] = None,
):
    return composition.api.record_audit_event(
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type=subject_type,
        subject_id=subject_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )


def _register_accepted_evidence(*, composition, record, meta: IntakeUploadMetadata, causation_id: Optional[str]):
    """Handle the ``ACCEPTED -> REGISTERED`` handoff WI-2's pipeline
    deliberately stops short of (see ``validation_pipeline``'s own
    docstring) — read the staged bytes back, re-store them under the
    canonical ``evidence/<id>/<hash>`` key (mirroring exactly the
    pattern the now-removed direct-upload route used), register the
    ``EvidenceItem``, and transition the ``IntakeRecord`` to
    ``REGISTERED``. See this module's own docstring for the
    partial-failure/orphan-object story this function's ``except``
    branch implements.

    Returns ``(registered_intake_record, evidence_item)``.
    """
    try:
        storage_reference = record.metadata["storage_reference"]
        staged_bytes = composition.object_store.get(storage_reference)

        # A fresh id purely to namespace the canonical storage key —
        # see app/api/routers/internal.py's (now-removed route's) own
        # documented distinction between this "storage-key id" and the
        # EvidenceItem.evidence_id register_evidence() mints moments
        # later; the two are never the same value.
        storage_key_id = identity.generate_id()
        canonical_storage_reference = composition.object_store.put(
            storage_key_id, record.content_hash, staged_bytes
        )

        stored_event = _emit(
            composition,
            event_type="EVIDENCE_STORED",
            actor_type=meta.actor_type,
            actor_id=meta.actor_id,
            subject_type="IntakeRecord",
            subject_id=record.intake_id,
            correlation_id=record.correlation_id,
            causation_id=causation_id,
            payload={"storage_reference": canonical_storage_reference},
        )

        evidence = composition.api.register_evidence(
            entity_id=None,  # CD-4 entity-resolution decision — see module docstring
            evidence_type=meta.evidence_type or "UNKNOWN",
            source_id=record.source_id,
            observed_at=record.received_at,
            received_at=record.received_at,
            content_hash=record.content_hash,
            mime_type=record.detected_mime_type,
            size_bytes=record.size_bytes,
            actor_type=meta.actor_type,
            actor_id=meta.actor_id,
            original_name=record.original_filename,
            storage_reference=canonical_storage_reference,
            status="OBSERVED",
            metadata={
                "intake_id": record.intake_id,
                "entity_hint": record.entity_hint,
                "note": meta.note,
            },
            correlation_id=record.correlation_id,
            causation_id=stored_event.audit_event_id,
        )

        # register_evidence() already emitted its own EVIDENCE_OBSERVED
        # (CD-2) threaded to stored_event above — find it so
        # EVIDENCE_REGISTERED's causation points at the true immediately
        # preceding event rather than skipping over it.
        observed_events = [
            e
            for e in composition.api.audit_repository.list_by_subject("EvidenceItem", evidence.evidence_id)
            if e.event_type == "EVIDENCE_OBSERVED"
        ]
        observed_event_id = (
            observed_events[-1].audit_event_id if observed_events else stored_event.audit_event_id
        )

        registered_event = _emit(
            composition,
            event_type="EVIDENCE_REGISTERED",
            actor_type=meta.actor_type,
            actor_id=meta.actor_id,
            subject_type="EvidenceItem",
            subject_id=evidence.evidence_id,
            correlation_id=record.correlation_id,
            causation_id=observed_event_id,
            payload={"intake_id": record.intake_id},
        )

        registered_record = composition.intake_repository.transition_status(
            record.intake_id, "REGISTERED", evidence_id=evidence.evidence_id
        )

        _emit(
            composition,
            event_type="INTAKE_COMPLETED",
            actor_type=meta.actor_type,
            actor_id=meta.actor_id,
            subject_type="IntakeRecord",
            subject_id=record.intake_id,
            correlation_id=record.correlation_id,
            causation_id=registered_event.audit_event_id,
            payload={"evidence_id": evidence.evidence_id},
        )
        return registered_record, evidence
    except BagmanError as exc:
        # Orphan-object doctrine (see module docstring): the intake
        # must never be reported as a success once it cannot honestly
        # complete registration.
        failed_record = composition.intake_repository.transition_status(
            record.intake_id, "FAILED", failure_code="EVIDENCE_REGISTRATION_FAILED"
        )
        _emit(
            composition,
            event_type="INTAKE_FAILED",
            actor_type=meta.actor_type,
            actor_id=meta.actor_id,
            subject_type="IntakeRecord",
            subject_id=record.intake_id,
            correlation_id=record.correlation_id,
            causation_id=causation_id,
            payload={"failure_code": "EVIDENCE_REGISTRATION_FAILED", "detail": str(exc)[:500]},
        )
        raise


def _http_status_for(record, *, just_registered: bool) -> int:
    """PID §68's own "your call; document it; do not return 201 for
    anything that did not actually create canonical evidence" — the
    explicit mapping this router uses:

    * 201 — a NEW canonical EvidenceItem was registered THIS request.
    * 422 — REJECTED (bad content — a client-actionable outcome).
    * 503 — FAILED (an infrastructure failure — retryable).
    * 202 — RECEIVED/VALIDATING (still processing — the narrow
      in-flight-replay cases described in this module's docstring).
    * 200 — everything else honestly reflects a completed, non-error,
      non-newly-created outcome: QUARANTINED, ACCEPTED-but-not-yet-
      registered-this-request, or REGISTERED via a pre-existing replay.
    """
    if just_registered:
        return 201
    if record.status == "REJECTED":
        return 422
    if record.status == "FAILED":
        return 503
    if record.status in ("RECEIVED", "VALIDATING"):
        return 202
    return 200


@router.post("/evidence")
async def intake_evidence(
    file: UploadFile = File(...),
    metadata: str = Form(..., description="JSON-encoded IntakeUploadMetadata"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    try:
        meta = IntakeUploadMetadata.model_validate_json(metadata)
    except PydanticValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid 'metadata' part: {exc}") from exc

    composition = get_composition()
    source_id = get_manual_upload_source_id(composition)

    # A fresh correlation_id, generated BEFORE create_intake_record —
    # see module docstring for why comparing it against what comes back
    # is the signal this handler uses to distinguish "I created this
    # record just now" from "this resolved to a pre-existing replay".
    candidate_correlation_id = identity.generate_id()

    record = composition.intake_repository.create_intake_record(
        source_id=source_id,
        entity_hint=meta.entity_hint,
        original_filename=file.filename,
        reported_mime_type=file.content_type,
        correlation_id=candidate_correlation_id,
        idempotency_key=idempotency_key,
        metadata={
            "actor_type": meta.actor_type,
            "actor_id": meta.actor_id,
            "evidence_type_hint": meta.evidence_type,
            "note": meta.note,
        },
    )
    # core.errors.IdempotencyConflictError, if create_intake_record
    # raises it, propagates uncaught — main.py's centralised handler
    # maps it to HTTP 409 (PID §53).

    is_new = record.correlation_id == candidate_correlation_id
    evidence = None
    just_registered = False

    if is_new or record.status == "RECEIVED":
        last_event_id: Optional[str] = None
        if is_new:
            received_event = _emit(
                composition,
                event_type="INTAKE_RECEIVED",
                actor_type=meta.actor_type,
                actor_id=meta.actor_id,
                subject_type="IntakeRecord",
                subject_id=record.intake_id,
                correlation_id=record.correlation_id,
                causation_id=None,
                payload={
                    "original_filename": record.original_filename,
                    "reported_mime_type": record.reported_mime_type,
                    "entity_hint": record.entity_hint,
                },
            )
            last_event_id = received_event.audit_event_id

        # CD-4 WI-5 (PID §54) — claim the RECEIVED -> VALIDATING
        # transition OURSELVES, explicitly, before emitting
        # INTAKE_VALIDATION_STARTED or calling run_intake_validation.
        # See this module's own docstring ("replay landing on a still-
        # RECEIVED row") for the full race analysis: a genuinely
        # concurrent request racing on the same idempotency key can
        # reach this exact point at the same moment. Losing this claim
        # (InvalidStateTransitionError) means a concurrent request won
        # it a moment earlier — this request falls straight through to
        # the same "replay landing on VALIDATING" handling (the final
        # `else` branch below this whole `if`), with NOTHING audited
        # for this attempt.
        try:
            validating_record = composition.intake_repository.transition_status(
                record.intake_id, "VALIDATING"
            )
        except InvalidStateTransitionError:
            record = composition.intake_repository.get_intake_record(record.intake_id)
        else:
            validation_started_event = _emit(
                composition,
                event_type="INTAKE_VALIDATION_STARTED",
                actor_type=meta.actor_type,
                actor_id=meta.actor_id,
                subject_type="IntakeRecord",
                subject_id=record.intake_id,
                correlation_id=record.correlation_id,
                causation_id=last_event_id,
            )
            last_event_id = validation_started_event.audit_event_id

            result = run_intake_validation(
                intake_id=validating_record.intake_id,
                stream=file.file,
                repository=composition.intake_repository,
                object_store=composition.object_store,
                scanner=composition.scanner,
                record=validating_record,
            )

            if result.status == "REJECTED":
                _emit(
                    composition,
                    event_type="INTAKE_REJECTED",
                    actor_type=meta.actor_type,
                    actor_id=meta.actor_id,
                    subject_type="IntakeRecord",
                    subject_id=record.intake_id,
                    correlation_id=record.correlation_id,
                    causation_id=last_event_id,
                    payload={"failure_code": result.failure_code},
                )
                record = result
            elif result.status == "QUARANTINED":
                _emit(
                    composition,
                    event_type="INTAKE_QUARANTINED",
                    actor_type=meta.actor_type,
                    actor_id=meta.actor_id,
                    subject_type="IntakeRecord",
                    subject_id=record.intake_id,
                    correlation_id=record.correlation_id,
                    causation_id=last_event_id,
                    payload={"quarantine_reason": result.quarantine_reason},
                )
                record = result
            elif result.status == "FAILED":
                _emit(
                    composition,
                    event_type="INTAKE_FAILED",
                    actor_type=meta.actor_type,
                    actor_id=meta.actor_id,
                    subject_type="IntakeRecord",
                    subject_id=record.intake_id,
                    correlation_id=record.correlation_id,
                    causation_id=last_event_id,
                    payload={"failure_code": result.failure_code},
                )
                record = result
            elif result.status == "ACCEPTED":
                accepted_event = _emit(
                    composition,
                    event_type="INTAKE_ACCEPTED",
                    actor_type=meta.actor_type,
                    actor_id=meta.actor_id,
                    subject_type="IntakeRecord",
                    subject_id=record.intake_id,
                    correlation_id=record.correlation_id,
                    causation_id=last_event_id,
                    payload={
                        "detected_mime_type": result.detected_mime_type,
                        "size_bytes": result.size_bytes,
                    },
                )
                record, evidence = _register_accepted_evidence(
                    composition=composition,
                    record=result,
                    meta=meta,
                    causation_id=accepted_event.audit_event_id,
                )
                just_registered = True
            else:  # pragma: no cover - defensive; the pipeline never returns anything else
                record = result
    # else: replay resolving to an already-progressed record
    # (VALIDATING/ACCEPTED/terminal) — see module docstring: nothing is
    # re-run, nothing new is audited; a REGISTERED replay's evidence is
    # fetched just below, alongside a NEW registration's own result.

    if evidence is None and record.status == "REGISTERED" and record.evidence_id:
        evidence = composition.api.get_evidence(record.evidence_id)

    http_status = _http_status_for(record, just_registered=just_registered)
    body = {
        "intake": record.to_dict(),
        "evidence": evidence.to_dict() if evidence is not None else None,
    }
    return JSONResponse(status_code=http_status, content=body)


# ---------------------------------------------------------------------
# GET /internal/intake, GET /internal/intake/{intake_id}
# ---------------------------------------------------------------------


def _validate_pagination(limit: int, offset: int) -> None:
    if limit <= 0 or limit > _MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_SIZE} (got {limit})"
        )
    if offset < 0:
        raise HTTPException(status_code=422, detail=f"offset must be >= 0 (got {offset})")


@router.get("")
async def list_intake(
    entity_hint: Optional[str] = None,
    status: Optional[str] = None,
    received_at_from: Optional[datetime] = None,
    received_at_to: Optional[datetime] = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated ``IntakeRecord`` listing (PID §44-46) — ``received_at
    DESC`` with ``intake_id`` as deterministic tie-breaker, filterable
    by ``entity_hint``/``status``/a ``received_at`` range."""
    _validate_pagination(limit, offset)
    composition = get_composition()
    records = composition.intake_repository.list_intake_records(
        entity_hint=entity_hint,
        status=status,
        received_at_from=received_at_from,
        received_at_to=received_at_to,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [r.to_dict() for r in records],
        "limit": limit,
        "offset": offset,
        "count": len(records),
    }


@router.get("/{intake_id}")
async def get_intake(intake_id: str) -> dict:
    composition = get_composition()
    record = composition.intake_repository.get_intake_record(intake_id)
    return record.to_dict()
