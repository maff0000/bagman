"""CD-6 architect amendment tests for
``services.mailbox.discovery_signals.evaluate_discovery_candidate`` —
the bounded, non-AI heuristic gating whether an UNKNOWN-domain message
raises exactly one Needs You question.
"""
from __future__ import annotations

from services.mailbox.discovery_signals import evaluate_discovery_candidate


def test_subject_keyword_is_a_candidate():
    result = evaluate_discovery_candidate(subject="Your Invoice #4471 is ready", attachment_metadata=[])
    assert result.is_candidate is True
    assert "invoice" in result.reason


def test_subject_keyword_matching_is_case_insensitive():
    result = evaluate_discovery_candidate(subject="RECEIPT for your order", attachment_metadata=[])
    assert result.is_candidate is True


def test_plain_subject_with_no_attachments_is_not_a_candidate():
    result = evaluate_discovery_candidate(subject="Let's catch up next week", attachment_metadata=[])
    assert result.is_candidate is False


def test_none_subject_is_not_a_candidate_on_its_own():
    result = evaluate_discovery_candidate(subject=None, attachment_metadata=[])
    assert result.is_candidate is False


def test_attachment_filename_keyword_is_a_candidate():
    result = evaluate_discovery_candidate(
        subject="See attached",
        attachment_metadata=[{"filename": "statement_march.pdf", "content_type": "application/octet-stream", "size_bytes": 1024}],
    )
    assert result.is_candidate is True
    assert "attachment filename" in result.reason


def test_attachment_pdf_content_type_alone_is_a_candidate():
    result = evaluate_discovery_candidate(
        subject="Hello",
        attachment_metadata=[{"filename": "doc1.pdf", "content_type": "application/pdf", "size_bytes": 2048}],
    )
    assert result.is_candidate is True
    assert "content_type" in result.reason


def test_attachment_with_an_unrelated_content_type_and_filename_is_not_a_candidate():
    result = evaluate_discovery_candidate(
        subject="Team photo",
        attachment_metadata=[{"filename": "photo.gif", "content_type": "image/gif", "size_bytes": 500}],
    )
    assert result.is_candidate is False


def test_no_attachments_and_no_subject_signal_is_not_a_candidate():
    result = evaluate_discovery_candidate(subject="", attachment_metadata=None)
    assert result.is_candidate is False
    assert "no invoice" in result.reason


# -- `has_attachments` fallback (CD-6 discovery-signal fix) ---------------
#
# Gmail's Stage-A `format=metadata` fetch structurally cannot populate
# `attachment_metadata` (no parsed MIME part tree exists at that
# boundary) — so a real Gmail message with a genuine attachment but a
# vague subject and no filename/content-type detail must still surface
# as a candidate via the coarser, provider-neutral `has_attachments`
# fact, not be silently lost to `CHECKED_NOT_CANDIDATE`.


def test_has_attachments_true_with_no_other_signal_is_a_candidate_gmail_shaped():
    """Gmail-shaped: vague subject, no `attachment_metadata` at all
    (always empty for Gmail), `has_attachments=True` -> candidate, with
    the exact, architect-specified generic reason string."""
    result = evaluate_discovery_candidate(
        subject="Your document", attachment_metadata=None, has_attachments=True
    )
    assert result.is_candidate is True
    assert result.reason == "message metadata indicates one or more attachments"


def test_subject_keyword_wins_over_has_attachments_fallback():
    """Priority order A wins over D: a genuine subject keyword hit still
    produces the SUBJECT reason, never the generic attachment one, even
    when `has_attachments=True` too."""
    result = evaluate_discovery_candidate(
        subject="Your Invoice is ready", attachment_metadata=None, has_attachments=True
    )
    assert result.is_candidate is True
    assert "subject contains keyword" in result.reason
    assert result.reason != "message metadata indicates one or more attachments"


def test_attachment_content_type_wins_over_has_attachments_fallback():
    """Priority order C wins over D: a real PDF `attachment_metadata`
    entry still produces the CONTENT-TYPE-specific reason, never the
    generic one, even when `has_attachments=True` too."""
    result = evaluate_discovery_candidate(
        subject="Hello",
        attachment_metadata=[{"filename": "doc1.pdf", "content_type": "application/pdf", "size_bytes": 2048}],
        has_attachments=True,
    )
    assert result.is_candidate is True
    assert "content_type" in result.reason
    assert result.reason != "message metadata indicates one or more attachments"


def test_attachment_filename_keyword_wins_over_has_attachments_fallback():
    """Priority order B wins over D: a filename keyword hit still
    produces the FILENAME-specific reason, never the generic one, even
    when `has_attachments=True` too."""
    result = evaluate_discovery_candidate(
        subject="See attached",
        attachment_metadata=[{"filename": "statement_march.pdf", "content_type": "application/octet-stream", "size_bytes": 1024}],
        has_attachments=True,
    )
    assert result.is_candidate is True
    assert "attachment filename" in result.reason
    assert result.reason != "message metadata indicates one or more attachments"


def test_has_attachments_defaults_false_and_does_not_change_existing_behavior():
    """`has_attachments` defaults to `False` — every pre-existing call
    site/test in this file (none passing the new parameter) must see
    IDENTICAL behavior to before this change."""
    result = evaluate_discovery_candidate(subject="Let's catch up next week", attachment_metadata=[])
    assert result.is_candidate is False
