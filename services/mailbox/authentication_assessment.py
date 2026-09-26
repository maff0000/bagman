"""``AuthenticationAssessment`` — the provider-neutral PASS/FAIL/UNKNOWN
message-authentication verdict type (CD-6 GUI-operations-foundation
follow-on WO, item B: "provider-neutral authentication assessment with a
real Microsoft trust-boundary selector").

Why this is its own tiny, dependency-free module
------------------------------------------------------------------------
``services/mailbox/sweep.py`` is the provider-neutral sweep orchestration
engine (see that module's own docstring) — the architect's explicit
requirement is that this abstraction "must be reusable by Gmail and IMAP
later", so the TYPE every provider's own selector produces (and that
``sweep.py`` gates on) must live somewhere importable with zero
provider-specific code pulled in. This module is exactly that seam: it
imports nothing from ``services/mailbox/microsoft/`` (or any other
provider package) and never will. A concrete provider's own trust-
boundary selector (e.g. :func:`services.mailbox.microsoft.authentication
.assess_microsoft_authentication`) constructs this type; it never lives
here itself.

The three closed verdict values — what they mean
------------------------------------------------------------------------
* ``AUTH_ASSESSMENT_PASS`` — the message's authenticity for its visible
  From-domain is affirmatively proven, either by a trusted composite
  verdict (Microsoft's own ``compauth=pass``) or by a genuine
  ``dmarc=pass`` on a header this selector trusts. Only a PASS proceeds
  down the normal MIME-fetch-and-evidence path for a ``MUST_READ``
  source.
* ``AUTH_ASSESSMENT_FAIL`` — a genuine, decisive authentication failure
  (a trusted ``compauth=fail`` or ``dmarc=fail``) — a real security
  event, escalated exactly like a PASS is required, never silently
  dropped.
* ``AUTH_ASSESSMENT_UNKNOWN`` — no decisive, trustworthy verdict could be
  determined at all (no usable header, only an untrusted/ARC-only
  header, or a non-committal verdict like ``softpass``/``none``/
  ``bestguesspass``/``temperror``/``permerror``). Deliberately treated
  identically to FAIL by the sweep gate (see
  ``services/mailbox/sweep.py``'s own docstring) — an inconclusive
  result is never silently treated as trusted.

Never AI/ML — a bounded, deterministic, fully documented threshold
------------------------------------------------------------------------
Every value this type carries is produced by fixed, documented string
comparisons and a fixed, documented tie-break rank (see
``services.mailbox.microsoft.authentication`` for the real selector) —
never a model call, never a learned weight, never a confidence score
along a continuum. ``evidence`` exists purely so the underlying
provider-specific facts (raw spf/dkim/dmarc/compauth tokens, which
header(s) contributed, any conflict/ambiguity notes) are NEVER
discarded — a human/auditor can always see exactly what was seen and
why a verdict was reached — they are simply not the thing the policy
engine gates on directly (see module docstring above: the strongest
available signal — compauth/dmarc — gates; spf/dkim alone never do).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Closed, three-value provider-neutral verdict vocabulary.
AUTH_ASSESSMENT_PASS = "PASS"
AUTH_ASSESSMENT_FAIL = "FAIL"
AUTH_ASSESSMENT_UNKNOWN = "UNKNOWN"
AUTH_ASSESSMENT_VERDICTS = frozenset({AUTH_ASSESSMENT_PASS, AUTH_ASSESSMENT_FAIL, AUTH_ASSESSMENT_UNKNOWN})


@dataclass(frozen=True)
class AuthenticationAssessment:
    """The bounded, deterministic outcome of a provider's own
    trust-boundary selector — never raised as an exception, never
    anything but this one type.

    ``verdict`` is always one of :data:`AUTH_ASSESSMENT_VERDICTS`.
    ``reason`` is a short, human-readable explanation (never a fabricated
    or more specific claim than what the selector actually determined).
    ``evidence`` carries the underlying provider-specific supporting
    facts — never discarded, never itself gated on directly by a caller;
    see module docstring.
    """

    verdict: str
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict:
        """A small, JSON-serialisable snapshot suitable for persisting
        on ``MailboxMessage.metadata["auth_assessment"]`` — see
        ``services/mailbox/sweep.py``'s own module docstring
        ("Authentication escalation" section) for why the open,
        already-established ``metadata`` field is used rather than a new
        typed column (a documented, no-migration judgment call)."""
        return {"verdict": self.verdict, "reason": self.reason, "evidence": dict(self.evidence)}
