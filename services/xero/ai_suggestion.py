"""AI-suggested-Xero-account validation (CD-6 Slice 2, architect spec
§6): "the model may choose ONLY from the current synced, eligible
account set explicitly supplied to it — never invent an
AccountID/Code/Name/TaxType... the output must be validated
(post-response) against that exact list — reject/treat as `UNRESOLVED`
anything not in it, never trust the model's own claim of validity."

Judgment call recorded here (architect spec §6: "you are NOT required
to build a new AI task/prompt for this in this slice... document your
judgment call either way")
------------------------------------------------------------------------
This slice does NOT wire a new AI task/prompt that actually calls an AI
provider to propose a Xero account. Reasoning: Slice 1's existing
Company/What/Why review flow (`app/api/routers/needs_you.py`,
`app/api/routers/intake.py`'s own `COMPANY_REQUIRED` producer) has no
existing AI-proposal step to extend — "What"/"Why" are, and remain,
plain operator-typed free text; nothing in Slice 1 already calls a
background AI task for this flow that this slice could simply widen the
candidate set of. Building a brand-new AI task/prompt purely to
originate a Xero-account suggestion would be new AI-integration surface
area, not a "wire this slice into an existing integration point" change
— exactly the kind of scope growth CD-6 Slice 2's own dispatch names as
unlikely to be forced and cautions against. The CONSTRAINT itself (this
module) is still built and tested now, real and ready: whichever future
slice DOES call an AI task to propose a Xero account (Slice 5/6's email/
invoice-coding delivery is the natural home) imports
:func:`resolve_ai_suggested_account` and gets this guarantee for free,
proven by :mod:`tests.integration.test_xero_ai_suggestion_constraint`
against a scripted fake AI response today — nothing about this
constraint is deferred, only the AI CALL that would produce a
suggestion to validate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from services.xero.account import XeroAccount

#: The sentinel this module returns instead of ever passing through an
#: out-of-candidate-set suggestion — architect spec §6's own literal
#: vocabulary ("reject/treat as `UNRESOLVED`"). Matches the existing
#: `ai/tasks.py` pattern of a small, explicit string constant for "the
#: model could not / must not resolve this" rather than `None` (which a
#: caller could too easily conflate with "no suggestion was attempted
#: at all").
UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class AccountSuggestionResolution:
    """The result of validating one AI-proposed `account_id` against a
    real candidate set."""

    resolved: bool
    account_id: Optional[str]  # the validated AccountID, or None if UNRESOLVED
    reason: str


def resolve_ai_suggested_account(
    suggested_account_id: Optional[str],
    *,
    eligible_candidates: Iterable[XeroAccount],
) -> AccountSuggestionResolution:
    """Validate one AI-proposed `AccountID` against `eligible_candidates`
    (the EXACT closed set a caller supplied to the model as its
    candidate list — never a broader "every synced account" set; the
    caller is responsible for having already narrowed to the real
    eligible set via `services.xero.eligibility.list_eligible_accounts`
    before ever prompting a model, per architect spec §6's own "the
    prompt must supply the closed candidate list explicitly").

    Never raises — a malformed/absent/out-of-set suggestion is always a
    normal, expected outcome (an AI proposal is advisory, CD-5 PID
    §98.10's own "AI output = proposal, never trusted at face value"
    doctrine applied here), returned as data via
    :class:`AccountSuggestionResolution`, exactly like every other
    provider/AI outcome in this codebase (`LiteLLMCompletionResult`,
    `ClaudeCodeInvocationResult`) is data, never an exception a caller
    must remember to catch.
    """
    candidate_ids = {a.account_id for a in eligible_candidates}

    if not suggested_account_id:
        return AccountSuggestionResolution(
            resolved=False, account_id=None, reason="no suggestion was supplied"
        )
    if suggested_account_id not in candidate_ids:
        return AccountSuggestionResolution(
            resolved=False,
            account_id=None,
            reason=(
                f"suggested account_id {suggested_account_id!r} is not one of the "
                f"{len(candidate_ids)} eligible candidates explicitly supplied to the model — "
                "rejected, never trusted on the model's own claim of validity (architect spec §6)"
            ),
        )
    return AccountSuggestionResolution(
        resolved=True, account_id=suggested_account_id, reason="within the supplied candidate set"
    )
