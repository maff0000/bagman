"""``MailboxDomainRule`` — the durable, MAILBOX-SPECIFIC domain-policy
registry that drives Stage B of the two-stage mail-processing gate (CD-6
architect amendment, superseding Slice 4A's "ingest everything
unconditionally" sweep behaviour).

Why mailbox-specific, never a global domain->entity table
------------------------------------------------------------
The same sender domain can legitimately mean something different in two
different mailboxes (architect spec §8) — this module never builds a
``sender_domain -> GovernedEntity`` table; every lookup/uniqueness rule
is scoped to ``(mailbox_id, sender_domain)``. Only one real mailbox
exists at the time of this delivery (``matt@infosecurs.com``), so this
matters architecturally, not operationally, yet — but the schema must
never assume otherwise (architect spec, verbatim).

Uniqueness — one rule per (mailbox_id, sender_domain), never a second
row for a changed mind
------------------------------------------------------------------------
:meth:`MailboxDomainRuleRepository.upsert_rule` is a resolve-or-create-
or-update operation, mirroring
``services.mailbox.message.MailboxMessageRepository.record_observation``'s
own "never a blind insert" discipline: an operator changing their
decision about a domain (e.g. IGNORED -> ALLOWED) updates the SAME row.
The real, authoritative enforcement is the database-level unique
constraint on ``(mailbox_id, sender_domain)`` (see
``persistence/postgres/mailbox_domain_rule_models.py``); the in-memory
repository below mirrors it with a plain dict.

Policy lifecycle — a real, tested transition (architect spec §6)
------------------------------------------------------------------------
``policy`` is a small, closed, real state machine (see
:data:`ALLOWED_POLICY_TRANSITIONS`) — an operator must later be able to
flip an ``IGNORED`` domain back to ``ALLOWED`` (or vice versa) as a
plain lifecycle transition, never by deleting/recreating the row.

Never a global domain->entity inference (architect spec §3)
------------------------------------------------------------------------
An ``ALLOWED`` rule relates to one of BAGMAN's canonical destinations
using a REAL ``destination_entity_id`` (never a string/name), and is
always either a confident, operator-approved routing decision
(``destination_mode="FIXED"``) or an honest "this domain is real
accounting evidence but its destination is not yet safely known"
placeholder (``destination_mode="REVIEW_REQUIRED"``, ``destination_entity_id=None``)
— this module never silently guesses a destination from the domain
alone.

Reprocessing doctrine on a policy change — a documented judgment call
------------------------------------------------------------------------
Changing a rule's policy (e.g. an operator later flips a long-ignored
domain to ALLOWED) does NOT, by itself, retroactively reprocess every
historically-``CHECKED_NOT_CANDIDATE``/ignored ``MailboxMessage`` row
under the new policy — see ``services/mailbox/sweep.py``'s own module
docstring for the full reasoning (a documented, PL/architect-flagged
judgment call). Only the one specific message that triggered a Needs
You domain-review item is guaranteed immediate reprocessing on
approval (architect spec §4's own explicit requirement) — see
``services.mailbox.sweep.reprocess_message_after_domain_rule_approval``.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "mailbox/bagman.mailbox_domain_rule.v1.schema.json"
SCHEMA_VERSION = "bagman.mailbox_domain_rule.v1"

MATCH_MODE_EXACT = "EXACT"
MATCH_MODE_INCLUDE_SUBDOMAINS = "INCLUDE_SUBDOMAINS"
MATCH_MODES = frozenset({MATCH_MODE_EXACT, MATCH_MODE_INCLUDE_SUBDOMAINS})

#: Closed policy vocabulary (architect spec §2) — matches the
#: contract's own closed `policy` enum.
POLICY_ALLOWED = "ALLOWED"
POLICY_IGNORED = "IGNORED"
POLICIES = frozenset({POLICY_ALLOWED, POLICY_IGNORED})

#: The single source of truth for valid MailboxDomainRule.policy
#: transitions (architect spec §6 — "An operator must later be able to
#: change an ignored domain back to review/allowed", applied
#: symmetrically since the reverse is equally a plain, real operator
#: decision).
ALLOWED_POLICY_TRANSITIONS: dict[str, frozenset[str]] = {
    POLICY_ALLOWED: frozenset({POLICY_IGNORED}),
    POLICY_IGNORED: frozenset({POLICY_ALLOWED}),
}

DESTINATION_MODE_FIXED = "FIXED"
DESTINATION_MODE_REVIEW_REQUIRED = "REVIEW_REQUIRED"
DESTINATION_MODES = frozenset({DESTINATION_MODE_FIXED, DESTINATION_MODE_REVIEW_REQUIRED})

#: Documented (non-exhaustive), OPEN `source` vocabulary — see module
#: docstring/contract description for why this is never a closed set.
SOURCE_OPERATOR = "OPERATOR"
SOURCE_BAGMAN_PROPOSED = "BAGMAN_PROPOSED"


@dataclass(frozen=True)
class MailboxDomainRule:
    """One mailbox-specific domain-policy routing rule. Immutable once
    constructed — every change produces a NEW snapshot via
    :func:`transition_policy` or a repository upsert, never an in-place
    mutation."""

    rule_id: str
    mailbox_id: str
    sender_domain: str
    match_mode: str
    policy: str
    destination_entity_id: Optional[str]
    destination_mode: Optional[str]
    source: str
    processor_hint: Optional[str]
    approved_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "mailbox_id": self.mailbox_id,
            "sender_domain": self.sender_domain,
            "match_mode": self.match_mode,
            "policy": self.policy,
            "destination_entity_id": self.destination_entity_id,
            "destination_mode": self.destination_mode,
            "source": self.source,
            "processor_hint": self.processor_hint,
            "approved_at": to_contract_string(self.approved_at) if self.approved_at is not None else None,
            "created_at": to_contract_string(self.created_at),
            "updated_at": to_contract_string(self.updated_at),
            "last_seen_at": to_contract_string(self.last_seen_at),
            "schema_version": self.schema_version,
        }


def normalize_domain(sender_domain: str) -> str:
    """The ONE place every caller normalises a sender domain before
    either a match/uniqueness comparison or storage — mirrors
    ``services.mailbox.mailbox.normalize_email``'s identical role."""
    return sender_domain.strip().lower()


