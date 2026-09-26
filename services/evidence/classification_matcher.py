"""Deterministic (non-AI) ``EvidenceClassificationRule`` matcher (CD-6
Slice 5 WI-2).

This module answers exactly one question, purely and deterministically:
"given one candidate sender/subject pair and a set of currently-ACTIVE
``EvidenceClassificationRule`` rows, which rule (if any) governs it?" —
see :func:`match_evidence_to_rule`. It is the SINGLE implementation of
the precedence doctrine ``services.evidence.classification_rule``'s own
module docstring already states ("Precedence — recorded, not
implemented"): address beats domain, EXACT beats STARTS_WITH, longest
STARTS_WITH wins, genuine ties fail closed. Three separate callers reuse
this SAME function rather than reimplementing it a second/third time:
the observed-evidence guard (:mod:`services.evidence.classification_matcher`
itself, :func:`observed_evidence_guard`), the read-only preview service,
and the deterministic classification service — all three built
elsewhere in this delivery on top of this module.

Pure, dependency-free, trivially unit-testable
------------------------------------------------------------------------
:func:`match_evidence_to_rule` takes ``active_rules`` as a plain
argument — a list the CALLER already fetched (typically via
``EvidenceClassificationRuleRepository.list_rules(status="ACTIVE")``).
This module has NO repository/database dependency of its own, and
therefore no import of ``persistence.postgres.*`` or any provider
adapter — see
``tests/integration/test_architecture_boundaries.py``'s WI-2 additions
for the AST-based proof.

CONFLICT reachability — a documented finding, not an assumption
------------------------------------------------------------------------
Given WI-1's own active-identity uniqueness constraint (at most one
ACTIVE rule per ``(sender_scope_type, sender_scope_value,
subject_predicate_type, subject_predicate_value)``), a genuine
``CONFLICT`` is believed to be STRUCTURALLY UNREACHABLE through the
normal, validated write path in V1's data model: within one sender tier
(all candidate rules necessarily share the identical, already-matched
``sender_scope_value``), two rules can only differ by
``subject_predicate_type``/``subject_predicate_value`` — and two
DIFFERENT values of the SAME type can never both match the SAME real
subject with equal specificity (two distinct ``EXACT`` values cannot
both equal one subject; two distinct ``STARTS_WITH`` values of equal
length cannot both be a length-N prefix of the same subject — a string
has exactly one length-N prefix). The ``CONFLICT`` branch is still
implemented defensively, exactly like
``MailboxDomainRule.find_for_sender``'s own analogous
"structurally impossible in ordinary operation, but never assumed
impossible" branch, because a manual DB operation or a future WI could
still create the condition (e.g. directly-constructed
``EvidenceClassificationRule`` fixtures bypassing the repository's own
uniqueness check — see this module's own test suite for exactly such a
constructed-tie proof).

No filename-based path in V1 (WO's own explicit instruction)
------------------------------------------------------------------------
A manual upload with no ``sender_address``/``subject`` metadata is
never classified from its filename or any other signal — it always
resolves to ``NO_APPLICABLE_RULE_INPUT``. There is no code path here
that reads ``EvidenceItem.original_name`` at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from core.errors import ValidationError
from core.text_matching import (
    SUBJECT_PREDICATE_EXACT,
    SUBJECT_PREDICATE_STARTS_WITH,
    domain_from_address,
    normalize_address,
    normalize_subject_for_policy,
    subject_matches_predicate,
)
from services.evidence.classification_rule import (
    SENDER_SCOPE_EXACT_SENDER_ADDRESS,
    SENDER_SCOPE_EXACT_SENDER_DOMAIN,
    EvidenceClassificationRule,
)

#: The closed set of outcomes :class:`MatchResult` may carry — see that
#: class's own docstring for what each one means and what payload
#: fields it carries.
OUTCOME_MATCH = "MATCH"
OUTCOME_NO_MATCH = "NO_MATCH"
OUTCOME_NO_APPLICABLE_RULE_INPUT = "NO_APPLICABLE_RULE_INPUT"
OUTCOME_CONFLICT = "CONFLICT"
MATCH_OUTCOMES = frozenset({OUTCOME_MATCH, OUTCOME_NO_MATCH, OUTCOME_NO_APPLICABLE_RULE_INPUT, OUTCOME_CONFLICT})

#: ``matching_tier`` string shapes this module produces on a genuine
#: ``MATCH`` — ``"<sender_scope_type>+<subject_predicate_type>"``. Not a
#: closed contract vocabulary (nothing persists this value — it exists
#: purely for observability/debugging in a preview/audit payload), but
#: kept as named constants here so every caller compares against the
#: same literal rather than re-deriving the string shape independently.
TIER_EXACT_SENDER_ADDRESS_EXACT = f"{SENDER_SCOPE_EXACT_SENDER_ADDRESS}+{SUBJECT_PREDICATE_EXACT}"
TIER_EXACT_SENDER_ADDRESS_STARTS_WITH = f"{SENDER_SCOPE_EXACT_SENDER_ADDRESS}+{SUBJECT_PREDICATE_STARTS_WITH}"
TIER_EXACT_SENDER_DOMAIN_EXACT = f"{SENDER_SCOPE_EXACT_SENDER_DOMAIN}+{SUBJECT_PREDICATE_EXACT}"
TIER_EXACT_SENDER_DOMAIN_STARTS_WITH = f"{SENDER_SCOPE_EXACT_SENDER_DOMAIN}+{SUBJECT_PREDICATE_STARTS_WITH}"


@dataclass(frozen=True)
class MatchResult:
    """The single, closed, typed result shape every caller of
    :func:`match_evidence_to_rule` receives — never a free-form dict
    used as an ad-hoc state machine.

    ``outcome`` is always one of :data:`MATCH_OUTCOMES`:

    * ``MATCH`` — exactly one rule governs this evidence.
      ``rule_id``/``document_type``/``matching_tier``/``normalized_subject``
      are all populated; ``conflicting_rule_ids`` is empty.
    * ``NO_MATCH`` — sender/subject were eligible (see
      :func:`match_evidence_to_rule`'s own eligibility gate) but no
      ACTIVE rule's identity/predicate matched them.
      ``normalized_subject`` is still populated (the input WAS
      eligible); every other payload field is ``None``/empty.
    * ``NO_APPLICABLE_RULE_INPUT`` — the sender/subject pair was never
      eligible for rule matching at all (missing/empty/malformed sender,
      or missing/empty subject) — every payload field is ``None``/empty,
      INCLUDING ``normalized_subject`` (normalisation was never
      attempted, or attempted and discarded, because the input as a
      whole was ineligible).
    * ``CONFLICT`` — two or more ACTIVE rules are equally authoritative
      for this evidence and cannot be ordered by the precedence rules —
      a genuine ambiguity, never silently resolved.
      ``conflicting_rule_ids`` names every tied rule;
      ``normalized_subject`` is populated (the input WAS eligible);
      ``rule_id``/``document_type``/``matching_tier`` are ``None``.
    """

    outcome: str
    rule_id: Optional[str] = None
    document_type: Optional[str] = None
    matching_tier: Optional[str] = None
    normalized_subject: Optional[str] = None
    conflicting_rule_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome not in MATCH_OUTCOMES:
            raise ValidationError(f"'{self.outcome}' is not one of {sorted(MATCH_OUTCOMES)}")


def _no_applicable_rule_input() -> MatchResult:
    return MatchResult(outcome=OUTCOME_NO_APPLICABLE_RULE_INPUT)


def match_evidence_to_rule(
    *,
    sender_address: Optional[str],
    subject: Optional[str],
    active_rules: Sequence[EvidenceClassificationRule],
) -> MatchResult:
    """Resolve the SINGLE governing ``EvidenceClassificationRule`` (if
    any) for one candidate ``(sender_address, subject)`` pair, against
    ``active_rules`` (a caller-supplied list — this function performs no
    repository call of its own; see module docstring).

    Precedence (see ``services.evidence.classification_rule``'s own
    module docstring, "Precedence — recorded, not implemented", which
    this function is the first real implementation of):

    1. Eligibility gate — ``NO_APPLICABLE_RULE_INPUT`` if ``sender_address``
       is missing/empty/malformed (cannot be normalised into something
       containing ``@`` with a non-empty domain), or ``subject`` is
       missing/empty.
    2. ``EXACT_SENDER_ADDRESS`` rules matching the normalised address
       beat ``EXACT_SENDER_DOMAIN`` rules matching the normalised domain
       — a domain rule is only even considered when zero address rules
       matched the sender at all.
    3. Within the winning sender tier, ``EXACT`` subject-predicate
       matches beat ``STARTS_WITH`` matches; among ``STARTS_WITH``
       matches, the LONGEST normalised predicate value wins.
    4. If, after 2-3, more than one rule remains equally authoritative
       -> ``CONFLICT`` (never broken by newest/oldest/rule_id order —
       fail closed).
    5. Zero rules matched at any point -> ``NO_MATCH``.
    6. Exactly one rule remains -> ``MATCH``.

    ``active_rules`` is expected to already be filtered to
    ``status == "ACTIVE"`` by the caller (this function does not itself
    check ``.status`` — a caller that passes a RETIRED rule in by
    mistake gets it considered as if active; every real caller in this
    delivery sources ``active_rules`` from
    ``EvidenceClassificationRuleRepository.list_rules(status="ACTIVE")``,
    which already excludes retired rows).
    """
    if not sender_address or not subject:
        return _no_applicable_rule_input()

    try:
        normalized_address = normalize_address(sender_address)
        normalized_domain = domain_from_address(normalized_address)
    except ValidationError:
        return _no_applicable_rule_input()

    normalized_subject = normalize_subject_for_policy(subject)
    if not normalized_subject:
        return _no_applicable_rule_input()

    # --- Sender tier -----------------------------------------------
    address_candidates = [
        r
        for r in active_rules
        if r.sender_scope_type == SENDER_SCOPE_EXACT_SENDER_ADDRESS and r.sender_scope_value == normalized_address
    ]
    if address_candidates:
        sender_tier_candidates = address_candidates
    else:
        sender_tier_candidates = [
            r
            for r in active_rules
            if r.sender_scope_type == SENDER_SCOPE_EXACT_SENDER_DOMAIN and r.sender_scope_value == normalized_domain
        ]

    if not sender_tier_candidates:
        return MatchResult(outcome=OUTCOME_NO_MATCH, normalized_subject=normalized_subject)

    # --- Subject-predicate evaluation -------------------------------
    predicate_matches = [
        r
        for r in sender_tier_candidates
        if subject_matches_predicate(
            subject, predicate_type=r.subject_predicate_type, predicate_value=r.subject_predicate_value
        )
    ]
    if not predicate_matches:
        return MatchResult(outcome=OUTCOME_NO_MATCH, normalized_subject=normalized_subject)

    # --- Specificity ordering: EXACT beats STARTS_WITH --------------
    exact_matches = [r for r in predicate_matches if r.subject_predicate_type == SUBJECT_PREDICATE_EXACT]
    if exact_matches:
        winners = exact_matches
    else:
        starts_with_matches = [r for r in predicate_matches if r.subject_predicate_type == SUBJECT_PREDICATE_STARTS_WITH]
        max_len = max(len(r.subject_predicate_value) for r in starts_with_matches)
        winners = [r for r in starts_with_matches if len(r.subject_predicate_value) == max_len]

    if len(winners) > 1:
        return MatchResult(
            outcome=OUTCOME_CONFLICT,
            normalized_subject=normalized_subject,
            conflicting_rule_ids=tuple(sorted(r.rule_id for r in winners)),
        )

    winner = winners[0]
    tier = f"{winner.sender_scope_type}+{winner.subject_predicate_type}"
    return MatchResult(
        outcome=OUTCOME_MATCH,
        rule_id=winner.rule_id,
        document_type=winner.document_type,
        matching_tier=tier,
        normalized_subject=normalized_subject,
    )
