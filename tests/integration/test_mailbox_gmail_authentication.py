"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.authentication.assess_gmail_authentication` —
the REAL, evidence-grounded Gmail trust-boundary selector, built from a
live, controlled security acceptance test (two forged-header "canary"
messages sent from a mail server the operator controls, through its
completely normal/unmodified delivery path, to a real, connected Gmail
mailbox). See `services/mailbox/gmail/authentication.py`'s own module
docstring for the full evidence chronology and selector specification
this file proves against.

Sanitized regression fixtures (section 9 of the WO): all sender
addresses, Message-IDs, and IPs below are synthesized using
`.example`/`.invalid`-style domains and RFC 5737 documentation IP
ranges (`203.0.113.0/24`) — none of this is real captured data, only
the real captured HEADER STRUCTURE/SHAPE.

Adversarial matrix (section 10 of the WO): every one of the 21 required
cases is implemented below, each as its own test function, numbered in
its docstring/name for direct cross-reference against the WO.
"""
from __future__ import annotations

from services.mailbox.authentication_assessment import (
    AUTH_ASSESSMENT_FAIL,
    AUTH_ASSESSMENT_PASS,
    AUTH_ASSESSMENT_UNKNOWN,
)
from services.mailbox.gmail.authentication import assess_gmail_authentication


def _header(name: str, value: str) -> dict:
    return {"name": name, "value": value}


# ---------------------------------------------------------------------
# Shared header building blocks, mirroring the real captured shapes.
# ---------------------------------------------------------------------

_GOOGLE_INTERNAL_RECEIVED = _header(
    "Received",
    "by 2002:a9a:abcd:0:0:0:0:0 with SMTP id ab1cd23ef456; "
    "Mon, 21 Sep 2026 00:00:00 -0700 (PDT)",
)

_GENUINE_ARC_SEAL = _header(
    "ARC-Seal",
    "i=1; a=rsa-sha256; t=1758412800; cv=none; d=google.com; s=arc-20160816; "
    "b=REDACTEDGENUINESIGNATURE==",
)

_GENUINE_ARC_MESSAGE_SIGNATURE = _header(
    "ARC-Message-Signature",
    "i=1; a=rsa-sha256; c=relaxed/relaxed; d=google.com; s=arc-20160816; "
    "h=from:to:subject:date:message-id; bh=REDACTED=; b=REDACTEDGENUINE==",
)

_GENUINE_ARC_AUTHENTICATION_RESULTS = _header(
    "ARC-Authentication-Results",
    "i=1; mx.google.com; dkim=pass header.i=@noust-ai.example header.s=selector1 "
    "header.b=redacted; spf=pass (google.com: domain of matt@noust-ai.example "
    "designates 203.0.113.10 as permitted sender) smtp.mailfrom=matt@noust-ai.example; "
    "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=noust-ai.example",
)

_GENUINE_EXTERNAL_RECEIVED = _header(
    "Received",
    "from mail.noust-ai.example (mail.noust-ai.example. [203.0.113.10]) "
    "by mx.google.com with ESMTPS id ab1cd23ef456.2026.09.21.00.00.00 "
    "for <mgs241171@example.invalid>; "
    "Mon, 21 Sep 2026 00:00:00 -0700 (PDT)",
)

_GENUINE_AUTHENTICATION_RESULTS_DMARC_PASS = _header(
    "Authentication-Results",
    "mx.google.com; dkim=pass header.i=@noust-ai.example header.s=selector1 "
    "header.b=redacted; spf=pass (google.com: domain of matt@noust-ai.example "
    "designates 203.0.113.10 as permitted sender) smtp.mailfrom=matt@noust-ai.example; "
    "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=noust-ai.example",
)

_GENUINE_AUTHENTICATION_RESULTS_WITH_ARC_FAIL_TOKEN = _header(
    "Authentication-Results",
    "mx.google.com; dkim=pass header.i=@noust-ai.example header.s=selector1 "
    "header.b=redacted; spf=pass (google.com: domain of matt@noust-ai.example "
    "designates 203.0.113.10 as permitted sender) smtp.mailfrom=matt@noust-ai.example; "
    "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=noust-ai.example; "
    "arc=fail (arc missing headers)",
)

_GENUINE_DKIM_SIGNATURE = _header(
    "DKIM-Signature",
    "v=1; a=rsa-sha256; c=relaxed/relaxed; d=noust-ai.example; s=selector1; "
    "h=from:to:subject:date:message-id; bh=REDACTED=; b=REDACTEDGENUINE==",
)

_SENDER_LOCAL_RELAY_RECEIVED = _header(
    "Received",
    "from localhost (localhost [127.0.0.1]) by mail.noust-ai.example "
    "(Postfix) with ESMTP id ab12cd34; Mon, 21 Sep 2026 00:00:00 -0700 (PDT)",
)


def _normal_sample_headers() -> tuple[dict, ...]:
    """Section 9(a): a synthesized normal/organic real Gmail sample with
    a genuine DMARC PASS — the stable five-natural-samples shape."""
    return (
        _GOOGLE_INTERNAL_RECEIVED,
        _GENUINE_ARC_SEAL,
        _GENUINE_ARC_MESSAGE_SIGNATURE,
        _GENUINE_ARC_AUTHENTICATION_RESULTS,
        _GENUINE_EXTERNAL_RECEIVED,
        _GENUINE_AUTHENTICATION_RESULTS_DMARC_PASS,
        _GENUINE_DKIM_SIGNATURE,
        _SENDER_LOCAL_RELAY_RECEIVED,
    )


def _canary_a_headers() -> tuple[dict, ...]:
    """Section 9(b): Canary A's exact final delivered structure — the
    forged plain Authentication-Results header is ABSENT (Gmail
    stripped it entirely); only genuine headers remain."""
    return _normal_sample_headers()


def _canary_b_headers() -> tuple[dict, ...]:
    """Section 9(c): Canary B's exact final delivered structure — the
    genuine Authentication-Results header (carrying the genuine
    `arc=fail` token Gmail itself added), PLUS the forged Received/ARC
    pair surviving, positioned strictly below the genuine structure."""
    forged_received = _header(
        "Received",
        "from forged.invalid (forged.invalid. [203.0.113.99]) "
        "by mx.google.com with ESMTP id BAGMAN-CANARY-B; "
        "Mon, 21 Sep 2026 00:00:00 -0700 (PDT)",
    )
    forged_arc_seal = _header(
        "ARC-Seal",
        "i=1; a=rsa-sha256; t=1758412800; cv=none; d=google.com; s=arc-fake; "
        "b=FORGEDNONCRYPTOGRAPHICGARBAGE==",
    )
    forged_arc_authentication_results = _header(
        "ARC-Authentication-Results",
        "i=1; mx.google.com; dmarc=pass header.from=forged.invalid",
    )
    return (
        _GOOGLE_INTERNAL_RECEIVED,
        _GENUINE_EXTERNAL_RECEIVED,
        _GENUINE_AUTHENTICATION_RESULTS_WITH_ARC_FAIL_TOKEN,
        _GENUINE_DKIM_SIGNATURE,
        _SENDER_LOCAL_RELAY_RECEIVED,
        forged_received,
        forged_arc_seal,
        forged_arc_authentication_results,
    )


# ---------------------------------------------------------------------
# Adversarial matrix — WO section 10, all 21 required cases.
# ---------------------------------------------------------------------


def test_01_real_shaped_candidate_dmarc_pass_is_pass():
    result = assess_gmail_authentication(_normal_sample_headers())
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert result.evidence["eligible_candidate_count"] == 1


def test_02_real_shaped_candidate_dmarc_fail_is_fail():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "mx.google.com; dmarc=fail (p=REJECT) header.from=forged.invalid"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_03_eligible_candidate_no_dmarc_token_spf_dkim_only_is_unknown():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "mx.google.com; spf=pass smtp.mailfrom=matt@noust-ai.example; dkim=pass"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["selector_reason"] == "missing_dmarc_token"


def test_04_only_arc_headers_present_no_eligible_plain_ar_is_unknown():
    headers = (
        _GOOGLE_INTERNAL_RECEIVED,
        _GENUINE_ARC_SEAL,
        _GENUINE_ARC_MESSAGE_SIGNATURE,
        _GENUINE_ARC_AUTHENTICATION_RESULTS,
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["eligible_candidate_count"] == 0
    assert result.evidence["authentication_results_header_count"] == 0


def test_05_forged_arc_claiming_google_no_separate_genuine_candidate_is_unknown():
    forged_arc_seal = _header("ARC-Seal", "i=1; a=rsa-sha256; cv=none; d=google.com; s=arc-fake; b=FORGED==")
    forged_arc_ar = _header("ARC-Authentication-Results", "i=1; mx.google.com; dmarc=pass header.from=forged.invalid")
    result = assess_gmail_authentication((forged_arc_seal, forged_arc_ar))
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_06_forged_arc_plus_separate_genuine_eligible_header_genuine_gates_pass():
    forged_arc_seal = _header("ARC-Seal", "i=1; a=rsa-sha256; cv=none; d=google.com; s=arc-fake; b=FORGED==")
    headers = (forged_arc_seal,) + _normal_sample_headers()
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert result.evidence["eligible_candidate_count"] == 1


def test_07_forged_received_by_mx_google_no_eligible_ar_header_is_unknown():
    forged_received = _header(
        "Received",
        "from forged.invalid (forged.invalid. [203.0.113.99]) by mx.google.com with ESMTP id FORGED; "
        "Mon, 21 Sep 2026 00:00:00 -0700 (PDT)",
    )
    result = assess_gmail_authentication((forged_received,))
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["authentication_results_header_count"] == 0


def test_08_canary_b_structure_genuine_header_gates_not_forged_material_below():
    result = assess_gmail_authentication(_canary_b_headers())
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert result.evidence["eligible_candidate_count"] == 1
    assert result.evidence["selected_header_tokens"]["dmarc"] == "pass"
    # The genuine header's own captured `arc=fail` token is evidence only.
    assert result.evidence["selected_header_tokens"].get("arc") == "fail"


def test_09_non_eligible_ar_header_with_conflicting_verdict_never_influences_genuine():
    non_eligible = _header("Authentication-Results", "mx.google.com; dmarc=fail header.from=forged.invalid")
    headers = (non_eligible,) + _normal_sample_headers()
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert result.evidence["additional_authentication_results_header_count"] == 1
    assert result.evidence["additional_authentication_results_header_tokens"]["dmarc"] == "fail"


def test_10_two_independently_eligible_headers_is_ambiguous_unknown():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "mx.google.com; dmarc=pass header.from=noust-ai.example"),
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "mx.google.com; dmarc=pass header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["selector_reason"] == "ambiguous_multiple_candidates"
    assert result.evidence["eligible_candidate_count"] == 2


def test_11_near_match_authserv_id_is_unknown():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "mx.google.com.invalid; dmarc=pass header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["eligible_candidate_count"] == 0


def test_12_near_match_received_by_host_is_unknown():
    near_match_received = _header(
        "Received",
        "from mail.noust-ai.example (mail.noust-ai.example. [203.0.113.10]) "
        "by mx.google.com.invalid with ESMTPS id ab1cd23ef456; Mon, 21 Sep 2026 00:00:00 -0700 (PDT)",
    )
    headers = (
        near_match_received,
        _header("Authentication-Results", "mx.google.com; dmarc=pass header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["eligible_candidate_count"] == 0


def test_13_eligible_shaped_header_detached_from_genuine_received_is_unknown():
    # A genuine `by mx.google.com` Received header exists further back,
    # but it is NOT the closest preceding Received header — a different,
    # non-matching Received hop (the sender's own local relay) sits
    # directly before the candidate instead. The positional relationship
    # criterion 3 requires is broken (detached/reordered).
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _SENDER_LOCAL_RELAY_RECEIVED,
        _header("Authentication-Results", "mx.google.com; dmarc=pass header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["eligible_candidate_count"] == 0


def test_14_no_received_with_by_mx_google_com_anywhere_is_unknown():
    headers = (
        _header("Authentication-Results", "mx.google.com; dmarc=pass header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["selector_reason"] == "no_eligible_candidate"


def test_15_dmarc_pass_spf_fail_is_still_pass():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header(
            "Authentication-Results",
            "mx.google.com; spf=fail smtp.mailfrom=forged.invalid; dmarc=pass header.from=noust-ai.example",
        ),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS


def test_16_dmarc_pass_dkim_fail_is_still_pass():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header(
            "Authentication-Results",
            "mx.google.com; dkim=fail header.i=@forged.invalid; dmarc=pass header.from=noust-ai.example",
        ),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS


def test_17_dmarc_fail_with_favorable_spf_dkim_is_still_fail():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header(
            "Authentication-Results",
            "mx.google.com; spf=pass smtp.mailfrom=matt@noust-ai.example; dkim=pass header.i=@noust-ai.example; "
            "dmarc=fail header.from=forged.invalid",
        ),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_18_duplicate_dmarc_tokens_same_header_worst_verdict_wins_is_fail():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header(
            "Authentication-Results",
            "mx.google.com; dmarc=pass header.from=noust-ai.example; dmarc=fail header.from=forged.invalid",
        ),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_19_unrecognized_malformed_dmarc_value_is_unknown():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "mx.google.com; dmarc=bogus header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["selector_reason"] == "unrecognized_dmarc_verdict"


def test_20_real_canary_a_fixture_is_pass():
    result = assess_gmail_authentication(_canary_a_headers())
    assert result.verdict == AUTH_ASSESSMENT_PASS


def test_21_real_canary_b_fixture_is_pass():
    result = assess_gmail_authentication(_canary_b_headers())
    assert result.verdict == AUTH_ASSESSMENT_PASS


# ---------------------------------------------------------------------
# Additional baseline/edge coverage (beyond the required 21).
# ---------------------------------------------------------------------


def test_no_headers_at_all_is_unknown():
    result = assess_gmail_authentication(None)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_empty_headers_list_is_unknown():
    result = assess_gmail_authentication(())
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_header_with_no_authserv_id_at_all_is_unknown():
    headers = (
        _GENUINE_EXTERNAL_RECEIVED,
        _header("Authentication-Results", "dmarc=pass"),  # malformed — no `;`
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_unrelated_header_names_are_ignored_entirely():
    headers = (_header("X-Some-Other-Header", "dmarc=pass"),)
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["authentication_results_header_count"] == 0


def test_malformed_received_by_host_is_unknown_with_specific_reason():
    unparseable_received = _header("Received", "from mail.noust-ai.example (a comment lacking the receiving-hop clause)")
    headers = (
        unparseable_received,
        _header("Authentication-Results", "mx.google.com; dmarc=pass header.from=noust-ai.example"),
    )
    result = assess_gmail_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["selector_reason"] == "malformed_received_by_host"


def test_evidence_never_dumps_raw_header_content_keys_are_bounded():
    result = assess_gmail_authentication(_normal_sample_headers())
    expected_keys = {
        "authentication_results_header_count",
        "eligible_candidate_count",
        "selected_header_authserv_id",
        "selected_header_tokens",
        "additional_authentication_results_header_count",
        "additional_authentication_results_header_tokens",
        "arc_seal_header_count",
        "arc_message_signature_header_count",
        "arc_authentication_results_header_count",
        "arc_authentication_results_tokens",
        "selector_reason",
    }
    assert set(result.evidence.keys()) == expected_keys
