"""``assess_gmail_authentication`` — the real Gmail-specific trust-
boundary selector (CD-6 GUI-operations-foundation follow-on WO — live
Gmail security acceptance test). Consumes the raw header list Gmail's
API returns (``payload.headers``: ``[{"name": ..., "value": ...}, ...]``,
order preserved — see "Evidence chronology" step 3 below) and produces a
provider-neutral
:class:`services.mailbox.authentication_assessment.AuthenticationAssessment`.

This module follows the SAME code shape/conventions/doctrine already
established by
:func:`services.mailbox.microsoft.authentication.assess_microsoft_authentication`
and :func:`services.mailbox.imap.authentication.assess_imap_authentication`:
a bounded, non-exhaustive ``mechanism=verdict`` token regex; a
``_VERDICT_RANK``/``_verdict_rank`` worst-verdict-wins intra-header
tie-break; and a hard rule that tokens are NEVER merged across separate
headers for gating purposes — this is the same security lesson already
learned from Microsoft's cross-header merge defect (see that module's
own docstring, "ONLY the selected final-hop header gates"), restated
here as a hard requirement for Gmail too.

Evidence chronology — how this selector came to be trusted
------------------------------------------------------------------------
1. Initial implementation was deliberately UNKNOWN-only: no live Gmail
   samples existed yet, so no content- or structure-based trust
   boundary could be defended.
2. Five natural, real Gmail-delivered samples established a stable,
   consistent header structure: topmost Google-internal ``Received`` ->
   genuine ARC triple (``ARC-Seal``/``ARC-Message-Signature``/
   ``ARC-Authentication-Results``) -> genuine external
   ``Received ... by mx.google.com`` -> genuine
   ``Authentication-Results`` -> the sender's own ``DKIM-Signature``/
   ``Received``.
3. A metadata-vs-raw-MIME comparison on one real sample proved the
   Gmail API's ``format=metadata`` mode preserves true wire header
   order (filtered to the requested headers, never reordered) — the
   structural property this selector's positional check (criterion 3
   below) depends on.
4. A controlled, live forged-header canary ("Canary A" — a message
   carrying only a forged ``Authentication-Results: mx.google.com; ...``
   header) proved Gmail actively STRIPS a sender-forged plain
   ``Authentication-Results`` header claiming its own ``mx.google.com``
   authserv-id: the forged header does not survive delivery at all, and
   exactly one genuine ``Authentication-Results`` header remains.
5. A second controlled canary ("Canary B" — a message carrying a forged
   ``Received``, a forged ``Authentication-Results``, and a forged ARC
   pair, all naming ``mx.google.com``/``d=google.com``) proved that
   forged ``Received``/ARC content CAN survive delivery (unlike the
   plain ``Authentication-Results`` header, which was again fully
   stripped) — but it always remains positioned BELOW Gmail's own
   genuine receiving structure, never able to appear above it.
   Therefore ``Received``/ARC content or position ALONE can never be
   trusted as an authority; only the STRIPPING behaviour of the plain
   ``Authentication-Results`` header (step 4) is a usable trust signal.
6. This selector now trusts exactly the bounded Gmail plain-
   ``Authentication-Results`` boundary implemented below: an empirical,
   provider-specific trust decision grounded in the two live canaries
   above — NOT a generic RFC assumption, and NOT something to broaden
   (e.g. to other ``*.google.com`` hostnames, or to ``ARC-*`` headers)
   without further live evidence.

Why the plain ``Authentication-Results`` header is the only usable
signal
------------------------------------------------------------------------
Gmail actively strips any sender-forged instance of a plain
``Authentication-Results`` header that claims its own ``mx.google.com``
authserv-id — Canary A proved this directly: the forged header simply
does not exist in the delivered message. This guarantees AT MOST ONE
such header can ever be present after delivery, and it is always
genuine. ``Received`` and ARC headers carry no equivalent guarantee —
Canary B proved forged instances of both CAN survive delivery — so
their content and position are never trusted for gating; see "ARC —
evidence-only, never gates" below.

1. Selector eligibility
------------------------------------------------------------------------
A candidate header is eligible to gate ONLY if ALL of the following
hold:

1. Header name is exactly ``Authentication-Results`` (case-insensitive)
   — never ``ARC-Authentication-Results``/``ARC-Seal``/
   ``ARC-Message-Signature``.
2. Its ``authserv-id`` (everything before the first ``;`` in the header
   value, trimmed) normalizes EXACTLY to ``mx.google.com`` — a
   case-insensitive hostname comparison, but otherwise exact string
   equality. ``mx.google.com.invalid``/``evil.mx.google.com``/
   ``mx.google.com.example`` do NOT match — this comparison is a
   trimmed, lowercased equality check, never a substring/prefix/suffix
   test (see :func:`_normalized_authserv_id`).
3. It occupies the empirically-proven Gmail final-receiving position:
   scanning backward from this ``Authentication-Results`` header
   through the full header list (wire order), the closest preceding
   header that is itself a ``Received`` header must have a ``by``
   clause whose host is exactly ``mx.google.com`` (same exact-match
   discipline as criterion 2 — see :func:`_received_by_host`). If no
   such preceding ``Received`` header exists, or its ``by`` host cannot
   be parsed with confidence, the candidate is NOT eligible (fail
   closed).
4. If MORE THAN ONE header in the full header list independently
   satisfies criteria 1-3, the message has NO eligible candidate — this
   is explicit, tested ambiguity, never resolved by picking first/last.

2. Ambiguity rule — fail closed
------------------------------------------------------------------------
No eligible candidate (zero, or more than one) -> ``AUTH_ASSESSMENT_UNKNOWN``.
Never guess.

3. DMARC-only gating on the ONE eligible header
------------------------------------------------------------------------
Once exactly one eligible header is identified, this selector parses
ONLY that header's own mechanism tokens (see :func:`_parse_tokens`) —
never merging tokens across headers. A duplicate ``dmarc=`` token
within the one eligible header is tie-broken with the same
``_VERDICT_RANK``/worst-verdict-wins rule Microsoft's own selector
uses.

* ``dmarc=pass`` -> ``PASS``.
* ``dmarc=fail`` -> ``FAIL``.
* No ``dmarc=`` token at all, or an unrecognized/non-committal value
  (``none``/``temperror``/``permerror``/anything else) -> ``UNKNOWN``.
* ``spf``/``dkim`` tokens on the eligible header are NEVER
  independently sufficient for ``PASS``, and an SPF/DKIM failure never
  overrides a genuine ``dmarc=pass`` — DMARC is the sole gating
  mechanism once eligibility is established.

4. ARC — evidence-only, never gates
------------------------------------------------------------------------
``ARC-Seal``/``ARC-Message-Signature``/``ARC-Authentication-Results``
content (including a claimed ``d=google.com``) never influences the
verdict, per the Canary B finding above. A genuine ``arc=`` token that
may appear INSIDE the eligible plain ``Authentication-Results`` header
itself (e.g. ``arc=fail (arc missing headers)``, genuinely observed in
Canary B) is captured in ``evidence`` as supporting/diagnostic
information only.

5. Other (non-eligible) ``Authentication-Results`` headers —
   evidence-only
------------------------------------------------------------------------
Any ``Authentication-Results`` header that does not satisfy every part
of section 1 is evidence-only — its tokens are never merged into or
allowed to influence the gating verdict, even if it also claims
``mx.google.com`` as authserv-id or carries a more "favorable" verdict.

6. ``evidence`` — bounded, structured facts only
------------------------------------------------------------------------
Never a raw header dump — only bounded, already-parsed facts: total
``Authentication-Results`` header count, eligible-candidate count
(0/1/>1), the selected header's own authserv-id and parsed tokens when
exactly one eligible candidate exists, the count and merged
(evidence-only) tokens of every other (non-eligible)
``Authentication-Results`` header, ARC header counts plus parsed
``ARC-Authentication-Results`` tokens (evidence-only, never gating),
and a short ``selector_reason`` structural string explaining why
UNKNOWN was returned when it was.
"""
from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from services.mailbox.authentication_assessment import (
    AUTH_ASSESSMENT_FAIL,
    AUTH_ASSESSMENT_PASS,
    AUTH_ASSESSMENT_UNKNOWN,
    AuthenticationAssessment,
)

#: Matched case-insensitively — see module docstring criterion 1. Never
#: matches `ARC-Authentication-Results`/`ARC-Seal`/`ARC-Message-Signature`.
_AUTHENTICATION_RESULTS_HEADER_NAME = "authentication-results"
_ARC_SEAL_HEADER_NAME = "arc-seal"
_ARC_MESSAGE_SIGNATURE_HEADER_NAME = "arc-message-signature"
_ARC_AUTHENTICATION_RESULTS_HEADER_NAME = "arc-authentication-results"
_RECEIVED_HEADER_NAME = "received"

#: The ONE trusted authserv-id, proven by the live Canary A/B tests (see
#: module docstring). Compared by trimmed, lowercased EXACT equality
#: only (:func:`_normalized_authserv_id`) — never `in`/`startswith`/
#: `endswith` — so `mx.google.com.invalid`/`evil.mx.google.com` can never
#: match. Do not broaden this without further live evidence.
_TRUSTED_AUTHSERV_ID = "mx.google.com"

#: Deliberately bounded, non-exhaustive token extraction (never a full
#: RFC 8601 parser) — mirrors `microsoft/authentication.py`'s own
#: `_TOKEN_PATTERN` (extended there with the Microsoft-proprietary
#: `compauth=` token); extended here with `arc=` so the genuine `arc=`
#: diagnostic token Gmail itself stamps inside the eligible header (see
#: Canary B) can be captured for evidence — `arc` is NEVER used for
#: gating (see "ARC — evidence-only" above).
_TOKEN_PATTERN = re.compile(r"\b(spf|dkim|dmarc|arc)\s*=\s*([a-zA-Z]+)")