def validate_policy_fields_or_raise(
    *, policy: str, destination_entity_id: Optional[str], destination_mode: Optional[str]
) -> None:
    """Shared validation both repository implementations call before
    persisting a rule — see contract's own field descriptions for the
    exact rules enforced here."""
    if policy not in POLICIES:
        raise ValidationError(f"'{policy}' is not a governed MailboxDomainRule policy — must be one of {sorted(POLICIES)}")
    if policy == POLICY_IGNORED:
        if destination_entity_id is not None or destination_mode is not None:
            raise ValidationError(
                "an IGNORED MailboxDomainRule must never carry a destination_entity_id/destination_mode"
            )
        return
    # policy == ALLOWED
    if destination_mode not in DESTINATION_MODES:
        raise ValidationError(
            f"an ALLOWED MailboxDomainRule requires destination_mode to be one of {sorted(DESTINATION_MODES)} "
            f"(got {destination_mode!r})"
        )
    if destination_mode == DESTINATION_MODE_FIXED and not destination_entity_id:
        raise ValidationError(
            "an ALLOWED MailboxDomainRule with destination_mode='FIXED' requires a real destination_entity_id "
            "— domain alone must never silently determine a destination (architect spec §3)"
        )


def transition_policy(rule: MailboxDomainRule, new_policy: str, **field_updates) -> MailboxDomainRule:
    """Move ``rule.policy`` to ``new_policy``, enforcing
    :data:`ALLOWED_POLICY_TRANSITIONS`. Stamps ``updated_at``
    automatically."""
    allowed = ALLOWED_POLICY_TRANSITIONS.get(rule.policy, frozenset())
    if new_policy not in allowed:
        raise InvalidStateTransitionError(
            f"MailboxDomainRule '{rule.rule_id}' cannot transition policy from "
            f"'{rule.policy}' to '{new_policy}'; allowed transitions from "
            f"'{rule.policy}' are {sorted(allowed) or '(none)'}"
        )
    try:
        updated = dataclasses.replace(rule, policy=new_policy, updated_at=utc_now(), **field_updates)
        validate_policy_fields_or_raise(
            policy=updated.policy,
            destination_entity_id=updated.destination_entity_id,
            destination_mode=updated.destination_mode,
        )
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition MailboxDomainRule policy: {exc}") from exc
    return updated


