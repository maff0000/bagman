"""``services.evidence.document_projection`` — the read-only,
evidence-first Documents projection (CD-6 Slice 5 WI-5 §5-10).

This module owns NO canonical truth of its own. It composes existing
canonical authorities only:

* ``EvidenceRepository`` — the base list ("every canonical document
  regardless of source", WI-5 §3).
* ``EntityRepository`` — resolves ``entity_id`` to a real display name.
* ``EvidenceClassificationRepository`` — the current ``DOCUMENT_TYPE``
  classification for each evidence item (never a second classification
  authority).
* ``NeedsYouRepository`` — whether an OPEN ``CLASSIFICATION_REVIEW``
  item exists for the current classification, found via the SAME
  ``(item_type, source_object_reference)`` dedupe key
  ``services.evidence.classification_review.ensure_classification_review_item``
  already uses to create it (``find_by_dedupe_key`` — never a second,
  independent dedupe mechanism).
* ``IntakeRepository`` — supplementary intake-record state (WI-5 §4:
  manual-upload intake information "may remain as supplementary
  state", never the primary authority any more).

No new persistence, no new table, no mutation of anything (WI-5 §5/
§49/§51/§64 — this module never calls ``create_classification``/
``create_classification_rule``/``update_status``/``assign_entity``, and
never imports ``services.xero.*``/``services.mailbox.*``/anything
AI-provider-shaped — see
``tests/integration/test_architecture_boundaries.py``'s WI-5 additions
for the automated proof of every one of these).

Bounded pagination (WI-5 §8) and bounded scan cost when a
classification-DERIVED filter is supplied (WI-5 §9)
------------------------------------------------------------------------
When none of ``document_type``/``classification_status``/
``classification_source``/``review_required`` is supplied, this module
answers directly off ``EvidenceRepository.list_evidence``'s own
``limit``/``offset`` — genuinely O(page size) work, newest-``received_at``
first with ``evidence_id`` as a deterministic tie-breaker (the SAME
ordering ``EvidenceRepository.list_evidence`` and
``IntakeRepository.list_intake_records`` already use).

Those four filters are all DERIVED state — not a column
``EvidenceRepository`` can filter on directly (the current
classification for one evidence item is itself a separate query) — so
honouring them means walking evidence newest-first, computing each
item's current classification, and keeping only the matches, until
either the requested page is filled or an internal scan bound
(:data:`_MAX_SCAN`) is reached. This is a genuine, bounded engineering
trade-off (recorded here, and in the WI-5 report, as a documented
judgment call): it is NOT the same as "return all 689 documents" (WI-5
§8's own explicit prohibition) — the response itself is always bounded
to ``limit`` rows — but a request combining one of these four filters
with a large ``offset`` against a corpus with very few matching rows
can do more internal work than an unfiltered page. ``DocumentListResult
.scan_truncated`` reports honestly whenever the bound was hit before
every candidate could be examined, rather than silently returning a
possibly-incomplete page with no signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from core.errors import NotFoundError, ValidationError
from core.text_matching import domain_from_address
from services.evidence.classification import (
    CLASSIFICATION_STATUSES,
    CLASSIFICATION_SOURCES,
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPES,
)
from services.needs_you.needs_you import ITEM_TYPE_CLASSIFICATION_REVIEW

#: WI-5 §8 — "default approximately 25. Maximum 200."
DEFAULT_LIMIT = 25
MAX_LIMIT = 200

#: WI-5 §10 — the pseudo `classification_status` value a caller passes
#: to mean "no EvidenceClassification row exists at all" (a real,
#: filterable state distinct from every real
#: `services.evidence.classification.CLASSIFICATION_STATUSES` value,
#: and never confused with the real `UNKNOWN` `document_type` —
#: "Unclassified" and "UNKNOWN" are different states, WI-5 §10). Never
#: persisted anywhere; a projection-layer-only vocabulary member.
CLASSIFICATION_STATUS_UNCLASSIFIED = "UNCLASSIFIED"

#: See module docstring's "Bounded pagination... scan cost" section.
#: Bounds worst-case per-request work for the four derived filters —
#: never "scan the whole evidence corpus unconditionally" (the
#: unfiltered path above never touches this constant at all).
_MAX_SCAN = 2000
_SCAN_BATCH = 200


def _validate_pagination(limit: int, offset: int) -> None:
    if limit is None or limit <= 0 or limit > MAX_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_LIMIT} (got {limit!r})")
    if offset is None or offset < 0:
        raise ValidationError(f"offset must be >= 0 (got {offset!r})")


def _validate_filters(
    *, document_type: Optional[str], classification_status: Optional[str], classification_source: Optional[str],
) -> None:
    if document_type is not None and document_type not in DOCUMENT_TYPES:
        raise ValidationError(f"document_type must be one of {sorted(DOCUMENT_TYPES)} (got {document_type!r})")
    if classification_status is not None and (
        classification_status not in CLASSIFICATION_STATUSES
        and classification_status != CLASSIFICATION_STATUS_UNCLASSIFIED
    ):
        allowed = sorted(CLASSIFICATION_STATUSES) + [CLASSIFICATION_STATUS_UNCLASSIFIED]
        raise ValidationError(f"classification_status must be one of {allowed} (got {classification_status!r})")
    if classification_source is not None and classification_source not in CLASSIFICATION_SOURCES:
        raise ValidationError(
            f"classification_source must be one of {sorted(CLASSIFICATION_SOURCES)} (got {classification_source!r})"
        )


def _entity_view(entity_repository, entity_id: Optional[str]) -> Optional[dict]:
    if not entity_id:
        return None
    try:
        entity = entity_repository.get_entity(entity_id)
    except NotFoundError:
        return None
    return {
        "entity_id": entity.entity_id,
        "canonical_name": entity.canonical_name,
        "display_name": entity.display_name,
    }


def _document_label(evidence) -> str:
    """WI-5 §11 — the preferred human-facing document label: (1) email
    subject when present, (2) original filename, (3) evidence_id
    fallback. Never a raw UUID as the PRIMARY label."""
    subject = (evidence.metadata or {}).get("subject")
    if subject:
        return str(subject)
    if evidence.original_name:
        return evidence.original_name
    return evidence.evidence_id


def _sender_domain(sender_address: Optional[str]) -> Optional[str]:
    if not sender_address:
        return None
    try:
        return domain_from_address(sender_address)
    except ValidationError:
        return None


def _classification_view(classification) -> Optional[dict]:
    """WI-5 §6/§10 — `None` (never a fabricated row) when no
    EvidenceClassification exists yet for this evidence item — the
    caller (HTTP layer / GUI) renders that as "Unclassified", distinct
    from a real, governed `UNKNOWN` classification (WI-5 §10)."""
    if classification is None:
        return None
    return {
        "classification_id": classification.classification_id,
        "classification_type": classification.classification_type,
        "document_type": classification.document_type,
        "status": classification.status,
        "source": classification.source,
        "confidence": classification.confidence,
        "rule_id": classification.rule_id,
        "ai_invocation_id": classification.ai_invocation_id,
        "operator_action_id": classification.operator_action_id,
    }


def _classification_review_view(classification, needs_you_repository) -> Optional[dict]:
    """WI-5 §6 — the CLASSIFICATION_REVIEW NeedsYouItem for the CURRENT
    classification, if one exists (open or already resolved) — found
    via the exact same `(item_type, source_object_reference)` dedupe
    key `services.evidence.classification_review
    .ensure_classification_review_item` uses to create it
    (`find_by_dedupe_key` — never a second, independent index)."""
    if classification is None:
        return None
    item = needs_you_repository.find_by_dedupe_key(
        ITEM_TYPE_CLASSIFICATION_REVIEW, classification.classification_id
    )
    if item is None:
        return None
    return {"item_id": item.item_id, "status": item.status}


def _build_intake_index(intake_repository) -> Mapping[str, Any]:
    """WI-5 §4/§6 — supplementary intake-record state, keyed by
    `evidence_id`. `IntakeRepository` has no `evidence_id` filter/lookup
    of its own (it is indexed by `intake_id`), so this reads the whole
    intake corpus ONCE per request (a separate, typically much smaller
    collection than the full evidence corpus — every intake record is
    itself one MANUAL_UPLOAD-sourced EvidenceItem candidate, never one
    per email) and builds an in-process index — never a per-row fetch
    (see module docstring's "no O(689) per request" doctrine)."""
    index: dict[str, Any] = {}
    for record in intake_repository.list_intake_records(limit=None):
        if record.evidence_id:
            index[record.evidence_id] = record
    return index


def _intake_view(evidence_id: str, intake_index: Mapping[str, Any]) -> Optional[dict]:
    record = intake_index.get(evidence_id)
    if record is None:
        return None
    return {"intake_id": record.intake_id, "status": record.status}


def _row_view(
    evidence,
    *,
    entity_repository,
    classification_repository,
    needs_you_repository,
    intake_index: Mapping[str, Any],
) -> dict:
    classification = classification_repository.get_current_classification(
        evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
    )
    metadata = evidence.metadata or {}
    sender_address = metadata.get("sender_address")
    subject = metadata.get("subject")
    return {
        "evidence_id": evidence.evidence_id,
        "entity_id": evidence.entity_id,
        "entity": _entity_view(entity_repository, evidence.entity_id),
        "document_label": _document_label(evidence),
        "received_at": evidence.received_at,
        "original_name": evidence.original_name,
        "mime_type": evidence.mime_type,
        "size_bytes": evidence.size_bytes,
        "evidence_type": evidence.evidence_type,
        "sender_address": sender_address,
        "sender_domain": _sender_domain(sender_address),
        "subject": subject,
        "content_hash": dict(evidence.content_hash) if evidence.content_hash else None,
        "current_classification": _classification_view(classification),
        "classification_review": _classification_review_view(classification, needs_you_repository),
        "intake": _intake_view(evidence.evidence_id, intake_index),
    }


def _matches_derived_filters(
    classification,
    *,
    document_type: Optional[str],
    classification_status: Optional[str],
    classification_source: Optional[str],
    review_required: Optional[bool],
    needs_you_repository,
) -> bool:
    if classification is None:
        # Only the explicit UNCLASSIFIED pseudo-status, and a
        # review_required=False (an unclassified document never has an
        # open review item), can ever match "no classification".
        if document_type is not None or classification_source is not None:
            return False
        if classification_status is not None and classification_status != CLASSIFICATION_STATUS_UNCLASSIFIED:
            return False
        if review_required is True:
            return False
        return True

    if classification_status == CLASSIFICATION_STATUS_UNCLASSIFIED:
        return False
    if document_type is not None and classification.document_type != document_type:
        return False
    if classification_status is not None and classification.status != classification_status:
        return False
    if classification_source is not None and classification.source != classification_source:
        return False
    if review_required is not None:
        review = _classification_review_view(classification, needs_you_repository)
        is_open_review = bool(review and review["status"] == "OPEN")
        if review_required != is_open_review:
            return False
    return True


@dataclass(frozen=True)
class DocumentListResult:
    items: tuple[dict, ...]
    limit: int
    offset: int
    count: int
    #: WI-5 §8/§9 — True only when the internal bounded scan
    #: (`_MAX_SCAN`) was exhausted before every candidate evidence item
    #: in range could be examined for a classification-derived filter —
    #: an honest "this page may be incomplete, narrow your filters or
    #: page further" signal, never silently hidden.
    scan_truncated: bool


def list_documents(
    *,
    evidence_repository,
    entity_repository,
    classification_repository,
    needs_you_repository,
    intake_repository,
    entity_id: Optional[str] = None,
    document_type: Optional[str] = None,
    classification_status: Optional[str] = None,
    classification_source: Optional[str] = None,
    review_required: Optional[bool] = None,
    received_at_from: Optional[datetime] = None,
    received_at_to: Optional[datetime] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> DocumentListResult:
    """WI-5 §5/§6/§8/§9 — the bounded, evidence-first Documents list
    projection. Stable newest-``received_at``-first ordering with
    ``evidence_id`` as a deterministic tie-breaker (the ordering
    ``EvidenceRepository.list_evidence`` itself already guarantees; this
    function never re-sorts).

    Raises:
        core.errors.ValidationError: invalid ``limit``/``offset``, or a
            ``document_type``/``classification_status``/
            ``classification_source`` outside its own closed vocabulary
            (see :func:`_validate_filters`).
    """
    _validate_pagination(limit, offset)
    _validate_filters(
        document_type=document_type, classification_status=classification_status,
        classification_source=classification_source,
    )

    intake_index = _build_intake_index(intake_repository)

    def _view(evidence) -> dict:
        return _row_view(
            evidence, entity_repository=entity_repository, classification_repository=classification_repository,
            needs_you_repository=needs_you_repository, intake_index=intake_index,
        )

    needs_derived_filter = not (
        document_type is None and classification_status is None
        and classification_source is None and review_required is None
    )

    if not needs_derived_filter:
        page = evidence_repository.list_evidence(
            entity_id=entity_id, received_at_from=received_at_from, received_at_to=received_at_to,
            limit=limit, offset=offset,
        )
        items = tuple(_view(evidence) for evidence in page)
        return DocumentListResult(items=items, limit=limit, offset=offset, count=len(items), scan_truncated=False)

    # ---- derived-filter path — bounded scan (see module docstring) ----
    matches: list[Any] = []
    scanned = 0
    batch_offset = 0
    scan_truncated = False
    target = offset + limit
    while len(matches) < target and scanned < _MAX_SCAN:
        batch = evidence_repository.list_evidence(
            entity_id=entity_id, received_at_from=received_at_from, received_at_to=received_at_to,
            limit=_SCAN_BATCH, offset=batch_offset,
        )
        if not batch:
            break
        for evidence in batch:
            scanned += 1
            classification = classification_repository.get_current_classification(
                evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
            )
            if _matches_derived_filters(
                classification, document_type=document_type, classification_status=classification_status,
                classification_source=classification_source, review_required=review_required,
                needs_you_repository=needs_you_repository,
            ):
                matches.append(evidence)
                if len(matches) >= target:
                    break
            if scanned >= _MAX_SCAN:
                break
        batch_offset += _SCAN_BATCH
        if len(batch) < _SCAN_BATCH:
            break
    if scanned >= _MAX_SCAN and len(matches) < target:
        scan_truncated = True

    page_evidence = matches[offset:target]
    items = tuple(_view(evidence) for evidence in page_evidence)
    return DocumentListResult(items=items, limit=limit, offset=offset, count=len(items), scan_truncated=scan_truncated)


def get_document_detail(
    evidence_id: str,
    *,
    evidence_repository,
    entity_repository,
    classification_repository,
    needs_you_repository,
    intake_repository,
) -> dict:
    """WI-5 §5 — the single-document projection.

    Raises:
        core.errors.NotFoundError: no such ``evidence_id``.
    """
    evidence = evidence_repository.get_evidence(evidence_id)
    intake_index = _build_intake_index(intake_repository)
    return _row_view(
        evidence, entity_repository=entity_repository, classification_repository=classification_repository,
        needs_you_repository=needs_you_repository, intake_index=intake_index,
    )
