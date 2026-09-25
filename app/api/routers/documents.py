"""``/internal/documents`` and ``/internal/documents/{evidence_id}`` —
the read-only, evidence-first Documents projection HTTP surface (CD-6
Slice 5 WI-5 §5).

Thin router only — see ``services.evidence.document_projection`` for
every real composition/filtering rule. This router validates/parses
the HTTP request, calls the projection service, and renders whatever
it returns; it enforces no canonical invariant of its own and creates
no ``EvidenceClassification``/``EvidenceClassificationRule`` row under
any circumstance (WI-5 §5/§49 — a pure read projection).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter

from app.api.composition import get_composition
from core.timestamps import to_contract_string
from services.evidence.document_projection import (
    DEFAULT_LIMIT,
    get_document_detail,
    list_documents,
)

router = APIRouter()


def _render_row(row: dict) -> dict[str, Any]:
    """Render one projection row to a JSON-safe dict — the only
    transform this router performs is `datetime -> contract string`;
    everything else is already plain JSON-shaped by the service layer."""
    rendered = dict(row)
    if isinstance(rendered.get("received_at"), datetime):
        rendered["received_at"] = to_contract_string(rendered["received_at"])
    return rendered


@router.get("/internal/documents")
async def list_documents_endpoint(
    entity_id: Optional[str] = None,
    document_type: Optional[str] = None,
    classification_status: Optional[str] = None,
    classification_source: Optional[str] = None,
    review_required: Optional[bool] = None,
    received_at_from: Optional[datetime] = None,
    received_at_to: Optional[datetime] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """WI-5 §5/§6/§8/§9 — bounded, evidence-first Documents listing.

    Every value in each row comes from an existing canonical
    authority (see ``services.evidence.document_projection``'s own
    module docstring) — this endpoint owns no business truth of its
    own. ``current_classification: null`` (never a fabricated
    ``UNKNOWN`` row) means no ``EvidenceClassification`` exists yet for
    that document (WI-5 §10 — "Unclassified" is a distinct state from a
    real, governed ``UNKNOWN`` classification).
    """
    composition = get_composition()
    result = list_documents(
        evidence_repository=composition.api.evidence_repository,
        entity_repository=composition.api.entity_repository,
        classification_repository=composition.classification_repository,
        needs_you_repository=composition.needs_you_repository,
        intake_repository=composition.intake_repository,
        entity_id=entity_id,
        document_type=document_type,
        classification_status=classification_status,
        classification_source=classification_source,
        review_required=review_required,
        received_at_from=received_at_from,
        received_at_to=received_at_to,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_render_row(row) for row in result.items],
        "limit": result.limit,
        "offset": result.offset,
        "count": result.count,
        "scan_truncated": result.scan_truncated,
    }


@router.get("/internal/documents/{evidence_id}")
async def get_document_detail_endpoint(evidence_id: str) -> dict[str, Any]:
    """WI-5 §5 — the single-document projection. 404s honestly (via
    ``core.errors.NotFoundError`` -> HTTP 404, ``app/api/main.py``'s
    own existing error mapping) when ``evidence_id`` does not reference
    a real ``EvidenceItem``."""
    composition = get_composition()
    row = get_document_detail(
        evidence_id,
        evidence_repository=composition.api.evidence_repository,
        entity_repository=composition.api.entity_repository,
        classification_repository=composition.classification_repository,
        needs_you_repository=composition.needs_you_repository,
        intake_repository=composition.intake_repository,
    )
    return _render_row(row)
