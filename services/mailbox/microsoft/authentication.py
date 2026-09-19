"""``assess_microsoft_authentication`` — the real Microsoft-specific
trust-boundary selector (CD-6 GUI-operations-foundation follow-on WO,
item B; corrected by a second architect review, Finding 2). Consumes
the RAW header list Microsoft Graph returns
(``internetMessageHeaders``: ``[{"name": ..., "value": ...}, ...]``) and
produces a provider-neutral
:class:`services.mailbox.authentication_assessment.AuthenticationAssessment`.

Built against the real, live, redacted diagnostic data (8 real Infosecurs
inbound messages) captured in this WO's own PID, not assumptions:

* Every one of the 8 real samples carried exactly ONE
  ``Authentication-Results`` header (zero ``ARC-Authentication-Results``
  observed) with the shape::

      spf=pass (sender IP is <redacted>) smtp.mailfrom=[REDACTED];
      dkim=pass (signature was verified) header.d=[REDACTED];
      dmarc=pass action=none header.from=[REDACTED];
      compauth=pass reason=100

* 2 of the 8 samples carried TWO separate ``dkim=`` tokens WITHIN THE
  SAME header value (the message was DKIM-signed by both the
  originating domain and a relay/ESP) — the confirmed real bug: the old
  parser's ``setdefault`` silently kept only the FIRST token, even
  within one legitimate header. This module fixes that bug class
  generally (see :func:`_parse_tokens`), not merely for the
  single-header case.

The trust model — why ``compauth`` is the strongest available signal
------------------------------------------------------------------------
``compauth=<verdict> reason=<code>`` is a genuine Microsoft-proprietary
COMPOSITE authentication verdict Microsoft's own inbound mail edge
stamps — an external sender cannot forge it before Microsoft's own edge
adds it. A header value that carries no ``compauth=`` token at all is
therefore treated as NOT Microsoft's own trusted stamp for the purposes
of this selector — its raw spf/dkim/dmarc tokens are still captured as
supporting evidence, but they never override a trusted header's own
verdict (architect's own explicit requirement).

ONLY the plain ``Authentication-Results`` header name is ever treated as
a trust candidate — ``ARC-Authentication-Results`` headers are real and
legitimate for inter-org-forwarded mail, but a spoofer could inject a
fake header under either name, and ARC sets are fundamentally about
attesting an EARLIER hop's own (already once-removed) authentication
state rather than this delivery's own final-hop verdict. This module's
own conservative, documented judgment call: ``ARC-Authentication-Results``
alone (no plain ``Authentication-Results`` header at all) is NEVER
treated as sufficiently trustworthy on its own — it resolves to
``AUTH_ASSESSMENT_UNKNOWN`` — its tokens are still captured as
supporting evidence only. A future delivery integrating a genuine
ARC-verifying provider could revisit this; nothing here assumes ARC
tokens are worthless, only that THIS selector does not trust them
unverified.

ONLY the selected final-hop header gates — corrected cross-header
merge model (Finding 2, second architect review)
------------------------------------------------------------------------
**This is a deliberate correction of this module's own FIRST cut, which
was a real, confirmed security bypass.** The original implementation
merged tokens across EVERY plain ``Authentication-Results`` header a
message carried, with a worst-verdict-wins tie-break — but that
tie-break only ever applies when TWO headers report the SAME mechanism.
When the genuine (first/topmost) header was SILENT on a mechanism (e.g.
it carried only ``dmarc=fail``, no ``compauth=`` token at all) and a
LATER, forged/untrusted header supplied ``compauth=pass``, there was
nothing for that value to "compete" against — it merged in unopposed
and won, because the top-level gating logic checked ``compauth ==
"pass"`` before ever looking at the genuine header's own real
``dmarc=fail``. Reproduced directly::

    headers = [
        {"name": "Authentication-Results", "value": "spf=fail; dkim=fail; dmarc=fail action=quarantine"},
        {"name": "Authentication-Results", "value": "compauth=pass reason=100"},
    ]
    assess_microsoft_authentication(headers).verdict  # was "PASS" -- WRONG. Now "FAIL".

The fix is simpler than the old cross-header-merge logic, not more
complex: Microsoft Graph preserves message-header ordering (a new hop's
own header is prepended ahead of any earlier hop's — the FIRST plain
``Authentication-Results`` header Graph returns is the final-hop,
most-recent, genuinely-Microsoft-stamped one for THIS delivery). This
selector now:

1. Locates the FIRST (topmost) plain ``Authentication-Results`` header
   in the raw, ordered header list — the "selected header". This ONE
   header is the sole trust boundary for gating.
2. Parses ALL mechanism tokens WITHIN the selected header's own value
   ONLY (see :func:`_parse_tokens`) — never looking at any other
   ``Authentication-Results`` header's tokens for gating purposes. The
   real, confirmed duplicate-``dkim=``-WITHIN-ONE-header case (see
   above) remains real and is still handled deterministically: the
   EXISTING conservative worst-verdict-wins tie-break
   (:data:`_VERDICT_RANK`/:func:`_verdict_rank`) still applies, but only
   for two tokens of the SAME mechanism found inside that ONE selected
   header's own value — this is an entirely intra-header concern,
   unaffected by the cross-header correction.
3. Gates on ONLY the selected header's own (possibly intra-header
   tie-broken) tokens: ``compauth`` -> ``dmarc`` waterfall (see "Never a
   bare SPF/DKIM pass alone" below).
4. EVERY OTHER plain ``Authentication-Results`` header (2nd, 3rd, ...)
   and EVERY ``ARC-Authentication-Results`` header are preserved as
   supporting/diagnostic evidence ONLY — their own tokens NEVER
   influence the gating verdict in any way. ARC was already never
   trusted for gating before this correction; what changes here is that
   SUBSEQUENT plain ``Authentication-Results`` headers are now ALSO
   excluded from gating, closing the exact bypass reproduced above.

``evidence`` distinguishes these three sources explicitly rather than
collapsing them into one ambiguous blob: ``selected_header_tokens`` (the
one header actually gated on), ``additional_authentication_results_header_count``
/ ``additional_authentication_results_header_tokens`` (every OTHER plain
header seen — untrusted, count + tokens kept only for a human/auditor),
and ``arc_header_count`` / ``arc_tokens`` (unchanged from before).

Never a bare SPF/DKIM pass alone
------------------------------------------------------------------------
This selector's gating waterfall is ``compauth`` -> ``dmarc`` only — spf/
dkim tokens are captured in ``evidence`` for a human/auditor, but NEVER
independently gate the verdict. This is deliberate: SPF/DKIM alone prove
a DIFFERENT domain authenticated, not necessarily the visible From
domain, unless DMARC's own alignment check has actually passed. The
architect's own named concern — "a DMARC PASS combined with an SPF FAIL
can currently be escalated merely because SPF contains fail, even though
DMARC may legitimately have passed through aligned DKIM" — is
structurally impossible to reproduce here: SPF is never consulted for
the verdict at all.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from services.mailbox.authentication_assessment import (
    AUTH_ASSESSMENT_FAIL,
    AUTH_ASSESSMENT_PASS,
    AUTH_ASSESSMENT_UNKNOWN,
    AuthenticationAssessment,
)

#: Matched case-insensitively — Graph/most MTAs are inconsistent about
#: header-name casing (mirrors the pre-existing `graph_client.py`
#: discipline this module supersedes for gating purposes).
_AUTHENTICATION_RESULTS_HEADER_NAME = "authentication-results"
_ARC_AUTHENTICATION_RESULTS_HEADER_NAME = "arc-authentication-results"

#: Deliberately bounded, non-exhaustive token extraction (never a full
#: RFC 8601 parser) — mirrors `graph_client._AUTH_RESULT_TOKEN_PATTERN`'s
#: own documented scope, extended to also capture the Microsoft-
#: proprietary `compauth=` token.
_TOKEN_PATTERN = re.compile(r"\b(spf|dkim|dmarc|compauth)\s*=\s*([a-zA-Z]+)")

#: Conservative same-mechanism tie-break rank — LOWER rank wins (is kept)
#: when the same mechanism appears more than once WITHIN ONE header
#: value (the real, confirmed multi-`dkim=`-in-one-header case). A
#: fail-shaped verdict is always more severe (lower rank, wins) than a
#: pass-shaped one; an ambiguous/non-committal verdict sits in between.
#: An unrecognised token value is treated as non-committal (never allowed
#: to silently win over a real, recognised fail or dominate a real pass).
_VERDICT_RANK: Mapping[str, int] = {
    "fail": 0,
    "reject": 0,
    "hardfail": 0,
    "softfail": 1,
    "softpass": 1,
    "neutral": 1,
    "none": 2,
    "temperror": 2,
    "permerror": 2,
    "bestguesspass": 2,
    "pass": 3,
}


def _verdict_rank(value: str) -> int:
    return _VERDICT_RANK.get(value.lower(), 1)


def _parse_tokens(value: str) -> dict[str, str]:
    """Parse every ``mechanism=verdict`` token in ONE header value,
    applying the conservative worst-verdict-wins tie-break
    (:data:`_VERDICT_RANK`) when the SAME mechanism appears more than
    once WITHIN this one value (the real, confirmed duplicate-``dkim=``
    case). This is a purely intra-header concern — see module docstring
    — callers never merge this across multiple header values for gating
    purposes."""
    parsed: dict[str, str] = {}
    for mechanism, verdict in _TOKEN_PATTERN.findall(value):
        mechanism = mechanism.lower()
        verdict = verdict.lower()
        if mechanism not in parsed or _verdict_rank(verdict) < _verdict_rank(parsed[mechanism]):
            parsed[mechanism] = verdict
    return parsed


def _merge_tokens_for_evidence_only(values: Sequence[str]) -> dict[str, str]:
    """Merge every value's own :func:`_parse_tokens` result across
    MULTIPLE header values — used ONLY to build supporting/diagnostic
    ``evidence`` for untrusted headers (every plain
    ``Authentication-Results`` header after the selected one, and every
    ``ARC-Authentication-Results`` header). NEVER used for gating — see
    module docstring's "ONLY the selected final-hop header gates"
    section for exactly why a cross-header merge must never feed the
    verdict."""
    merged: dict[str, str] = {}
    for value in values:
        for mechanism, verdict in _parse_tokens(value).items():
            if mechanism not in merged or _verdict_rank(verdict) < _verdict_rank(merged[mechanism]):
                merged[mechanism] = verdict
    return merged


def _header_name(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("name") or "").strip().lower()


def _header_value(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("value") or "")


def assess_microsoft_authentication(
    raw_headers: Optional[Sequence[Mapping[str, Optional[str]]]],
) -> AuthenticationAssessment:
    """The real Microsoft-specific selector. ``raw_headers`` is the raw
    ``internetMessageHeaders`` list Graph returns (order preserved,
    exactly as Graph returned it) — see module docstring for the full
    trust-boundary reasoning this implements.
    """
    headers = raw_headers or ()

    trusted_values = [_header_value(h) for h in headers if _header_name(h) == _AUTHENTICATION_RESULTS_HEADER_NAME]
    arc_values = [_header_value(h) for h in headers if _header_name(h) == _ARC_AUTHENTICATION_RESULTS_HEADER_NAME]

    # Supporting-evidence-only facts from ARC headers — captured, never
    # allowed to influence the verdict (see module docstring).
    arc_tokens = _merge_tokens_for_evidence_only(arc_values) if arc_values else {}

    if not trusted_values:
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_UNKNOWN,
            reason=(
                "no plain Authentication-Results header was present on this message — "
                "ARC-Authentication-Results alone is not treated as sufficiently trustworthy by this "
                "selector, and no other usable authentication header exists"
                if arc_values
                else "no Authentication-Results (or ARC-Authentication-Results) header was present on this message at all"
            ),
            evidence={
                "authentication_results_header_count": 0,
                "arc_header_count": len(arc_values),
                "arc_tokens": arc_tokens,
            },
        )

    # The FIRST plain Authentication-Results header is the sole trust
    # boundary for gating (Graph prepends each new hop's own header
    # ahead of earlier ones, so this is the genuine final-hop verdict
    # for THIS delivery — see module docstring). Every OTHER plain
    # header is untrusted, supporting evidence only.
    selected_value = trusted_values[0]
    untrusted_values = trusted_values[1:]

    selected_tokens = _parse_tokens(selected_value)
    untrusted_tokens = _merge_tokens_for_evidence_only(untrusted_values) if untrusted_values else {}

    compauth = selected_tokens.get("compauth")

    evidence: dict[str, Any] = {
        "authentication_results_header_count": len(trusted_values),
        "selected_header_tokens": dict(selected_tokens),
        "additional_authentication_results_header_count": len(untrusted_values),
        "additional_authentication_results_header_tokens": untrusted_tokens,
        "arc_header_count": len(arc_values),
        "arc_tokens": arc_tokens,
    }

    if compauth == "pass":
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_PASS,
            reason="trusted Microsoft composite-authentication verdict compauth=pass (selected header)",
            evidence=evidence,
        )
    if compauth == "fail":
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_FAIL,
            reason="trusted Microsoft composite-authentication verdict compauth=fail (selected header)",
            evidence=evidence,
        )

    # No decisive compauth (missing, softpass, none, or any other
    # non-pass/fail token) on the SELECTED header — fall through to the
    # DMARC tier, using ONLY the selected header's own tokens. spf/dkim
    # are deliberately never consulted here — see module docstring's
    # "Never a bare SPF/DKIM pass alone" section.
    dmarc = selected_tokens.get("dmarc")
    if dmarc == "pass":
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_PASS,
            reason=(
                f"no decisive compauth verdict ({compauth!r}) on the selected header; "
                "its own dmarc=pass is used instead"
            ),
            evidence=evidence,
        )
    if dmarc == "fail":
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_FAIL,
            reason=(
                f"no decisive compauth verdict ({compauth!r}) on the selected header; "
                "its own dmarc=fail is used instead"
            ),
            evidence=evidence,
        )

    return AuthenticationAssessment(
        verdict=AUTH_ASSESSMENT_UNKNOWN,
        reason=(
            f"neither a decisive compauth ({compauth!r}) nor dmarc ({dmarc!r}) verdict was found on the "
            "selected (final-hop) Authentication-Results header"
        ),
        evidence=evidence,
    )