class MailboxDomainRuleRepository(abc.ABC):
    """Repository abstraction for MailboxDomainRule."""

    @abc.abstractmethod
    def upsert_rule(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        policy: str,
        destination_entity_id: Optional[str],
        destination_mode: Optional[str],
        source: str,
        processor_hint: Optional[str] = None,
        approved_at: Optional[datetime] = None,
    ) -> MailboxDomainRule:
        """Resolve-or-create-or-update by ``(mailbox_id, sender_domain)``
        (``sender_domain`` normalised via :func:`normalize_domain`
        first) — see module docstring's "Uniqueness" section. A
        genuinely new domain creates a fresh row; an existing domain's
        rule is fully replaced (mirrors
        ``services.mailbox.mailbox.MailboxSourceRepository
        .update_mailbox``'s own "full replace, never a partial patch"
        documented choice), INCLUDING a policy change (e.g. operator-
        driven ``IGNORED`` -> ``ALLOWED``) — deliberately a plain
        replace here, NOT routed through :func:`transition_policy`'s
        own closed state machine (that function exists and is tested
        for a caller that specifically wants transition-legality
        enforcement, but this method does not call it): PL review
        correction (docstring only, behaviour was already correct) —
        a same-policy upsert being a harmless no-op, rather than an
        error, mirrors this delivery's own established "a redundant
        same-state action must never fail" doctrine (see
        ``services.mailbox.mailbox``'s enable/disable/retire
        idempotency fix), which :func:`transition_policy`'s own
        raise-on-no-real-transition shape would have contradicted here.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_rule(self, rule_id: str) -> MailboxDomainRule:
        raise NotImplementedError

    @abc.abstractmethod
    def find_for_sender(self, *, mailbox_id: str, sender_domain: str) -> Optional[MailboxDomainRule]:
        """Resolve the governing rule (if any) for a message from
        ``sender_domain`` observed in ``mailbox_id``: an ``EXACT`` rule
        for this exact domain, if one exists; otherwise any
        ``INCLUDE_SUBDOMAINS`` rule whose ``sender_domain`` is this
        domain or a parent of it. Returns ``None`` — never
        ``NotFoundError`` — when no rule governs this domain yet (the
        Stage-B 'unknown domain' path)."""
        raise NotImplementedError

    @abc.abstractmethod
    def touch_last_seen(self, *, mailbox_id: str, sender_domain: str, seen_at: datetime) -> MailboxDomainRule:
        """Stamp ``last_seen_at`` on the rule matched for this
        ``(mailbox_id, sender_domain)`` pair (observability only —
        never a gate decision). Raises ``core.errors.NotFoundError`` if
        no rule exists (a caller always resolves a rule via
        :meth:`find_for_sender` first)."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_rules(self, *, mailbox_id: str) -> list[MailboxDomainRule]:
        raise NotImplementedError


