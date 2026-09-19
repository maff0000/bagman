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
