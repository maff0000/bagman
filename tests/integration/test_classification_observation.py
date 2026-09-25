"""CD-6 Slice 5 WI-2 tests for
`services.evidence.classification_observation` — the observed-evidence
guard and read-only preview service, against the in-memory
`EvidenceRepository`/`EvidenceClassificationRepository`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai.invocation import InMemoryAIInvocationRepository
from core import identity
from core.external_reference import InMemoryExternalReferenceRepository
from services.evidence.classification import InMemoryEvidenceClassificationRepository
from services.evidence.classification_observation import observed_evidence_guard, preview_classification_rule
from services.evidence.classification_rule import InMemoryEvidenceClassificationRuleRepository
from services.evidence.evidence import InMemoryEvidenceRepository


@pytest.fixture
def evidence_repository():
    return InMemoryEvidenceRepository(InMemoryExternalReferenceRepository())


@pytest.fixture
def rule_repository():
    return InMemoryEvidenceClassificationRuleRepository()


@pytest.fixture
def classification_repository(evidence_repository, rule_repository):
    return InMemoryEvidenceClassificationRepository(
        evidence_repository=evidence_repository,
        rule_repository=rule_repository,
        ai_invocation_repository=InMemoryAIInvocationRepository(),
    )


def _register_email_evidence(evidence_repository, *, sender_address, subject, evidence_type="EMAIL") -> str:
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type=evidence_type, source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash="a" * 64, mime_type="message/rfc822", size_bytes=100,
        metadata={"sender_address": sender_address, "subject": subject},
    )
    return item.evidence_id


def _register_manual_upload_evidence(evidence_repository) -> str:
    now = datetime.now(timezone.utc)
    item = evidence_repository.register_evidence(
        entity_id=None, evidence_type="INVOICE", source_id=identity.generate_id(),
        observed_at=now, received_at=now, content_hash="b" * 64, mime_type="application/pdf", size_bytes=200,
        original_name="invoice.pdf", metadata={},
    )
    return item.evidence_id


# ---------------------------------------------------------------------
# Observed-evidence guard
# ---------------------------------------------------------------------


def test_guard_permits_when_one_real_matching_item_exists(evidence_repository):
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    match_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        evidence_repository=evidence_repository,
    )
    assert match_count == 1


def test_guard_rejects_zero_observed_matches(evidence_repository):
    match_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="never-seen.example",
        subject_predicate_type="EXACT", subject_predicate_value="anything",
        evidence_repository=evidence_repository,
    )
    assert match_count == 0


def test_guard_never_counts_manual_upload_evidence(evidence_repository):
    _register_manual_upload_evidence(evidence_repository)
    _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    match_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        evidence_repository=evidence_repository,
    )
    # Only the real EMAIL evidence counts — the manual upload (no
    # sender_address/subject metadata at all) can never accidentally
    # qualify.
    assert match_count == 1


def test_guard_address_scope(evidence_repository):
    _register_email_evidence(evidence_repository, sender_address="ap@vendor.com", subject="Invoice 123")
    _register_email_evidence(evidence_repository, sender_address="noreply@vendor.com", subject="Invoice 123")
    match_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_ADDRESS", sender_scope_value="ap@vendor.com",
        subject_predicate_type="STARTS_WITH", subject_predicate_value="Invoice",
        evidence_repository=evidence_repository,
    )
    assert match_count == 1


# ---------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------


def test_preview_returns_match_count_and_representative_evidence(evidence_repository, classification_repository):
    ev1 = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    result = preview_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE",
        evidence_repository=evidence_repository, classification_repository=classification_repository,
    )
    assert result.match_count == 1
    assert result.representative_evidence_ids == (ev1,)
    assert result.representative_subjects == ("Monthly Statement",)
    assert result.current_classification_distribution == {"NONE": 1}


def test_preview_current_classification_distribution_reflects_existing_classifications(
    evidence_repository, classification_repository, rule_repository
):
    ev1 = _register_email_evidence(evidence_repository, sender_address="billing@vendor.com", subject="Monthly Statement")
    rule = rule_repository.create_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE", source="OPERATOR",
    )
    classification_repository.create_classification(
        evidence_id=ev1, classification_type="DOCUMENT_TYPE", document_type="SUPPLIER_INVOICE",
        status="CLASSIFIED", source="DETERMINISTIC_RULE", rule_id=rule.rule_id,
    )
    result = preview_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
        subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
        document_type="SUPPLIER_INVOICE",
        evidence_repository=evidence_repository, classification_repository=classification_repository,
    )
    assert result.current_classification_distribution == {"SUPPLIER_INVOICE": 1}


def test_preview_never_returns_document_content_shape():
    """Structural proof: PreviewResult has no field that could carry
    document content/raw MIME/attachment bytes."""
    from services.evidence.classification_observation import PreviewResult

    field_names = {f for f in PreviewResult.__dataclass_fields__}
    forbidden = {"content", "raw_mime", "bytes", "attachment", "body"}
    assert not (field_names & forbidden)


def test_preview_rejects_unknown_document_type(evidence_repository, classification_repository):
    from core.errors import ValidationError

    with pytest.raises(ValidationError):
        preview_classification_rule(
            sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="vendor.com",
            subject_predicate_type="EXACT", subject_predicate_value="Monthly Statement",
            document_type="NOT_A_REAL_TYPE",
            evidence_repository=evidence_repository, classification_repository=classification_repository,
        )


# ---------------------------------------------------------------------
# Normalization identity between preview/guard/matcher — literal proof
# all three code paths agree on one hand-picked edge case.
# ---------------------------------------------------------------------


def test_guard_finds_older_than_200th_match_offset_exhaustive_walk(evidence_repository):
    """CD-6 correctness delta (2026-09-25), §8 — 'the key missing
    regression'. Create 226 persisted EvidenceItems for the SAME
    sender/domain (matching the real production interactivebrokers.com
    shape). Arrange the NEWEST 200 to NOT match the candidate subject
    predicate, while one OLDER record (received strictly before all 200
    newest) DOES match. The exhaustive guard must find it — a
    single-page (limit=200, no pagination) implementation would return
    0 here since the one genuine match falls entirely outside the first
    page."""
    from datetime import timedelta

    base = datetime.now(timezone.utc)
    # 226 total messages, oldest first in registration order — the
    # OLDEST one (received furthest in the past) is the sole genuine
    # match; the newest 200 are all decoys with a different subject.
    _register_email_evidence(
        evidence_repository, sender_address="donotreply@interactivebrokers.com",
        subject="FYI: Changes in Analyst Ratings",
    )
    oldest_evidence_id = None
    for i in range(226):
        item = evidence_repository.register_evidence(
            entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
            observed_at=base - timedelta(days=226 - i), received_at=base - timedelta(days=226 - i),
            content_hash=f"{i:064x}"[-64:], mime_type="message/rfc822", size_bytes=100,
            metadata={
                "sender_address": "donotreply@interactivebrokers.com",
                # Only the very oldest (i == 0) genuinely matches; the
                # remaining 225 (i == 1..225, i.e. all 200+ newest) do not.
                "subject": "FYI: Changes in Analyst Ratings" if i == 0 else "Some other newsletter",
            },
        )
        if i == 0:
            oldest_evidence_id = item.evidence_id
    assert oldest_evidence_id is not None

    match_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="interactivebrokers.com",
        subject_predicate_type="EXACT", subject_predicate_value="FYI: Changes in Analyst Ratings",
        evidence_repository=evidence_repository,
    )
    # The pre-existing single item registered via _register_email_evidence
    # above ALSO genuinely matches, so the true count is 2 (that item +
    # the oldest of the 226-item batch) — never a bounded/truncated 0 or 1.
    assert match_count == 2


def test_preview_exhaustive_match_count_and_full_corpus_distribution(evidence_repository, classification_repository):
    """CD-6 correctness delta (2026-09-25), §9 — a synthetic corpus
    shaped like production Preview B: 89 genuinely matching messages,
    137 nonmatching, total sender population > 200 (226). Requires
    `match_count == 89` regardless of which rows fall in the
    newest-200-row page, and requires
    `current_classification_distribution` to be computed over ALL 89
    matches (full matched-corpus, per the Architect's explicit V1
    preference), not just the bounded representative subset (default
    representative_limit=10)."""
    from datetime import timedelta

    base = datetime.now(timezone.utc)
    total = 226
    matching = 89
    # Interleave matching/nonmatching so genuine matches are NOT all
    # conveniently clustered at either end of the received_at ordering —
    # a stronger proof than "all matches happen to be oldest".
    match_evidence_ids = []
    for i in range(total):
        is_match = (i % (total // matching + 1)) == 0 and len(match_evidence_ids) < matching
        # Ensure we still land on exactly `matching` matches even if the
        # modulo pattern falls short near the end.
        if not is_match and (total - i) <= (matching - len(match_evidence_ids)):
            is_match = True
        item = evidence_repository.register_evidence(
            entity_id=None, evidence_type="EMAIL", source_id=identity.generate_id(),
            observed_at=base - timedelta(minutes=total - i), received_at=base - timedelta(minutes=total - i),
            content_hash=f"{i:064x}"[-64:], mime_type="message/rfc822", size_bytes=100,
            metadata={
                "sender_address": "donotreply@interactivebrokers.com",
                "subject": "FYI: Changes in Analyst Ratings" if is_match else "Daily Activity Statement",
            },
        )
        if is_match:
            match_evidence_ids.append(item.evidence_id)
    assert len(match_evidence_ids) == matching

    result = preview_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="interactivebrokers.com",
        subject_predicate_type="EXACT", subject_predicate_value="FYI: Changes in Analyst Ratings",
        document_type="NON_ACCOUNTING_DOCUMENT",
        evidence_repository=evidence_repository, classification_repository=classification_repository,
    )
    assert result.match_count == matching
    # Representative output stays bounded (default representative_limit
    # applies) — never equal to the full 89 match_count.
    assert len(result.representative_evidence_ids) < matching
    # The distribution is computed over ALL 89 matches, not just the
    # bounded representative subset.
    assert sum(result.current_classification_distribution.values()) == matching


def test_normalization_identical_across_guard_preview_and_matcher(evidence_repository, classification_repository):
    """One normalisation edge case (internal whitespace collapse +
    casefold), proven to be treated identically by all three call
    sites: the guard, the preview service, and the raw matcher."""
    from services.evidence.classification_matcher import OUTCOME_MATCH, match_evidence_to_rule
    from services.evidence.classification_rule import EvidenceClassificationRule

    ev1 = _register_email_evidence(
        evidence_repository, sender_address="Noreply@eBay.com", subject="Order  confirmed:  item 123"
    )

    guard_count = observed_evidence_guard(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="EBAY.com",
        subject_predicate_type="STARTS_WITH", subject_predicate_value="Order confirmed:",
        evidence_repository=evidence_repository,
    )
    assert guard_count == 1

    preview_result = preview_classification_rule(
        sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="EBAY.com",
        subject_predicate_type="STARTS_WITH", subject_predicate_value="Order confirmed:",
        document_type="ORDER_CONFIRMATION",
        evidence_repository=evidence_repository, classification_repository=classification_repository,
    )
    assert preview_result.match_count == 1
    assert preview_result.representative_evidence_ids == (ev1,)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    synthetic_rule = EvidenceClassificationRule(
        rule_id="r", sender_scope_type="EXACT_SENDER_DOMAIN", sender_scope_value="ebay.com",
        subject_predicate_type="STARTS_WITH", subject_predicate_value="order confirmed:",
        document_type="ORDER_CONFIRMATION", status="ACTIVE", source="OPERATOR", created_at=now, approved_at=now,
    )
    matcher_result = match_evidence_to_rule(
        sender_address="Noreply@eBay.com", subject="Order  confirmed:  item 123", active_rules=[synthetic_rule]
    )
    assert matcher_result.outcome == OUTCOME_MATCH
