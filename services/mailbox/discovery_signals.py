"""``evaluate_discovery_candidate`` — a deliberately small, bounded,
NON-AI heuristic used ONLY to decide whether an email from an UNKNOWN
sender domain deserves exactly one Needs You question (CD-6 architect
amendment §4).

This is intentionally crude, and must stay that way
--------------------------------------------------------
This function is plain keyword/pattern matching over metadata BAGMAN
already has from Stage-A discovery (subject text, attachment filename/
content-type) — it never fetches/parses MIME content, never calls an
AI/LLM, never learns, never scores confidence beyond a boolean. Real
document classification intelligence is Slice 5's entire scope (see
``services/mailbox/sweep.py``'s own module docstring for the identical
hard boundary at the orchestration layer). If a future change to this
module starts resembling a classifier — weighted scoring, a trained
model, fuzzy matching — that no longer belongs here.

Fail-quiet, not fail-open
------------------------------
A message that does not match anything here is simply
``CHECKED_NOT_CANDIDATE`` (see ``services/mailbox/sweep.py``) — never
escalated, never retried, never MIME-fetched. This function's only job
is to keep BAGMAN from raising a Needs You item for every stranger's
newsletter while still catching an obvious first invoice from a new
supplier.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

#: Deliberately plain, English, singular/plural-insensitive-by-substring
#: keyword set — architect spec's own worked examples ("invoice",
#: "receipt", "payment", "order confirmation"-style language).
_CANDIDATE_KEYWORDS: frozenset[str] = frozenset(
    {
        "invoice",
        "receipt",
        "statement",
        "payment",
        "remittance",
        "order confirmation",
        "purchase order",
        "billing",
        "tax invoice",
        "credit note",
        "account statement",
        "subscription renewal",
    }
)

#: Common accounting-document attachment content types — a bounded,
#: documented, non-exhaustive list (architect spec: "attachment
#: content-type being PDF/common receipt-image types").
_CANDIDATE_ATTACHMENT_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/heic",
    }
)


@dataclass(frozen=True)
class DiscoverySignalResult:
    is_candidate: bool
    reason: str


def _contains_keyword(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for keyword in _CANDIDATE_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def evaluate_discovery_candidate(
    *, subject: Optional[str], attachment_metadata: Optional[Sequence[Mapping[str, Any]]]
) -> DiscoverySignalResult:
    """Bounded, non-AI verdict: does this UNKNOWN-domain message look
    like a credible new accounting-document source? Uses only Stage-A
    discovery metadata (never MIME content)."""
    subject_hit = _contains_keyword(subject)
    if subject_hit is not None:
        return DiscoverySignalResult(True, f"subject contains keyword {subject_hit!r}")

    for attachment in attachment_metadata or ():
        filename_hit = _contains_keyword(attachment.get("filename"))
        if filename_hit is not None:
            return DiscoverySignalResult(True, f"attachment filename contains keyword {filename_hit!r}")

        content_type = (attachment.get("content_type") or "").strip().lower()
        if content_type in _CANDIDATE_ATTACHMENT_CONTENT_TYPES:
            return DiscoverySignalResult(
                True, f"attachment content_type {content_type!r} is a common accounting-document type"
            )

    return DiscoverySignalResult(
        False, "no invoice/receipt/statement-style signal found in subject or attachment metadata"
    )
