"""``/internal/evidence-classification/*`` and
``/internal/evidence/{evidence_id}/classifications/deterministic`` — the
deterministic (non-AI) evidence-classification HTTP surface (CD-6 Slice
5 WI-2).

Thin router, all business logic elsewhere — mirrors
``app/api/routers/mailboxes.py``'s own "thin router, no business logic"
convention exactly: every handler here validates/parses the HTTP
request, calls into
``services.evidence.classification_observation``/
``services.evidence.classification_rule_service``/
``services.evidence.classification_service``, and renders the returned
result. No canonical invariant is enforced here.

Never wired into any automatic path — see
``services.evidence.classification_service``'s own module docstring:
this router (in particular the deterministic-classify endpoint) is an
explicit, manually-invoked surface only.

Endpoints
---------
* ``POST /internal/evidence-classification/rules/preview`` — read-only
  preview (no persistence, no audit event).
* ``POST /internal/evidence-classification/rules`` — governed rule
  creation (observed-evidence guard, audit on genuine creation only).
* ``GET /internal/evidence-classification/rules`` — bounded, filtered
  listing.
* ``GET /internal/evidence-classification/rules/{rule_id}`` — single
  rule detail.
* ``POST /internal/evidence-classification/rules/{rule_id}/retire`` —
  governed, idempotent-safe retirement.
* ``POST /internal/evidence/{evidence_id}/classifications/deterministic``
  — the deterministic classification service, invoked directly against
  one ``EvidenceItem``. See its own docstring below for the full
  outcome -> HTTP status mapping.

``source`` doctrine on rule creation
------------------------------------------------------------------------
``CreateClassificationRuleRequest.source`` only ever accepts
``"OPERATOR"`` — ``services.evidence.classification_rule_service
.create_classification_rule`` itself rejects anything else (including
``"BAGMAN_PROPOSED"``) with a ``ValidationError`` (-> 422); this router
adds no separate check of its own (the service's rejection is
authoritative and already tested directly).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.composition import get_composition
from core.errors import ConflictError
from services.evidence.classification_observation import preview_classification_rule
from services.evidence.classification_rule_service import (
    create_classification_rule,
    retire_classification_rule,
)
from services.evidence.classification_service import (
    OUTCOME_CLASSIFIED,
    OUTCOME_CONFLICT,
    OUTCOME_CURRENT_CLASSIFICATION_EXISTS,
    OUTCOME_EXISTING,
    OUTCOME_NO_APPLICABLE_RULE_INPUT,
    OUTCOME_NO_MATCH,
    classify_evidence_deterministically,
)

router = APIRouter()

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------


class PreviewClassificationRuleRequest(BaseModel):
    sender_scope_type: str  # "EXACT_SENDER_DOMAIN" | "EXACT_SENDER_ADDRESS"
    sender_scope_value: str
    subject_predicate_type: str  # "EXACT" | "STARTS_WITH"
    subject_predicate_value: str
    document_type: str


@router.post("/internal/evidence-classification/rules/preview")
async def preview_rule(payload: PreviewClassificationRuleRequest) -> dict[str, Any]:
    """Read-only — never persists, never emits an audit event, never
    returns document content/raw MIME/attachment bytes (subject/sender
    metadata only)."""
    composition = get_composition()
    result = preview_classification_rule(
        sender_scope_type=payload.sender_scope_type,
        sender_scope_value=payload.sender_scope_value,
        subject_predicate_type=payload.subject_predicate_type,
        subject_predicate_value=payload.subject_predicate_value,
        document_type=payload.document_type,
        evidence_repository=composition.api.evidence_repository,
        classification_repository=composition.classification_repository,
    )
    return {
        "normalized_sender_scope_value": result.normalized_sender_scope_value,
        "normalized_subject_predicate_value": result.normalized_subject_predicate_value,
        "match_count": result.match_count,
        "representative_evidence_ids": list(result.representative_evidence_ids),
        "representative_subjects": list(result.representative_subjects),
        "current_classification_distribution": dict(result.current_classification_distribution),
    }


# ---------------------------------------------------------------------
# Rule create / list / detail / retire
# ---------------------------------------------------------------------


class CreateClassificationRuleRequest(BaseModel):
    sender_scope_type: str
    sender_scope_value: str
    subject_predicate_type: str
    subject_predicate_value: str
    document_type: str
    #: Only "OPERATOR" is ever accepted — see this module's own
    #: docstring, "source doctrine on rule creation".
    source: str
    actor_type: str
    actor_id: str


@router.post("/internal/evidence-classification/rules")
async def create_rule(payload: CreateClassificationRuleRequest) -> JSONResponse:
    composition = get_composition()
    result = create_classification_rule(
        sender_scope_type=payload.sender_scope_type,
        sender_scope_value=payload.sender_scope_value,
        subject_predicate_type=payload.subject_predicate_type,
        subject_predicate_value=payload.subject_predicate_value,
        document_type=payload.document_type,
        source=payload.source,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        rule_repository=composition.classification_rule_repository,
        evidence_repository=composition.api.evidence_repository,
        audit_repository=composition.api.audit_repository,
    )
    body = {
        "evidence_classification_rule": result.rule.to_dict(),
        "match_count": result.match_count,
        "was_created": result.was_created,
    }
    # 201 only for a genuine first creation; an exact-identity replay
    # returns the existing row with 200 (mirrors this codebase's own
    # "a redundant same-state action must never fail, but never claims
    # to have created something new either" doctrine).
    return JSONResponse(status_code=201 if result.was_created else 200, content=body)


@router.get("/internal/evidence-classification/rules")
async def list_rules(
    status: Optional[str] = None,
    sender_scope_type: Optional[str] = None,
    document_type: Optional[str] = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    if limit <= 0 or limit > _MAX_PAGE_SIZE:
        raise HTTPException(status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_SIZE} (got {limit})")
    if offset < 0:
        raise HTTPException(status_code=422, detail=f"offset must be >= 0 (got {offset})")

    composition = get_composition()
    rules = composition.classification_rule_repository.list_rules(
        status=status, sender_scope_type=sender_scope_type, document_type=document_type, limit=limit, offset=offset
    )
    return {
        "items": [r.to_dict() for r in rules],
        "limit": limit,
        "offset": offset,
        "count": len(rules),
    }


@router.get("/internal/evidence-classification/rules/{rule_id}")
async def get_rule(rule_id: str) -> dict[str, Any]:
    composition = get_composition()
    rule = composition.classification_rule_repository.get_rule(rule_id)
    return rule.to_dict()


class RetireClassificationRuleRequest(BaseModel):
    actor_type: str
    actor_id: str


@router.post("/internal/evidence-classification/rules/{rule_id}/retire")
async def retire_rule(rule_id: str, payload: RetireClassificationRuleRequest) -> dict[str, Any]:
    composition = get_composition()
    result = retire_classification_rule(
        rule_id=rule_id,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        rule_repository=composition.classification_rule_repository,
        audit_repository=composition.api.audit_repository,
    )
    return {
        "evidence_classification_rule": result.rule.to_dict(),
        "was_retired_now": result.was_retired_now,
    }


# ---------------------------------------------------------------------
# Deterministic classification
# ---------------------------------------------------------------------


class DeterministicClassifyRequest(BaseModel):
    actor_type: str
    actor_id: str


@router.post("/internal/evidence/{evidence_id}/classifications/deterministic")
async def classify_deterministically(evidence_id: str, payload: DeterministicClassifyRequest) -> JSONResponse:
    """Invokes ONLY ``services.evidence.classification_service
    .classify_evidence_deterministically`` — zero import of ``ai.*``,
    zero LiteLLM/Claude Code call anywhere in this call path. Never
    reachable as a fallback from any other endpoint (see that module's
    own docstring).

    Outcome -> HTTP status mapping (a documented judgment call — see
    the WO's own discussion of this choice):

    * ``CLASSIFIED`` -> 201 (a genuinely new row was created).
    * ``EXISTING`` -> 200 (exact same-rule replay; same row as before).
    * ``NO_MATCH`` / ``NO_APPLICABLE_RULE_INPUT`` -> 200 — these are
      VALID, non-error outcomes (no rule governs this evidence, or the
      evidence has no usable sender/subject metadata at all), never
      forced into an HTTP error status; the response body's own
      ``outcome`` field tells the caller which happened.
    * ``CURRENT_CLASSIFICATION_EXISTS`` -> 409 via ``core.errors
      .ConflictError`` — a DIFFERENT producer already holds the current
      classification; this is a genuine "write conflicts with existing
      canonical state" in exactly the sense ``ConflictError`` already
      names elsewhere in this codebase, so it is mapped the same way
      rather than invented as a bespoke 200-with-body shape.
    * ``CONFLICT`` -> 409 via ``core.errors.ConflictError``, naming
      every tied ``conflicting_rule_ids`` — two or more ACTIVE rules are
      equally authoritative and neither may be silently preferred.
    """
    composition = get_composition()
    result = classify_evidence_deterministically(
        evidence_id=evidence_id,
        evidence_repository=composition.api.evidence_repository,
        rule_repository=composition.classification_rule_repository,
        classification_repository=composition.classification_repository,
        audit_repository=composition.api.audit_repository,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
    )

    if result.outcome == OUTCOME_CURRENT_CLASSIFICATION_EXISTS:
        raise ConflictError(
            f"EvidenceItem '{evidence_id}' already has a current DOCUMENT_TYPE classification "
            f"'{result.existing_classification_id}' (source={result.existing_source!r}, "
            f"document_type={result.existing_document_type!r}) from a different producer — "
            "deterministic classification never supersedes an existing classification"
        )
    if result.outcome == OUTCOME_CONFLICT:
        raise ConflictError(
            f"EvidenceItem '{evidence_id}': {len(result.conflicting_rule_ids)} ACTIVE "
            "EvidenceClassificationRule rows are equally authoritative for this evidence — "
            f"refusing to silently pick one: {list(result.conflicting_rule_ids)}"
        )

    body: dict[str, Any] = {"outcome": result.outcome}
    if result.classification is not None:
        body["evidence_classification"] = result.classification.to_dict()
    if result.matched_rule_id is not None:
        body["matched_rule_id"] = result.matched_rule_id
        body["matching_tier"] = result.matching_tier

    status_code = {
        OUTCOME_CLASSIFIED: 201,
        OUTCOME_EXISTING: 200,
        OUTCOME_NO_MATCH: 200,
        OUTCOME_NO_APPLICABLE_RULE_INPUT: 200,
    }[result.outcome]
    return JSONResponse(status_code=status_code, content=body)
