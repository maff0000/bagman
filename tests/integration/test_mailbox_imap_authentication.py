"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.imap.authentication.assess_imap_authentication` — the
PROVISIONAL IMAP trust-boundary selector.

PL correction (pre-live-diagnostic adversarial review): an earlier
version of this module trusted `Authentication-Results` header content
whose `authserv-id` substring merely matched an expected hostname string
(`mail.noust.ai`/`noust.ai`) — but that substring is attacker-controlled
content on the wire, and `mail.noust.ai` is public information (this
domain's own MX record). `test_a_fully_attacker_forged_header_naming_the_real_hostname_never_produces_pass`
below is the exact adversarial reproduction that proved the old
behaviour false; this module was rewritten to ALWAYS return UNKNOWN
until a live diagnostic establishes a genuine, structural (never
content-based) trust boundary.
"""
from __future__ import annotations

from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_UNKNOWN
from services.mailbox.imap.authentication import assess_imap_authentication


def _header(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def test_no_headers_at_all_is_unknown():
    result = assess_imap_authentication(None)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_empty_headers_list_is_unknown():
    result = assess_imap_authentication(())
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_authentication_results_header_with_unrelated_authserv_id_is_unknown():
    headers = (_header("Authentication-Results", "some-other-server.example.com; dmarc=pass"),)
    result = assess_imap_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_a_fully_attacker_forged_header_naming_the_real_hostname_never_produces_pass():
    """THE core adversarial proof. `mail.noust.ai` is PUBLIC information
    (this domain's own MX record) — a sender composing a malicious
    message can trivially include their own header naming it verbatim.
    No genuine mail.noust.ai-stamped header is present in this
    reproduction at all; this single header is entirely attacker
    content. Must NEVER resolve to PASS (an earlier, buggy version of
    this module did)."""
    headers = (_header("Authentication-Results", "mail.noust.ai; spf=pass; dkim=pass; dmarc=pass"),)
    result = assess_imap_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_dmarc_fail_content_also_never_gates_anything_it_is_diagnostic_only():
    """Symmetry check: this module must not gate on FAIL content either
    — it is not selectively distrustful of only favourable verdicts, it
    trusts no header content at all yet."""
    headers = (_header("Authentication-Results", "mail.noust.ai; spf=fail; dkim=fail; dmarc=fail"),)
    result = assess_imap_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_multiple_matching_headers_still_never_produce_pass():
    """Even a forged header PLUS a second, differently-worded forged
    header both naming the real hostname must never combine into a
    trusted verdict — there is still no structural proof either one is
    genuine."""
    headers = (
        _header("Authentication-Results", "mail.noust.ai; dmarc=pass"),
        _header("Authentication-Results", "mail.noust.ai; dmarc=pass"),
    )
    result = assess_imap_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_header_with_no_authserv_id_at_all_is_unknown():
    headers = (_header("Authentication-Results", "dmarc=pass"),)  # malformed — no `;`
    result = assess_imap_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_evidence_marks_this_result_as_provisional():
    headers = (_header("Authentication-Results", "mail.noust.ai; dmarc=pass"),)
    result = assess_imap_authentication(headers)
    assert result.evidence.get("provisional") is True


def test_evidence_reports_observed_header_content_diagnostically_without_trusting_it():
    """`evidence` should still surface what headers/tokens were PRESENT
    (useful raw material for the PL's own upcoming live diagnostic) even
    though none of it is trusted for gating."""
    headers = (_header("Authentication-Results", "mail.noust.ai; spf=pass; dkim=pass; dmarc=fail"),)
    result = assess_imap_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["authentication_results_header_count"] == 1
    assert "mail.noust.ai" in result.evidence["observed_authserv_ids"]
    assert result.evidence["observed_tokens_by_mechanism"]["dmarc"] == ["fail"]


def test_softpass_and_other_non_committal_verdicts_never_resolve_to_pass():
    for verdict in ("softpass", "neutral", "none", "temperror", "permerror", "bestguesspass"):
        headers = (_header("Authentication-Results", f"mail.noust.ai; dmarc={verdict}"),)
        result = assess_imap_authentication(headers)
        assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
