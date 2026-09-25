"""The shared "has this candidate rule identity ever matched a REAL,
persisted EvidenceItem" computation (CD-6 Slice 5 WI-2) — backs BOTH
the creation-time observed-evidence guard (:func:`observed_evidence_guard`)
and the read-only preview service (:func:`preview_classification_rule`).
Neither reimplements the other's matching logic; both call
:func:`_observed_matches` below, which itself reuses
``services.evidence.classification_matcher.match_evidence_to_rule`` —
the SAME matcher the deterministic classification service (WI-2's own
``classify_evidence_deterministically``) uses for a real message. This
is deliberate: normalisation/matching must be identical across all
three call sites (guard, preview, live classification), never three
independent near-copies that could silently drift apart — see this
module's own test suite for a literal proof all three code paths agree
on one hand-picked normalisation edge case.

Bounded, never a full scan
------------------------------------------------------------------------
``services.evidence.evidence.EvidenceRepository.find_candidate_evidence_for_sender``
is a NARROWING SQL query, bounded to ``candidate_limit`` rows
(default 200) — never "every EvidenceItem ever observed". Consequently
``match_count``/the guard's return value are correct WITHIN that
bounded candidate window, not a full-corpus count: a sender with more
than ``candidate_limit`` historical messages could have MORE real
matches than this reports. This is judged acceptable for a
CREATION-TIME "has this ever been observed at least once" guard (only
``>= 1`` matters, and the query is ordered most-recent-first, so a
genuinely-never-observed identity will correctly show zero regardless
of corpus size) and for an operator-facing PREVIEW (an operator
does not need every historical match enumerated to understand what a
rule would do) — flagged here explicitly rather than silently
presented as an exhaustive count.

Never reads document content, raw MIME, or attachment bytes
------------------------------------------------------------------------
Every candidate here is inspected via its OWN ``EvidenceItem.metadata``
(``sender_address``/``subject`` — already stored, plain, short strings)
only. Nothing in this module ever calls into an object store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from core.errors import ValidationError
from core.text_matching import domain_from_address, normalize_subject_for_policy
from core.timestamps import utc_now
from services.evidence.classification import CLASSIFICATION_TYPE_DOCUMENT_TYPE, DOCUMENT_TYPE_UNKNOWN, DOCUMENT_TYPES
from services.evidence.classification_matcher import OUTCOME_MATCH, match_evidence_to_rule
from services.evidence.classification_rule import (
    RULE_SOURCE_OPERATOR,
    RULE_STATUS_ACTIVE,
    SENDER_SCOPE_EXACT_SENDER_ADDRESS,
    SENDER_SCOPE_TYPES,
    SUBJECT_PREDICATE_TYPES,
    EvidenceClassificationRule,
    normalize_sender_scope_value,
)

#: Default bound both `observed_evidence_guard`/`preview_classification_rule`
#: use for the underlying candidate query — see module docstring.
_DEFAULT_CANDIDATE_LIMIT = 200
#: How many representative evidence_id/subject pairs
#: `preview_classification_rule` returns — bounded, never every match.
_DEFAULT_REPRESENTATIVE_LIMIT = 10

#: The distribution-dict key `preview_classification_rule` uses for a
#: matched evidence item that currently has NO DOCUMENT_TYPE
#: classification at all.
_NO_CURRENT_CLASSIFICATION_KEY = "NONE"


def _synthetic_candidate_rule(
    *, sender_scope_type: str, normalized_sender_scope_value: str,
    subject_predicate_type: str, normalized_subject_predicate_value: str,
) -> EvidenceClassificationRule:
    """Build an EvidenceClassificationRule-shaped object representing the
    CANDIDATE identity being proposed/previewed — never persisted, never
    passed to any repository. `document_type` is a neutral placeholder
    (`DOCUMENT_TYPE_UNKNOWN`) — irrelevant to whether
    `match_evidence_to_rule` reports MATCH, which is the only thing this
    module ever inspects on the result."""
    now = utc_now()
    return EvidenceClassificationRule(
        rule_id="__classification_observation_synthetic_candidate__",
        sender_scope_type=sender_scope_type,
        sender_scope_value=normalized_sender_scope_value,
        subject_predicate_type=subject_predicate_type,
        subject_predicate_value=normalized_subject_predicate_value,
        document_type=DOCUMENT_TYPE_UNKNOWN,
        status=RULE_STATUS_ACTIVE,
        source=RULE_SOURCE_OPERATOR,
        created_at=now,
        approved_at=now,
    )


def _validate_identity_fields(
    *, sender_scope_type: str, subject_predicate_type: str, normalized_subject_predicate_value: str,
) -> None:
    if sender_scope_type not in SENDER_SCOPE_TYPES:
        raise ValidationError(f"'{sender_scope_type}' is not one of {sorted(SENDER_SCOPE_TYPES)}")
    if subject_predicate_type not in SUBJECT_PREDICATE_TYPES:
        raise ValidationError(f"'{subject_predicate_type}' is not one of {sorted(SUBJECT_PREDICATE_TYPES)}")
    if not normalized_subject_predicate_value:
        raise ValidationError("subject_predicate_value must normalise to a non-empty string")


def _observed_matches(
    *,
    sender_scope_type: str,
    sender_scope_value: str,
    subject_predicate_type: str,
    subject_predicate_value: str,
    evidence_repository,
    candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT,
) -> list:
    """The one shared core: normalise the candidate identity, fetch a
    BOUNDED set of plausible candidates from `evidence_repository`
    (never trusting that SQL-level narrowing as final truth), and
    re-verify EACH one through the real matcher
    (`services.evidence.classification_matcher.match_evidence_to_rule`)
    against a single synthetic candidate rule. Returns the list of
    `EvidenceItem`s that genuinely matched, most-recent-first (the same
    order `find_candidate_evidence_for_sender` already returns)."""
    normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
    normalized_subject_value = normalize_subject_for_policy(subject_predicate_value) or ""
    _validate_identity_fields(
        sender_scope_type=sender_scope_type, subject_predicate_type=subject_predicate_type,
        normalized_subject_predicate_value=normalized_subject_value,
    )

    if sender_scope_type == SENDER_SCOPE_EXACT_SENDER_ADDRESS:
        candidate_sender_domain = domain_from_address(normalized_scope_value)
        candidate_sender_address: Optional[str] = normalized_scope_value
    else:
        candidate_sender_domain = normalized_scope_value
        candidate_sender_address = None

    candidates = evidence_repository.find_candidate_evidence_for_sender(
        sender_domain=candidate_sender_domain, sender_address=candidate_sender_address, limit=candidate_limit,
    )

    synthetic_rule = _synthetic_candidate_rule(
        sender_scope_type=sender_scope_type, normalized_sender_scope_value=normalized_scope_value,
        subject_predicate_type=subject_predicate_type, normalized_subject_predicate_value=normalized_subject_value,
    )

    matches = []
    for item in candidates:
        result = match_evidence_to_rule(
            sender_address=item.metadata.get("sender_address"),
            subject=item.metadata.get("subject"),
            active_rules=[synthetic_rule],
        )
        if result.outcome == OUTCOME_MATCH:
            matches.append(item)
    return matches


def observed_evidence_guard(
    *,
    sender_scope_type: str,
    sender_scope_value: str,
    subject_predicate_type: str,
    subject_predicate_value: str,
    evidence_repository,
    candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT,
) -> int:
    """CD-6 Slice 5 WI-2 — the creation-time guard: before a new ACTIVE
    ``EvidenceClassificationRule`` may be created, it must match at
    least one REAL, persisted ``EvidenceItem`` (via the SAME matcher
    every other caller uses — see module docstring). Returns
    ``match_count`` (bounded — see module docstring); the CALLER decides
    the ``>= 1`` policy (this function never raises on zero matches
    itself — see ``services.evidence.classification_rule_service.create_rule``
    for where that decision is actually enforced).

    Never accidentally counts a manual-upload/non-mailbox
    ``EvidenceItem``: such items carry no ``sender_address``/``subject``
    metadata at all, so they are never even returned by
    ``find_candidate_evidence_for_sender`` in the first place."""
    return len(
        _observed_matches(
            sender_scope_type=sender_scope_type, sender_scope_value=sender_scope_value,
            subject_predicate_type=subject_predicate_type, subject_predicate_value=subject_predicate_value,
            evidence_repository=evidence_repository, candidate_limit=candidate_limit,
        )
    )


@dataclass(frozen=True)
class PreviewResult:
    """Read-only preview of what a candidate
    ``EvidenceClassificationRule`` identity WOULD do, against real,
    already-observed evidence — never persists anything, never emits an
    audit event, never returns document content/raw MIME/attachment
    bytes (subject/sender metadata only)."""

    normalized_sender_scope_value: str
    normalized_subject_predicate_value: str
    #: Bounded — see module docstring's "Bounded, never a full scan".
    match_count: int
    #: Bounded to (at most) the representative limit, paired by index
    #: with `representative_subjects`.
    representative_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    representative_subjects: tuple[Optional[str], ...] = field(default_factory=tuple)
    #: document_type (or the literal "NONE") -> count, over the
    #: representative subset only (see module docstring) — a bounded
    #: summary, never a full-corpus tally.
    current_classification_distribution: Mapping[str, int] = field(default_factory=dict)


def preview_classification_rule(
    *,
    sender_scope_type: str,
    sender_scope_value: str,
    subject_predicate_type: str,
    subject_predicate_value: str,
    document_type: str,
    evidence_repository,
    classification_repository,
    candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT,
    representative_limit: int = _DEFAULT_REPRESENTATIVE_LIMIT,
) -> PreviewResult:
    """CD-6 Slice 5 WI-2 — read-only preview of a candidate
    classification-rule identity. Validates ``document_type`` against
    the closed WI-1 vocabulary (this function's own concern — the
    shared identity-field validation
    ``services.evidence.classification_rule.validate_rule_fields_or_raise``
    performs is a superset that ALSO checks
    ``sender_scope_type``/``subject_predicate_type``/status/source —
    callers at the HTTP layer are expected to call that too; this
    function independently guards ``document_type`` since
    ``_observed_matches`` itself never needs or checks it).

    Never writes anything, never calls
    ``EvidenceClassificationRuleRepository.create_rule``, never emits an
    audit event.
    """
    if document_type not in DOCUMENT_TYPES:
        raise ValidationError(f"'{document_type}' is not one of {sorted(DOCUMENT_TYPES)}")

    normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
    normalized_subject_value = normalize_subject_for_policy(subject_predicate_value) or ""

    matches = _observed_matches(
        sender_scope_type=sender_scope_type, sender_scope_value=sender_scope_value,
        subject_predicate_type=subject_predicate_type, subject_predicate_value=subject_predicate_value,
        evidence_repository=evidence_repository, candidate_limit=candidate_limit,
    )

    representative = matches[:representative_limit]
    distribution: dict[str, int] = {}
    for item in representative:
        current = classification_repository.get_current_classification(
            item.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
        )
        key = current.document_type if current is not None else _NO_CURRENT_CLASSIFICATION_KEY
        distribution[key] = distribution.get(key, 0) + 1

    return PreviewResult(
        normalized_sender_scope_value=normalized_scope_value,
        normalized_subject_predicate_value=normalized_subject_value,
        match_count=len(matches),
        representative_evidence_ids=tuple(item.evidence_id for item in representative),
        representative_subjects=tuple(item.metadata.get("subject") for item in representative),
        current_classification_distribution=distribution,
    )
