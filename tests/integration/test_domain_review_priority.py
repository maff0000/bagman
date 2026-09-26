"""Tests for ``services.mailbox.domain_review_priority`` — the bounded,
deterministic, non-AI mailbox-evidence-based operator-triage priority
(CD-6 GUI-operations-foundation WO, mailbox-evidence-based triage
addendum). Mirrors ``tests/integration/test_discovery_signals.py``'s own
lightweight, no-fixture-framework style — these are pure functions, no
repository/HTTP setup needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.mailbox.domain_review_priority import (
    BUCKET_ACCOUNTING_DOCUMENT_ATTACHMENT,
    BUCKET_INVOICE_LIKE_ATTACHMENT,
    BUCKET_INVOICE_SUBJECT,
    BUCKET_OTHER,
    BUCKET_RECEIPT_SUBJECT,
    aggregate_discovery_reasons,
    compute_review_priority,
)

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _msg(reason):
    """The cheapest correct stand-in — `aggregate_discovery_reasons`
    only ever reads `.discovery_reason` off whatever it is given."""
    return SimpleNamespace(discovery_reason=reason)


# ---------------------------------------------------------------------
# compute_review_priority — the docstring's own worked examples, each
# asserted exactly (the worked math is a real, checked contract).
# ---------------------------------------------------------------------


def test_worked_example_a_is_high():
    # 12 candidates, 8 with attachments, spanning 60 days,
    # STRONG_PURCHASE_BILL, 2 distinct discovery-reason buckets.
    # score = 3 (volume) + 3 (ratio) + 2 (span) + 4 (xero) + 1 (diversity) = 13
    priority = compute_review_priority(
        candidate_message_count=12,
        attachment_bearing_count=8,
        first_seen_at=_NOW,
        last_seen_at=_NOW + timedelta(days=60),
        xero_correlation_class="STRONG_PURCHASE_BILL",
        discovery_reason_counts={BUCKET_INVOICE_SUBJECT: 7, BUCKET_ACCOUNTING_DOCUMENT_ATTACHMENT: 5},
    )
    assert priority == "HIGH"


def test_worked_example_b_is_medium():
    # 4 candidates, 1 with attachment, spanning 10 days, CONTACT_ONLY,
    # 2 distinct discovery-reason buckets.
    # score = 2 (volume) + 1 (ratio) + 0 (span) + 1 (xero) + 1 (diversity) = 5
    priority = compute_review_priority(
        candidate_message_count=4,
        attachment_bearing_count=1,
        first_seen_at=_NOW,
        last_seen_at=_NOW + timedelta(days=10),
        xero_correlation_class="CONTACT_ONLY",
        discovery_reason_counts={BUCKET_RECEIPT_SUBJECT: 3, BUCKET_OTHER: 1},
    )
    assert priority == "MEDIUM"


def test_worked_example_c_is_low():
    # 1 candidate, no attachment, single day, no correlation, 1 bucket.
    # score = 1 (volume) + 0 (ratio) + 0 (span) + 0 (xero) + 0 (diversity) = 1
    priority = compute_review_priority(
        candidate_message_count=1,
        attachment_bearing_count=0,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        xero_correlation_class=None,
        discovery_reason_counts={BUCKET_OTHER: 1},
    )
    assert priority == "LOW"


def test_shared_domain_with_high_volume_never_scores_high():
    # 20 candidates, 20 with attachments (ratio 1.0), spanning 100 days,
    # SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW, 3 distinct buckets.
    # Pre-cap score = 3 (volume) + 3 (ratio) + 2 (span) - 1 (xero) + 1 (diversity) = 8
    # -- exactly the HIGH threshold -- but the shared-domain cap must
    # force this down to MEDIUM regardless. This proves the cap is a
    # real, enforced rule (this exact case would be HIGH without it),
    # not an accident of the chosen weights.
    priority = compute_review_priority(
        candidate_message_count=20,
        attachment_bearing_count=20,
        first_seen_at=_NOW,
        last_seen_at=_NOW + timedelta(days=100),
        xero_correlation_class="SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW",
        discovery_reason_counts={
            BUCKET_INVOICE_SUBJECT: 5,
            BUCKET_INVOICE_LIKE_ATTACHMENT: 5,
            BUCKET_ACCOUNTING_DOCUMENT_ATTACHMENT: 5,
        },
    )
    assert priority != "HIGH"
    assert priority == "MEDIUM"


def test_shared_domain_can_still_be_low_when_evidence_is_thin():
    # A shared domain with almost no evidence at all is still LOW, not
    # artificially inflated to MEDIUM by the cap logic (the cap only
    # ever pulls a HIGH down to MEDIUM, it never raises a LOW).
    priority = compute_review_priority(
        candidate_message_count=1,
        attachment_bearing_count=0,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        xero_correlation_class="SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW",
        discovery_reason_counts={BUCKET_OTHER: 1},
    )
    assert priority == "LOW"


def test_zero_candidate_count_never_crashes_on_attachment_ratio():
    # Defensive: candidate_message_count == 0 must never raise a
    # ZeroDivisionError computing the attachment ratio.
    priority = compute_review_priority(
        candidate_message_count=0,
        attachment_bearing_count=0,
        first_seen_at=_NOW,
        last_seen_at=_NOW,
        xero_correlation_class=None,
        discovery_reason_counts={},
    )
    assert priority == "LOW"


# ---------------------------------------------------------------------
# aggregate_discovery_reasons — every one of the 5 buckets, the
# other-bucket catch-all with 2+ distinct underlying keywords, an empty
# list, and an unrecognised/None reason string.
# ---------------------------------------------------------------------


def test_aggregate_discovery_reasons_buckets_every_real_reason_shape():
    messages = [
        _msg("subject contains keyword 'invoice'"),
        _msg("subject contains keyword 'tax invoice'"),
        _msg("subject contains keyword 'receipt'"),
        _msg("attachment filename contains keyword 'invoice'"),
        _msg("attachment filename contains keyword 'receipt'"),
        _msg("attachment content_type 'application/pdf' is a common accounting-document type"),
        _msg("attachment content_type 'image/heic' is a common accounting-document type"),
        # "other" bucket — at least 2 different underlying keywords,
        # via both subject and attachment-filename templates.
        _msg("subject contains keyword 'statement'"),
        _msg("subject contains keyword 'payment'"),
        _msg("attachment filename contains keyword 'remittance'"),
    ]
    counts = aggregate_discovery_reasons(messages)
    assert counts == {
        "invoice_subject_signal_count": 2,
        "receipt_subject_signal_count": 1,
        "invoice_like_attachment_filename_count": 2,
        "accounting_document_attachment_signal_count": 2,
        "other_bounded_heuristic_reason_count": 3,
    }


def test_aggregate_discovery_reasons_empty_list_is_all_zero_no_crash():
    counts = aggregate_discovery_reasons([])
    assert counts == {
        "invoice_subject_signal_count": 0,
        "receipt_subject_signal_count": 0,
        "invoice_like_attachment_filename_count": 0,
        "accounting_document_attachment_signal_count": 0,
        "other_bounded_heuristic_reason_count": 0,
    }


def test_aggregate_discovery_reasons_none_and_unrecognised_reason_never_raises():
    messages = [
        _msg(None),
        _msg("some completely unexpected future reason string"),
    ]
    counts = aggregate_discovery_reasons(messages)
    assert counts["other_bounded_heuristic_reason_count"] == 2
    assert sum(counts.values()) == 2
