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
    regardless of which dkim token 'wins'). This is entirely an
    intra-header concern (both `dkim=` tokens are inside the ONE
    selected header), unaffected by the Finding 2 cross-header
    correction — see `result.evidence["selected_header_tokens"]`."""
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
    assert result.evidence["selected_header_tokens"]["dkim"] == "pass"


def test_real_diagnostic_shape_duplicate_dkim_token_worst_wins_when_they_disagree():
    """The generalised fix: when two `dkim=` tokens in the SAME header
    genuinely disagree, the conservative worst-verdict-wins rule applies
    — a mixed result is not an unambiguous pass. Here the SAME header
    still carries a real compauth=pass (an entirely independent
    Microsoft-stamped verdict, unaffected by the dkim disagreement), so
    the overall verdict is still PASS — but the underlying selected-
    header evidence must honestly reflect the WORSE dkim verdict, never
    the better one, so an auditor never sees a falsely-rosier picture of
    the raw facts."""
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
    assert result.evidence["selected_header_tokens"]["dkim"] == "fail"  # worst-wins evidence, honestly recorded


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
# CD-6 second architect review — Finding 2: the OLD cross-header-merge
# model was a real, confirmed security bypass (see
# `services.mailbox.microsoft.authentication` module docstring's
# "ONLY the selected final-hop header gates" section for the full
# reproduction and reasoning). The corrected model trusts ONLY the
# FIRST/topmost plain `Authentication-Results` header for gating —
# Microsoft Graph prepends each new hop's own header ahead of earlier
# ones, so that first header genuinely IS the final-hop, Microsoft-
# stamped verdict for THIS delivery. Every one of the four required
# adversarial cases below is the architect's own exact case, verbatim.
# ---------------------------------------------------------------------


def test_genuine_first_header_compauth_fail_beats_forged_later_compauth_pass():
    """Required case 1: genuine (first/selected) header compauth=fail +
    forged LATER header compauth=pass -> FAIL. The forged header is
    excluded from gating entirely (untrusted, evidence-only)."""
    headers = [
        _header("spf=fail; dkim=fail; dmarc=fail action=quarantine; compauth=fail reason=001"),  # genuine, selected
        _header("spf=pass; dkim=pass; dmarc=pass action=none; compauth=pass reason=100"),  # forged, untrusted
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL
    assert result.evidence["selected_header_tokens"]["compauth"] == "fail"
    assert result.evidence["additional_authentication_results_header_count"] == 1


def test_genuine_first_header_dmarc_fail_no_compauth_beats_forged_later_compauth_pass():
    """Required case 2 — THE key regression test: genuine (first/
    selected) header carries dmarc=fail with NO decisive compauth token
    at all, and a forged LATER header supplies compauth=pass -> FAIL.
    This is the exact bypass the OLD cross-header merge did NOT protect
    against (see module docstring's exact reproduction): with nothing
    on the genuine header for the forged compauth=pass to "compete"
    against under the old worst-wins-per-mechanism rule, it used to
    merge in unopposed and win. Under the corrected selected-header-only
    model, the forged header's compauth token is never even looked at
    for gating, so the genuine header's own real dmarc=fail is what
    decides the verdict."""
    headers = [
        _header("spf=fail; dkim=fail; dmarc=fail action=quarantine"),  # genuine, selected — no compauth at all
        _header("compauth=pass reason=100"),  # forged, untrusted
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL
    assert "compauth" not in result.evidence["selected_header_tokens"]
    assert result.evidence["selected_header_tokens"]["dmarc"] == "fail"
    assert result.evidence["additional_authentication_results_header_tokens"]["compauth"] == "pass"


def test_genuine_first_header_compauth_and_dmarc_none_with_forged_later_compauth_pass_is_unknown():
    """Required case 3: genuine (first/selected) header compauth=none;
    dmarc=none + forged LATER header compauth=pass -> UNKNOWN. Neither
    the non-committal genuine verdict nor the untrusted forged one can
    produce an affirmative PASS."""
    headers = [
        _header("compauth=none; dmarc=none"),  # genuine, selected — non-committal
        _header("compauth=pass reason=100"),  # forged, untrusted
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_UNKNOWN


def test_genuine_first_header_compauth_pass_is_not_downgraded_by_forged_later_compauth_fail():
    """Required case 4 — the mirror image: genuine (first/selected)
    header compauth=pass + forged LATER header compauth=fail -> PASS.
    The later untrusted header must not be able to force either a
    bypass OR a false security escalation — a forged FAIL appended after
    a genuine PASS must not trigger an unnecessary security review."""
    headers = [
        _header("spf=pass; dkim=pass; dmarc=pass action=none; compauth=pass reason=100"),  # genuine, selected
        _header("compauth=fail reason=001"),  # forged, untrusted
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_PASS
    assert result.evidence["additional_authentication_results_header_tokens"]["compauth"] == "fail"


def test_pl_exact_reproduction_snippet_from_wo_background_now_returns_fail():
    """Required case 7 — a dedicated regression test reproducing the PL's
    own exact reproduction snippet from this WO's background section,
    verbatim, proving the old cross-header-merge bypass is closed: this
    used to return PASS (wrong); it must now return FAIL."""
    headers = [
        {"name": "Authentication-Results", "value": "spf=fail; dkim=fail; dmarc=fail action=quarantine"},
        {"name": "Authentication-Results", "value": "compauth=pass reason=100"},
    ]
    result = assess_microsoft_authentication(headers)
    assert result.verdict == AUTH_ASSESSMENT_FAIL


def test_forged_header_alone_with_no_genuine_header_is_trusted_as_is():
    """Sanity check: a SINGLE header (whether genuine or "forged" is not
    something this selector can ever know without a real signing
    verification step it does not perform) claiming compauth=pass is
    trusted at face value — the adversarial proofs above are about a
    genuine header's own real verdict never being overridden by a LATER,
    untrusted one, not about a lone header being distrusted."""
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
