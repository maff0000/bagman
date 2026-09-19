"""``assess_imap_authentication`` — the provider-neutral trust-boundary
selector for the plain-IMAP `matt@noust.ai` mailbox (CD-6
GUI-operations-foundation follow-on WO — second mailbox provider).

******************************************************************
* DELIBERATELY PROVISIONAL — READ BEFORE TREATING THIS AS FINAL. *
******************************************************************

No real header samples from `mail.noust.ai` exist at the time this
module is written — no live IMAP credentials have been provisioned yet
(see `services/mailbox/imap/secrets.py`'s own module docstring: neither
`username` nor `password` exists on disk on the production host yet).
Unlike `services.mailbox.microsoft.authentication.assess_microsoft_authentication`
(built against 8 REAL, live, redacted diagnostic `Authentication-Results`
samples Microsoft Graph actually returned), this module is built
BLIND — from the general `Authentication-Results` (RFC 8601) header
shape any compliant MTA is expected to produce, not from anything
`mail.noust.ai`'s own Postfix/Exim/whatever-it-runs actually stamps in
practice.

**The PL will run a live, redacted diagnostic against real
`mail.noust.ai` messages after credentials are provisioned (see
`scripts/imap_capability_probe.py` and — separately, once real message
samples exist — a follow-up diagnostic script in the same spirit as the
Microsoft WO's own PID §"live diagnostic" writeup) and MAY REPLACE THIS
FUNCTION'S INTERNAL LOGIC ENTIRELY based on that real evidence. Do not
treat this implementation as the final trust-boundary design. Do not
build anything downstream that assumes this module's specific selection
strategy is permanent.**

The trust-boundary problem this module has to solve, generically
------------------------------------------------------------------------
`mail.noust.ai` is a SELF-HOSTED server — there is no third-party mail
platform (like Microsoft 365) whose edge stamps one recognisable,
hard-to-forge proprietary verdict token (Microsoft's own `compauth=`).
For a self-hosted server, the ONE genuine trust boundary is: which
`Authentication-Results` (or `Received`) header was added by
`mail.noust.ai`'s OWN receiving MTA, as opposed to a header an external,
untrusted sender could simply have included in the message they sent
(headers are attacker-controlled input on the wire; a spoofer can put
ANY header, including a fake `Authentication-Results: ... dmarc=pass`,
into a message they compose). This is EXACTLY the same class of problem
`assess_microsoft_authentication` solves for Microsoft (see that
module's own docstring, "ONLY the plain `Authentication-Results` header
name is ever treated as a trust candidate... the FIRST plain
`Authentication-Results` header... is the final-hop, genuinely-stamped
one") — the general PRINCIPLE transfers directly: identify which header
the receiving server itself added (the one hop `mail.noust.ai` controls),
trust ONLY that one for gating, and never a header an attacker could
have injected.

Why this module ALWAYS returns UNKNOWN — no content-based trust signal
------------------------------------------------------------------------
**PL correction (pre-live-diagnostic adversarial review caught a real,
live-exploitable gap in an earlier version of this module — recorded
here since it is exactly the class of mistake this delivery has been
built to catch, not to repeat silently).** An earlier version of this
function trusted any `Authentication-Results` header whose `authserv-id`
token TEXT matched a small hint set (`mail.noust.ai`/`noust.ai`). That is
UNSOUND and was a genuine security bug, not a defensible conservative
default: the header VALUE — including its `authserv-id` substring — is
attacker-controlled input on the wire. `mail.noust.ai` is PUBLIC
information (it is this domain's own MX record); nothing stops a sender
from composing their own message containing the literal header
`Authentication-Results: mail.noust.ai; dmarc=pass` themselves. Trusting
header CONTENT that merely claims to be self-referential provides
**zero** actual security — proven directly:
```python
assess_imap_authentication([
    {"name": "Authentication-Results", "value": "mail.noust.ai; dmarc=pass"},
])  # the OLD code returned PASS here — a fully attacker-forged header,
    # with NO genuine mail.noust.ai-stamped header present at all.
```
A trustworthy selector needs a STRUCTURAL signal a sender cannot forge —
for Microsoft, that signal is Graph's own PROVEN behaviour of always
prepending its own hop's header ahead of any earlier one (see
`assess_microsoft_authentication`'s own module docstring). This module
has NO equivalent proof for `mail.noust.ai`'s own MTA yet — no live
diagnostic has been run (see `scripts/imap_capability_probe.py`; a
follow-up header-diagnostic script the PL will run once credentials
exist). Per the architect's own explicit instruction: "if you cannot
determine with confidence which header (if any) is genuinely stamped by
the receiving server without real samples, default to returning
AUTH_ASSESSMENT_UNKNOWN rather than guessing" — this module now follows
that literally: it inspects `Authentication-Results` headers ONLY to
report bounded, non-gating diagnostic counts in `evidence` (useful input
for the PL's own upcoming live diagnostic), and NEVER uses their content
to produce a PASS or FAIL verdict. **Every call currently returns
UNKNOWN, unconditionally**, until the PL's live diagnostic establishes a
real, structural (never content-based) trust boundary and replaces this
function's internal logic entirely.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from services.mailbox.authentication_assessment import (
    AUTH_ASSESSMENT_UNKNOWN,
    AuthenticationAssessment,
)

_AUTHENTICATION_RESULTS_HEADER_NAME = "authentication-results"

#: Mirrors `services.mailbox.microsoft.authentication._TOKEN_PATTERN`'s
#: own bounded, non-exhaustive RFC 8601 token grammar — kept here ONLY
#: to populate `evidence` with diagnostic mechanism/verdict counts (never
#: to gate a verdict on — see module docstring). Provider-independent
#: (RFC 8601's own token shape), so correct to reuse without depending on
#: the Microsoft package at runtime.
_TOKEN_PATTERN = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-zA-Z]+)")


def _parse_tokens(value: str) -> dict[str, str]:
    """Diagnostic-only extraction — see module docstring: this module
    never gates on these tokens' content, since the header they come from
    cannot yet be structurally distinguished from attacker-supplied text.
    Kept (rather than deleted) purely so `evidence` can report what a
    header CLAIMED, for the PL's own upcoming live diagnostic to compare
    against reality."""
    parsed: dict[str, str] = {}
    for mechanism, verdict in _TOKEN_PATTERN.findall(value):
        parsed[mechanism.lower()] = verdict.lower()
    return parsed


def _authserv_id(value: str) -> Optional[str]:
    """The RFC 8601 `authserv-id` is everything before the first `;` in
    the header VALUE, trimmed. Returns `None` for a value with no `;` at
    all. Diagnostic-only (see module docstring) — never used to decide
    trust, since this substring is entirely attacker-controlled content."""
    if ";" not in value:
        return None
    return value.split(";", 1)[0].strip()


def _header_name(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("name") or "").strip().lower()


def _header_value(header: Mapping[str, Optional[str]]) -> str:
    return str(header.get("value") or "")


def assess_imap_authentication(
    raw_headers: Optional[Sequence[Mapping[str, Optional[str]]]],
) -> AuthenticationAssessment:
    """PROVISIONAL — see module docstring. ALWAYS returns
    `AUTH_ASSESSMENT_UNKNOWN`: no `Authentication-Results` header content
    is used to gate a PASS/FAIL verdict, because this module has no
    structural (position/origin) proof of which header — if any — was
    genuinely added by `mail.noust.ai`'s own receiving MTA, and header
    CONTENT (including an `authserv-id` that merely claims to be
    self-referential) is attacker-controlled and trivially forgeable —
    see module docstring for the concrete adversarial reproduction that
    proved an earlier, content-trusting version of this function false.

    `raw_headers` is the raw header list
    `services.mailbox.imap.imap_client.ImapMessageHeaders.raw_headers`/
    `ImapFetchHeadersResult` carries (`[{"name": ..., "value": ...}, ...]`,
    order preserved as the raw MIME header block's own order). The
    diagnostic fields in `evidence` (below) report what is PRESENT in the
    headers — never what is TRUSTED — purely to give the PL's own
    upcoming live diagnostic real, already-observed shape to work from."""
    headers = raw_headers or ()

    authentication_results_values = [
        _header_value(header) for header in headers if _header_name(header) == _AUTHENTICATION_RESULTS_HEADER_NAME
    ]

    observed_authserv_ids = sorted({aid for aid in (_authserv_id(v) for v in authentication_results_values) if aid})
    observed_tokens: dict[str, set[str]] = {}
    for value in authentication_results_values:
        for mechanism, verdict in _parse_tokens(value).items():
            observed_tokens.setdefault(mechanism, set()).add(verdict)

    evidence: dict[str, Any] = {
        "authentication_results_header_count": len(authentication_results_values),
        "observed_authserv_ids": observed_authserv_ids,
        "observed_tokens_by_mechanism": {k: sorted(v) for k, v in observed_tokens.items()},
        "provisional": True,
    }

    return AuthenticationAssessment(
        verdict=AUTH_ASSESSMENT_UNKNOWN,
        reason=(
            "PROVISIONAL selector: no live mail.noust.ai diagnostic has established a genuine, "
            "structural trust boundary yet, so no Authentication-Results header content is trusted "
            "for gating — always UNKNOWN until the PL replaces this function's internal logic with a "
            "real, evidence-based selector"
        ),
        evidence=evidence,
    )