class InMemoryMailboxDomainRuleRepository(MailboxDomainRuleRepository):
    """Narrow in-memory reference implementation."""

    def __init__(self) -> None:
        self._by_id: dict[str, MailboxDomainRule] = {}
        self._id_by_key: dict[tuple[str, str], str] = {}

    def _match_key(self, *, mailbox_id: str, sender_domain: str) -> tuple[str, str]:
        return (mailbox_id, normalize_domain(sender_domain))

    def upsert_rule(
        self,
        *,
        mailbox_id: str,
        sender_domain: str,
        match_mode: str,
        policy: str,
        destination_entity_id: Optional[str],
        destination_mode: Optional[str],
        source: str,
        processor_hint: Optional[str] = None,
        approved_at: Optional[datetime] = None,
    ) -> MailboxDomainRule:
        if match_mode not in MATCH_MODES:
            raise ValidationError(f"'{match_mode}' is not a governed match_mode — must be one of {sorted(MATCH_MODES)}")
        validate_policy_fields_or_raise(
            policy=policy, destination_entity_id=destination_entity_id, destination_mode=destination_mode
        )
        normalized_domain = normalize_domain(sender_domain)
        key = (mailbox_id, normalized_domain)
        existing_id = self._id_by_key.get(key)
        now = utc_now()

        if existing_id is None:
            try:
                candidate = MailboxDomainRule(
                    rule_id=identity.generate_id(),
                    mailbox_id=mailbox_id,
                    sender_domain=normalized_domain,
                    match_mode=match_mode,
                    policy=policy,
                    destination_entity_id=destination_entity_id,
                    destination_mode=destination_mode,
                    source=source,
                    processor_hint=processor_hint,
                    approved_at=approved_at,
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
                validate_against_contract(candidate.to_dict(), _SCHEMA)
            except ValidationError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a raw exception
                raise ValidationError(f"could not create MailboxDomainRule: {exc}") from exc
            self._by_id[candidate.rule_id] = candidate
            self._id_by_key[key] = candidate.rule_id
            return candidate

        current = self._by_id[existing_id]
        try:
            updated = dataclasses.replace(
                current,
                match_mode=match_mode,
                policy=policy,
                destination_entity_id=destination_entity_id,
                destination_mode=destination_mode,
                source=source,
                processor_hint=processor_hint,
                approved_at=approved_at if approved_at is not None else current.approved_at,
                updated_at=now,
            )
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not update MailboxDomainRule: {exc}") from exc
        self._by_id[existing_id] = updated
        return updated

    def get_rule(self, rule_id: str) -> MailboxDomainRule:
        try:
            return self._by_id[rule_id]
        except KeyError:
            raise NotFoundError(f"no MailboxDomainRule with rule_id '{rule_id}'") from None

    def find_for_sender(self, *, mailbox_id: str, sender_domain: str) -> Optional[MailboxDomainRule]:
        normalized_domain = normalize_domain(sender_domain)
        exact_id = self._id_by_key.get((mailbox_id, normalized_domain))
        if exact_id is not None:
            rule = self._by_id[exact_id]
            if rule.match_mode == MATCH_MODE_EXACT:
                return rule
            # An EXACT-keyed row can itself be an INCLUDE_SUBDOMAINS
            # rule (the rule's own domain IS the observed domain) —
            # still a legitimate direct hit.
            return rule

        for rule in self._by_id.values():
            if rule.mailbox_id != mailbox_id or rule.match_mode != MATCH_MODE_INCLUDE_SUBDOMAINS:
                continue
            if normalized_domain == rule.sender_domain or normalized_domain.endswith(f".{rule.sender_domain}"):
                return rule
        return None

    def touch_last_seen(self, *, mailbox_id: str, sender_domain: str, seen_at: datetime) -> MailboxDomainRule:
        rule = self.find_for_sender(mailbox_id=mailbox_id, sender_domain=sender_domain)
        if rule is None:
            raise NotFoundError(
                f"no MailboxDomainRule governs mailbox_id={mailbox_id!r} sender_domain={sender_domain!r} — "
                "find_for_sender must resolve a rule before touch_last_seen"
            )
        updated = dataclasses.replace(rule, last_seen_at=seen_at)
        self._by_id[rule.rule_id] = updated
        return updated

    def list_rules(self, *, mailbox_id: str) -> list[MailboxDomainRule]:
        return sorted(
            (r for r in self._by_id.values() if r.mailbox_id == mailbox_id),
            key=lambda r: (r.sender_domain, r.rule_id),
        )
