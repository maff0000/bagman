"""``services.mailbox.microsoft.authentication.assess_microsoft_authentication``
— the real Microsoft trust-boundary selector (CD-6 GUI-operations-
foundation follow-on WO, item B: "provider-neutral authentication
assessment with a real Microsoft trust-boundary selector"). Lives under
``tests/security/`` (not ``tests/integration/``) because these are
adversarial/security-boundary proofs — a spoofer's forged header must
never be able to manufacture a PASS verdict — the exact class of proof
this directory's own module docstring (PID.md §15) exists for.

Built against the real, live, redacted diagnostic data captured in this
WO's own PID (8 real Infosecurs inbound messages):

* Every one of the 8 real samples carried exactly ONE
  ``Authentication-Results`` header (zero ``ARC-Authentication-Results``
  observed) shaped::

      spf=pass (sender IP is <redacted>) smtp.mailfrom=[REDACTED];
      dkim=pass (signature was verified) header.d=[REDACTED];
      dmarc=pass action=none header.from=[REDACTED];
      compauth=pass reason=100

* 2 of the 8 samples carried TWO separate ``dkim=`` tokens WITHIN the
  SAME header value (DKIM-signed by both the originating domain and a
  relay/ESP) — the confirmed real bug this WO fixes (the old parser's
  ``setdefault`` silently kept only the FIRST token).
"""
from __future__ import annotations

from services.mailbox.authentication_assessment import (
    AUTH_ASSESSMENT_FAIL,
    AUTH_ASSESSMENT_PASS,
    AUTH_ASSESSMENT_UNKNOWN,
)
from services.mailbox.microsoft.authentication import assess_microsoft_authentication


def _header(value: str, *, name: str = "Authentication-Results") -> dict:
    return {"name": name, "value": value}


# ---------------------------------------------------------------------
# The real, live, redacted diagnostic shape — worked examples
# ---------------------------------------------------------------------


