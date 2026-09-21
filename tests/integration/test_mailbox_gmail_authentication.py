"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.authentication.assess_gmail_authentication` —
the PROVISIONAL Gmail trust-boundary selector. Mirrors
`tests/integration/test_mailbox_imap_authentication.py`'s own
adversarial-proof style exactly (see that file's own docstring for the
real, confirmed pre-live-diagnostic finding this doctrine guards
against), adapted to Gmail's own plausible header shape.
"""
from __future__ import annotations

from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_UNKNOWN
from services.mailbox.gmail.authentication import assess_gmail_authentication


def _header(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def test_no_headers_at_all_is_unknown():
    result = assess_gmail_authentication(None)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_empty_headers_list_is_unknown():
    result = assess_gmail_authentication(())
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_a_fully_attacker_forged_header_naming_googles_own_real_hostname_never_produces_pass():
    """THE core adversarial proof, mirroring the IMAP module's own
    identical one. `mx.google.com` is PUBLIC information (Gmail's own,
    widely-documented receiving MTA convention) — a sender composing a
    malicious message can trivially include their own header naming it
    verbatim. No genuinely Gmail-stamped header is present in this
    reproduction at all; this single header is entirely attacker
    content. Must NEVER resolve to PASS."""
    headers = (_header("Authentication-Results", "mx.google.com; spf=pass; dkim=pass; dmarc=pass"),)
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_arc_authentication_results_header_also_never_gates_a_verdict():
    """`ARC-Authentication-Results` is inspected diagnostically too (see
    module docstring), but is equally attacker-forgeable content — must
    never gate a PASS either."""
    headers = (_header("ARC-Authentication-Results", "i=1; mx.google.com; spf=pass; dkim=pass; dmarc=pass"),)
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_dmarc_fail_content_also_never_gates_anything_it_is_diagnostic_only():
    """Symmetry check: this module must not gate on FAIL content
    either."""
    headers = (_header("Authentication-Results", "mx.google.com; spf=fail; dkim=fail; dmarc=fail"),)
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_multiple_matching_headers_still_never_produce_pass():
    headers = (
        _header("Authentication-Results", "mx.google.com; dmarc=pass"),
        _header("Authentication-Results", "mx.google.com; dmarc=pass"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_header_with_no_authserv_id_at_all_is_unknown():
    headers = (_header("Authentication-Results", "dmarc=pass"),)  # malformed — no `;`
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_evidence_marks_this_result_as_provisional():
    headers = (_header("Authentication-Results", "mx.google.com; dmarc=pass"),)
    result = assess_gmail_authentication(headers)
    assert result.evidence.get("provisional") is True


def test_evidence_reports_observed_header_content_diagnostically_without_trusting_it():
    headers = (_header("Authentication-Results", "mx.google.com; spf=pass; dkim=pass; dmarc=fail"),)
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["authentication_results_header_count"] == 1
    assert "mx.google.com" in result.evidence["observed_authserv_ids"]
    assert result.evidence["observed_tokens_by_mechanism"]["dmarc"] == ["fail"]


def test_softpass_and_other_non_committal_verdicts_never_resolve_to_pass():
    for verdict in ("softpass", "neutral", "none", "temperror", "permerror", "bestguesspass"):
        headers = (_header("Authentication-Results", f"mx.google.com; dmarc={verdict}"),)
        result = assess_gmail_authentication(headers)
        assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_unrelated_header_names_are_ignored_entirely():
    headers = (_header("X-Some-Other-Header", "dmarc=pass"),)
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["authentication_results_header_count"] == 0
