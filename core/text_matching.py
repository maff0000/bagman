"""Canonical BAGMAN text-normalisation and predicate-matching primitives
(CD-6 Slice 5 WI-2).

Extracted verbatim from ``services.mailbox.domain_rule`` (CD-6
architect amendment / GUI-operations-foundation follow-on WO), which
built and proved these five functions FIRST, against real mailbox
domain-policy data, before this module existed — see that module's own
docstring, "Normalization — reused, not re-implemented" section, for
the forward-declared intent this extraction fulfils. This is a pure
extraction, not a rewrite: every byte of behaviour is unchanged, and
``services.mailbox.domain_rule`` re-exports all five names so every
existing caller (across ``services/mailbox/*.py``,
``persistence/postgres/mailbox_domain_rule_repository.py``, and this
codebase's tests) keeps working with zero code changes at any call
site.

Why this module is neutral — no mailbox-specific concept here
------------------------------------------------------------------------
Nothing in this module knows what a ``MailboxDomainRule`` is, what a
``sender_domain``/``sender_address`` "belongs to" a mailbox, or what
``INCLUDE_SUBDOMAINS`` scope means (that boundary logic —
``services.mailbox.domain_rule.domain_in_scope`` — stays in the
mailbox module, deliberately NOT moved here: it is mailbox-domain-rule
specific, not a generic text-matching primitive). What lives here is
purely: normalise an email address/domain to canonical comparison
form, normalise a free-text subject line to canonical comparison form,
and test a normalised subject against a stored EXACT/STARTS_WITH
predicate. Any BAGMAN component that needs "does this address/subject
match that other one, after normalisation" — today
``services.mailbox.domain_rule`` and ``services.evidence.classification_rule``
— depends on this module directly, never on each other for this
purpose.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from core.errors import ValidationError

#: Collapse consecutive internal whitespace to one ASCII space — applied
#: AFTER casefold/strip in `normalize_subject_for_policy` below.
_WHITESPACE_RUN = re.compile(r"\s+")

SUBJECT_PREDICATE_EXACT = "EXACT"
SUBJECT_PREDICATE_STARTS_WITH = "STARTS_WITH"


def normalize_domain(sender_domain: str) -> str:
    """The ONE place every caller normalises a sender domain before
    either a match/uniqueness comparison or storage."""
    return sender_domain.strip().lower()


def normalize_address(sender_address: str) -> str:
    """The ONE place every caller normalises a full sender email address
    before either a match/uniqueness comparison or storage. Mirrors
    :func:`normalize_domain`'s identical role one level up."""
    return sender_address.strip().lower()


def domain_from_address(sender_address: str) -> str:
    """The one place a domain is derived from a full email address,
    applied to an already-known-valid address.

    Raises:
        core.errors.ValidationError: ``sender_address`` is not a valid
            email address (missing ``@``, or an empty domain).
    """
    normalized = normalize_address(sender_address)
    if "@" not in normalized:
        raise ValidationError(f"'{sender_address}' is not a valid email address (missing '@')")
    domain = normalized.rsplit("@", 1)[-1]
    if not domain:
        raise ValidationError(f"'{sender_address}' is not a valid email address (empty domain)")
    return domain


def normalize_subject_for_policy(subject: Optional[str]) -> Optional[str]:
    """The ONE place every caller normalises a message subject before
    either a predicate comparison or storage. Applied in this EXACT
    order:

    1. Unicode NFKC normalisation (`unicodedata.normalize("NFKC", ...)`)
       — canonicalises visually-identical-but-differently-encoded
       characters (e.g. full-width vs. half-width forms) before any
       comparison.
    2. `.casefold()` — a real subject rule must never be defeated by
       case variation.
    3. `.strip()` — leading/trailing whitespace never carries meaning.
    4. Collapse consecutive INTERNAL whitespace to one ASCII space — a
       real, observed eBay defect (a literal double space inside a
       subject line) must not silently defeat an otherwise-correct
       predicate.

    Deliberately does NOT strip punctuation/digits, and does NOT strip
    `Re:`/`Fwd:` reply/forward prefixes — this is a NORMALISATION step,
    never a semantic rewrite; a predicate author who wants to match past
    a `Re:` prefix uses `STARTS_WITH` with the prefix included, or
    `EXACT` against the reply subject's own literal (normalised) text.

    `None` in -> `None` out."""
    if subject is None:
        return None
    normalized = unicodedata.normalize("NFKC", subject).casefold().strip()
    return _WHITESPACE_RUN.sub(" ", normalized)


def subject_matches_predicate(subject: Optional[str], *, predicate_type: str, predicate_value: str) -> bool:
    """The ONE place a real candidate subject is tested against a
    stored ``EXACT``/``STARTS_WITH`` predicate.

    ``subject`` is the RAW (un-normalised) candidate subject —
    normalised HERE, internally, via `normalize_subject_for_policy`.
    ``predicate_value`` MUST already be in canonical normalised form
    (exactly what every caller stores); it is never re-normalised here,
    so a caller passing a raw, un-normalised predicate value would
    silently never match — every caller of this function is expected to
    already hold a normalised stored value."""
    normalized_subject = normalize_subject_for_policy(subject)
    if not normalized_subject:
        return False
    if predicate_type == SUBJECT_PREDICATE_EXACT:
        return normalized_subject == predicate_value
    if predicate_type == SUBJECT_PREDICATE_STARTS_WITH:
        return normalized_subject.startswith(predicate_value)
    return False