def test_real_diagnostic_shape_single_header_all_pass_yields_pass():
    """Exact real shape (redacted) from the live Infosecurs diagnostic
    sample — a single trusted header, everything pass, compauth=pass."""
    headers = [
        _header(
            "spf=pass (sender IP is 40.107.20.51) smtp.mailfrom=REDACTED.example.com;"
            "dkim=pass (signature was verified) header.d=REDACTED.example.com;"
            "dmarc=pass action=none header.from=REDACTED.example.com;"
            "compauth=pass reason=100"
        )
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert "compauth" in result.reason


def test_real_diagnostic_shape_duplicate_dkim_token_within_one_header_is_handled():
    """The exact real, confirmed bug: 2 of the 8 real live samples
    carried TWO separate `dkim=` tokens within the SAME single trusted
    header value (DKIM-signed by both the originating domain and a
    relay/ESP). The OLD parser's `setdefault` would silently keep only
    the FIRST token; this selector must not merely fail to crash on it —
    it must resolve it via the documented worst-wins rule and still
    reach the correct overall verdict (real compauth=pass here, so PASS
    regardless of which dkim token 'wins')."""
    headers = [
        _header(
            "spf=pass (sender IP is 40.107.20.51) smtp.mailfrom=REDACTED.example.com;"
            "dkim=pass (signature was verified) header.d=REDACTED.example.com;"
            "dkim=pass (signature was verified) header.d=relay.example.com;"
            "dmarc=pass action=none header.from=REDACTED.example.com;"
            "compauth=pass reason=100"
        )
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert result.evidence["merged_tokens"]["dkim"] == "pass"


def test_real_diagnostic_shape_duplicate_dkim_token_worst_wins_when_they_disagree():
    """The generalised fix: when two `dkim=` tokens in the SAME header
    genuinely disagree, the conservative worst-verdict-wins rule applies
    — a mixed result is not an unambiguous pass. Here the SAME header
    still carries a real compauth=pass (an entirely independent
    Microsoft-stamped verdict, unaffected by the dkim disagreement), so
    the overall verdict is still PASS — but the underlying merged
    evidence must honestly reflect the WORSE dkim verdict, never the
    better one, so an auditor never sees a falsely-rosier picture of the
    raw facts."""
    headers = [
        _header(
            "dkim=pass header.d=REDACTED.example.com;"
            "dkim=fail header.d=relay.example.com;"
            "dmarc=pass action=none header.from=REDACTED.example.com;"
            "compauth=pass reason=100"
        )
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS  # compauth=pass gates the verdict, spf/dkim never do
    assert result.evidence["merged_tokens"]["dkim"] == "fail"  # worst-wins evidence, honestly recorded


# ---------------------------------------------------------------------
# The real compauth PASS/FAIL/UNKNOWN matrix
# ---------------------------------------------------------------------


def test_compauth_pass_yields_pass():
    result = assess_microsoft_authentication([_header("dmarc=pass; compauth=pass reason=100")])
    assert result.verdict == AUTH_ASSESSMENT_PASS


def test_compauth_fail_yields_fail_even_with_passing_dmarc():
    """compauth is checked BEFORE dmarc — a trusted compauth=fail is
    decisive even if dmarc on the same header says pass (a real,
    documented judgment call: compauth is Microsoft's own strongest,
    least-forgeable signal)."""
    result = assess_microsoft_authentication([_header("dmarc=pass; compauth=fail reason=001")])
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_compauth_softpass_falls_through_to_dmarc_pass():
    result = assess_microsoft_authentication([_header("dmarc=pass action=none; compauth=softpass reason=002")])
    assert result.verdict == AUTH_ASSESSMENT_PASS


def test_compauth_softpass_falls_through_to_dmarc_fail():
    result = assess_microsoft_authentication([_header("dmarc=fail action=quarantine; compauth=softpass reason=002")])
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_compauth_none_and_dmarc_none_yields_unknown():
    result = assess_microsoft_authentication([_header("dmarc=none; compauth=none reason=000")])
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_compauth_missing_and_dmarc_bestguesspass_yields_unknown():
    """`bestguesspass`/`temperror`/`permerror` are all non-committal —
    never treated as an affirmative pass."""
    result = assess_microsoft_authentication([_header("dmarc=bestguesspass action=none")])
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_no_headers_at_all_yields_unknown():
    assert assess_microsoft_authentication(None).verdict == AUTH_ASSESSMENT_UNKNOWN
    assert assess_microsoft_authentication([]).verdict == AUTH_ASSESSMENT_UNKNOWN


# ---------------------------------------------------------------------
# The DMARC-alignment-sensitive case (architect's own named concern)
# ---------------------------------------------------------------------


def test_spf_fail_under_dmarc_pass_never_escalates():
    """The architect's own explicitly-named real problem case: 'a DMARC
    PASS combined with an SPF FAIL can currently be escalated merely
    because SPF contains fail, even though DMARC may legitimately have
    passed through aligned DKIM'. SPF/DKIM are NEVER independently
    gated on by this selector — only compauth/dmarc do."""
    headers = [
        _header(
            "spf=fail (sender IP is 1.2.3.4) smtp.mailfrom=vendor.com;"
            "dkim=pass header.d=vendor.com;"
            "dmarc=pass action=none header.from=vendor.com;"
            "compauth=pass reason=100"
        )
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS


def test_spf_pass_alone_without_dmarc_or_compauth_is_never_treated_as_proof():
    """A bare SPF PASS (or DKIM PASS) alone, with no decisive DMARC/
    compauth verdict at all, must never be treated as proof of
    authenticity — SPF/DKIM alone prove a DIFFERENT domain authenticated,
    not necessarily the visible From domain, unless DMARC's own
    alignment check has actually passed."""
    headers = [_header("spf=pass smtp.mailfrom=vendor.com; dkim=pass header.d=vendor.com")]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


# ---------------------------------------------------------------------
# The required adversarial forged/duplicate-header proof
# ---------------------------------------------------------------------


def test_forged_header_claiming_pass_cannot_override_a_genuine_fail_when_forged_is_first():
    """A forged SECOND `Authentication-Results` header claiming
    `compauth=pass`, positioned FIRST (the position a naive 'first match
    wins' parser would trust), alongside a genuine header reporting
    `compauth=fail` SECOND. The real trust-boundary logic must still
    pick the correct (FAIL) outcome — position alone must never be
    exploitable."""
    headers = [
        _header("spf=pass; dkim=pass; dmarc=pass action=none; compauth=pass reason=100"),  # forged, first
        _header("spf=fail; dkim=fail; dmarc=fail action=quarantine; compauth=fail reason=001"),  # genuine, second
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_forged_header_claiming_pass_cannot_override_a_genuine_fail_when_forged_is_second():
    """The same proof with the forged header positioned SECOND (the more
    realistic real-world RFC 5322 stacking scenario — an attacker's own
    header, injected earlier in the delivery chain, ends up LOWER in the
    list Graph returns, since Microsoft's own genuine final-hop header is
    prepended ahead of it) — the outcome must be identical either way:
    order-independent for a genuine conflict."""
    headers = [
        _header("spf=fail; dkim=fail; dmarc=fail action=quarantine; compauth=fail reason=001"),  # genuine, first
        _header("spf=pass; dkim=pass; dmarc=pass action=none; compauth=pass reason=100"),  # forged, second
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_forged_header_alone_with_no_genuine_header_is_trusted_as_is():
    """Sanity check on the adversarial proofs above: a SINGLE header
    (whether genuine or "forged" is not something this selector can ever
    know without a real signing verification step it does not perform)
    claiming compauth=pass is trusted at face value — the adversarial
    proof's whole point is that a genuine FAIL, when present, can never
    be overridden by an added forgery, not that a lone header is
    distrusted."""
    headers = [_header("compauth=pass reason=100")]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS


# ---------------------------------------------------------------------
# ARC-Authentication-Results — the required, documented judgment call
# ---------------------------------------------------------------------


def test_arc_header_alone_with_no_plain_authentication_results_is_unknown():
    """Real, legitimate for inter-org-forwarded mail — but this
    selector's own conservative, documented default: ARC alone, no
    compauth, no direct dmarc on a plain header -> UNKNOWN, never
    trusted on its own."""
    headers = [_header("spf=pass; dkim=pass; dmarc=pass action=none; compauth=pass reason=100", name="ARC-Authentication-Results")]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN
    assert result.evidence["arc_header_count"] == 1
    # ARC tokens are still captured as supporting evidence — never
    # discarded, just never trusted to gate the verdict on their own.
    assert result.evidence["arc_tokens"]["compauth"] == "pass"


def test_arc_header_never_overrides_a_genuine_plain_header_fail():
    """A forged ARC header claiming pass must never override a genuine
    plain Authentication-Results header's own real fail — ARC tokens
    never contribute to the verdict at all in this selector."""
    headers = [
        _header("compauth=fail reason=001"),  # genuine, plain, trusted
        _header("compauth=pass reason=100", name="ARC-Authentication-Results"),  # never trusted
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_case_insensitive_header_name_matching():
    """Graph/most MTAs are inconsistent about header-name casing (mirrors
    the pre-existing doctrine this module supersedes for gating
    purposes) — matched case-insensitively."""
    headers = [_header("compauth=pass reason=100", name="AUTHENTICATION-RESULTS")]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS
