"""``assess_gmail_authentication`` — the provider-neutral trust-boundary
selector for BAGMAN's two Gmail mailboxes (CD-6 GUI-operations-
foundation follow-on WO — third mailbox provider).

******************************************************************
* DELIBERATELY PROVISIONAL — READ BEFORE TREATING THIS AS FINAL. *
******************************************************************

No real Gmail message header samples exist at the time this module is
written — no live Google Cloud OAuth credentials have been provisioned
yet (see `services/mailbox/gmail/secrets.py`'s own module docstring:
neither `client_id` nor `client_secret` exists on disk on the
production host yet, and no per-mailbox token has ever been minted).
Unlike `services.mailbox.microsoft.authentication
.assess_microsoft_authentication` (built against 8 REAL, live, redacted
diagnostic `Authentication-Results` samples Microsoft Graph actually
returned), this module is built ENTIRELY BLIND — from the general
`Authentication-Results` (RFC 8601) header shape any compliant MTA
(including Google's own receiving MTA) is expected to produce, not from
anything Gmail's own infrastructure actually stamps in practice for a
real message landing in one of these two accounts.

**The PL will run a live, redacted diagnostic against real Gmail API
messages once real credentials are provisioned (the same spirit as the
Microsoft WO's own PID §"live diagnostic" writeup, and the IMAP
delivery's own `assess_imap_authentication` precedent) and MAY REPLACE
THIS FUNCTION'S INTERNAL LOGIC ENTIRELY based on that real evidence. Do
not treat this implementation as the final trust-boundary design. Do not
build anything downstream that assumes this module's specific (currently
non-existent) selection strategy is permanent.**

Why this module ALWAYS returns UNKNOWN — no content-based trust signal
------------------------------------------------------------------------
Exactly the same reasoning `services.mailbox.imap.authentication
.assess_imap_authentication`'s own module docstring documents in full
(including the concrete, real adversarial finding that PROVED an
earlier, content-trusting version of THAT module false — read that
docstring in full; it is not repeated here) applies here, for the same
underlying reason: a Gmail message's `Authentication-Results` header
VALUE — including any `authserv-id` substring that merely CLAIMS to be
`mx.google.com`/`gmail.com` — is attacker-controlled content on the
wire. Nothing stops a sender from composing their own message containing
a literal, fully forged `Authentication-Results: mx.google.com;
dmarc=pass` header themselves; Gmail's own receiving infrastructure's
real hostname convention is public knowledge, exactly like
`mail.noust.ai` was for the IMAP module. Per the SAME architect
instruction the IMAP module already follows ("if you cannot determine
with confidence which header (if any) is genuinely stamped by the
receiving server without real samples, default to returning
AUTH_ASSESSMENT_UNKNOWN rather than guessing"): this module inspects
`Authentication-Results`/`ARC-Authentication-Results` headers ONLY to
report bounded, non-gating diagnostic counts in `evidence` (useful raw
material for the PL's own upcoming live diagnostic), and NEVER uses
their content to produce a PASS or FAIL verdict. **Every call currently
returns UNKNOWN, unconditionally**, until the PL's live diagnostic
establishes a real, structural (never content-based) trust boundary —
for Gmail, likely which HOP position within the message's own header
block Google's real receiving MTA writes its own
`Authentication-Results` header at (mirrors
`assess_microsoft_authentication`'s own "position, not content" proof
for Microsoft Graph) — and replaces this function's internal logic
entirely.

Structural difference from the IMAP selector, noted for the PL's future
diagnostic
------------------------------------------------------------------------
Unlike IMAP (`services.mailbox.imap.imap_client.uid_fetch_headers`
returns the raw MIME header BLOCK, preserving the message's own true
header ORDER exactly as the wire bytes carried it — the property a
position-based selector would need), Gmail's own `payload.headers` array
(from `format=metadata`) is API-CONSTRUCTED — Google's own JSON
representation of the parsed message, not a byte-for-byte header-block
echo, and this delivery has not verified (no live samples exist) whether
Gmail's own `payload.headers` array preserves the ORIGINAL wire header
order faithfully enough to support a Microsoft-style "first
Authentication-Results header wins" positional proof, or whether Gmail
guarantees anything at all about relative header ordering in that
representation. The PL's own live diagnostic must establish this before
attempting a position-based selector for Gmail — do not assume order is
preserved merely because it worked for Microsoft/IMAP.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_UNKNOWN, AuthenticationAssessment

_AUTHENTICATION_RESULTS_HEADER_NAMES = frozenset({"authentication-results", "arc-authentication-results"})

#: Mirrors `services.mailbox.imap.authentication._TOKEN_PATTERN`'s own
#: bounded, non-exhaustive RFC 8601 token grammar — kept here ONLY to
#: populate `evidence` with diagnostic mechanism/verdict counts (never to
#: gate a verdict on — see module docstring). Provider-independent (RFC
#: 8601's own token shape).
_TOKEN_PATTERN = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-zA-Z]+)")


def _parse_tokens(value: str) -> dict[str, str]:
    """Diagnostic-only extraction — see module docstring: this module
    never gates on these tokens' content."""
    parsed: dict[str, str] = {}
    for mechanism, verdict in _TOKEN_PATTERN.findall(value):
        parsed[mechanism.lower()] = verdict.lower()
    return parsed


def _authserv_id(value: str) -> Optional[str]:
    """The RFC 8601 `authserv-id` is everything before the first `;` in
    the header VALUE, trimmed. Diagnostic-only (see module docstring) —
    never used to decide trust, since this substring is entirely
    attacker-controlled content."""
    if ";" not in value:
        return None
    return value.split(";", 1)[0].strip()


def _header_name(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("name") or "").strip().lower()


def _header_value(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("value") or "")


def assess_gmail_authentication(raw_headers: Optional[Sequence[Mapping[str, Optional[str]]]]) -> AuthenticationAssessment:
    """PROVISIONAL — see module docstring. ALWAYS returns
    `AUTH_ASSESSMENT_UNKNOWN`: no `Authentication-Results`/
    `ARC-Authentication-Results` header content is used to gate a
    PASS/FAIL verdict, because this module has no structural
    (position/origin) proof of which header — if any — was genuinely
    added by Gmail's own receiving infrastructure, and header CONTENT
    (including an `authserv-id` that merely claims to be
    self-referential) is attacker-controlled and trivially forgeable.

    `raw_headers` is the raw header list
    `services.mailbox.gmail.gmail_client.GmailMessageMetadata.raw_headers`/
    `GmailMessageHeadersResult.raw_headers` carries (`[{"name": ...,
    "value": ...}, ...]`) — Gmail's own `payload.headers` array, order as
    Gmail's API returned it (see module docstring's own "structural
    difference" note on why this delivery does NOT assume that order is
    meaningful). The diagnostic fields in `evidence` (below) report what
    is PRESENT in the headers — never what is TRUSTED — purely to give
    the PL's own upcoming live diagnostic real, already-observed shape to
    work from.
    """
    headers = raw_headers or ()

    matching_values = [_header_value(h) for h in headers if _header_name(h) in _AUTHENTICATION_RESULTS_HEADER_NAMES]

    observed_authserv_ids = sorted({aid for aid in (_authserv_id(v) for v in matching_values) if aid})
    observed_tokens: dict[str, set[str]] = {}
    for value in matching_values:
        for mechanism, verdict in _parse_tokens(value).items():
            observed_tokens.setdefault(mechanism, set()).add(verdict)

    evidence: dict[str, Any] = {
        "authentication_results_header_count": len(matching_values),
        "observed_authserv_ids": observed_authserv_ids,
        "observed_tokens_by_mechanism": {k: sorted(v) for k, v in observed_tokens.items()},
        "provisional": True,
    }

    return AuthenticationAssessment(
        verdict=AUTH_ASSESSMENT_UNKNOWN,
        reason=(
            "PROVISIONAL selector: no live Gmail diagnostic has established a genuine, structural "
            "trust boundary yet, so no Authentication-Results/ARC-Authentication-Results header content "
            "is trusted for gating — always UNKNOWN until the PL replaces this function's internal logic "
            "with a real, evidence-based selector"
        ),
        evidence=evidence,
    )