#: Bounded, defensive `Received`-header `by`-clause extractor. RFC 5321
#: §4.4 trace-header syntax: `from <sender-info> by <receiver-host> with
#: <protocol> id <id> ...` — `by` is a required, well-defined clause,
#: though real-world ordering of the other clauses varies. This regex
#: only locates the token immediately following a `by` clause keyword;
#: it does not attempt to parse the rest of the header. If it cannot
#: find a confident match, the caller treats the candidate as NOT
#: eligible (fail closed — see module docstring criterion 3).
_RECEIVED_BY_HOST_PATTERN = re.compile(r"(?i)\bby\s+([^\s;()]+)")

#: Conservative same-mechanism tie-break rank — LOWER rank wins (is
#: kept) when the same mechanism appears more than once WITHIN ONE
#: header value. Mirrors `microsoft/authentication.py`'s own
#: `_VERDICT_RANK` exactly (same rank ordering).
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
    """Parse every `mechanism=verdict` token in ONE header value,
    applying the conservative worst-verdict-wins tie-break
    (:data:`_VERDICT_RANK`) when the SAME mechanism appears more than
    once WITHIN this one value. Purely an intra-header concern — see
    module docstring; callers never merge this across multiple header
    values for gating purposes."""
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
    `evidence` (non-eligible `Authentication-Results` headers, and
    `ARC-Authentication-Results` headers). NEVER used for gating."""
    merged: dict[str, str] = {}
    for value in values:
        for mechanism, verdict in _parse_tokens(value).items():
            if mechanism not in merged or _verdict_rank(verdict) < _verdict_rank(merged[mechanism]):
                merged[mechanism] = verdict
    return merged


def _normalized_authserv_id(value: str) -> Optional[str]:
    """The RFC 8601 `authserv-id` is everything before the first `;` in
    the header VALUE, trimmed. Returns the trimmed, LOWERCASED value (for
    exact-equality comparison only — see module docstring criterion 2),
    or `None` for a value with no `;` at all."""
    if ";" not in value:
        return None
    authserv_id = value.split(";", 1)[0].strip()
    return authserv_id.lower() if authserv_id else None


def _received_by_host(value: str) -> Optional[str]:
    """Extract the host token immediately following a `Received` header's
    `by` clause (see :data:`_RECEIVED_BY_HOST_PATTERN`). Returns the
    trimmed, lowercased host, or `None` when no confident match is
    found (the caller treats that as NOT eligible — fail closed)."""
    match = _RECEIVED_BY_HOST_PATTERN.search(value)
    if not match:
        return None
    host = match.group(1).strip().strip(".,;").lower()
    return host or None


def _header_name(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("name") or "").strip().lower()


def _header_value(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("value") or "")


def assess_gmail_authentication(
    raw_headers: Optional[Sequence[Mapping[str, Optional[str]]]],
) -> AuthenticationAssessment:
    """The real Gmail-specific selector. `raw_headers` is the raw
    `payload.headers` list Gmail's API returns (order preserved, exactly
    as Gmail returned it — see module docstring's "Evidence chronology"
    step 3 for why this order is trusted for the positional check
    below).
    """
    headers = list(raw_headers or ())

    authentication_results: List[Tuple[int, str]] = [
        (idx, _header_value(h)) for idx, h in enumerate(headers) if _header_name(h) == _AUTHENTICATION_RESULTS_HEADER_NAME
    ]

    arc_seal_count = sum(1 for h in headers if _header_name(h) == _ARC_SEAL_HEADER_NAME)
    arc_message_signature_count = sum(1 for h in headers if _header_name(h) == _ARC_MESSAGE_SIGNATURE_HEADER_NAME)
    arc_authentication_results_values = [_header_value(h) for h in headers if _header_name(h) == _ARC_AUTHENTICATION_RESULTS_HEADER_NAME]
    arc_authentication_results_tokens = (
        _merge_tokens_for_evidence_only(arc_authentication_results_values) if arc_authentication_results_values else {}
    )

    eligible: List[Tuple[int, str, str]] = []
    ineligible_values: List[str] = []
    malformed_received_by_host_seen = False

    for idx, value in authentication_results:
        authserv_id = _normalized_authserv_id(value)
        if authserv_id != _TRUSTED_AUTHSERV_ID:
            ineligible_values.append(value)
            continue

        preceding_received_value: Optional[str] = None
        for j in range(idx - 1, -1, -1):
            if _header_name(headers[j]) == _RECEIVED_HEADER_NAME:
                preceding_received_value = _header_value(headers[j])
                break

        if preceding_received_value is None:
            ineligible_values.append(value)
            continue

        by_host = _received_by_host(preceding_received_value)
        if by_host is None:
            malformed_received_by_host_seen = True
            ineligible_values.append(value)
            continue

        if by_host != _TRUSTED_AUTHSERV_ID:
            ineligible_values.append(value)
            continue

        eligible.append((idx, value, authserv_id))

    additional_tokens = _merge_tokens_for_evidence_only(ineligible_values) if ineligible_values else {}

    evidence: dict[str, Any] = {
        "authentication_results_header_count": len(authentication_results),
        "eligible_candidate_count": len(eligible),
        "selected_header_authserv_id": None,
        "selected_header_tokens": {},
        "additional_authentication_results_header_count": len(ineligible_values),
        "additional_authentication_results_header_tokens": additional_tokens,
        "arc_seal_header_count": arc_seal_count,
        "arc_message_signature_header_count": arc_message_signature_count,
        "arc_authentication_results_header_count": len(arc_authentication_results_values),
        "arc_authentication_results_tokens": arc_authentication_results_tokens,
    }

    if not eligible:
        selector_reason = "malformed_received_by_host" if malformed_received_by_host_seen else "no_eligible_candidate"
        evidence["selector_reason"] = selector_reason
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_UNKNOWN,
            reason=(
                "no Authentication-Results header satisfied every eligibility criterion "
                f"(authserv-id == {_TRUSTED_AUTHSERV_ID!r}, positioned directly after a genuine "
                f"Received ... by {_TRUSTED_AUTHSERV_ID} hop) — selector_reason={selector_reason!r}"
            ),
            evidence=evidence,
        )

    if len(eligible) > 1:
        evidence["selector_reason"] = "ambiguous_multiple_candidates"
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_UNKNOWN,
            reason=(
                f"{len(eligible)} Authentication-Results headers each independently satisfied every "
                "eligibility criterion — this ambiguity is never resolved by picking first/last"
            ),
            evidence=evidence,
        )

    _idx, selected_value, selected_authserv_id = eligible[0]
    selected_tokens = _parse_tokens(selected_value)
    evidence["selected_header_authserv_id"] = selected_authserv_id
    evidence["selected_header_tokens"] = dict(selected_tokens)

    dmarc = selected_tokens.get("dmarc")

    if dmarc == "pass":
        evidence["selector_reason"] = "dmarc_pass"
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_PASS,
            reason="the sole eligible Authentication-Results header carries dmarc=pass",
            evidence=evidence,
        )
    if dmarc == "fail":
        evidence["selector_reason"] = "dmarc_fail"
        return AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_FAIL,
            reason="the sole eligible Authentication-Results header carries dmarc=fail",
            evidence=evidence,
        )

    selector_reason = "missing_dmarc_token" if dmarc is None else "unrecognized_dmarc_verdict"
    evidence["selector_reason"] = selector_reason
    return AuthenticationAssessment(
        verdict=AUTH_ASSESSMENT_UNKNOWN,
        reason=(
            f"the sole eligible Authentication-Results header carries no decisive dmarc verdict "
            f"(dmarc={dmarc!r}) — spf/dkim tokens are never independently sufficient for a verdict"
        ),
        evidence=evidence,
    )
