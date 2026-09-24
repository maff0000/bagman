"""``EvidenceClassificationRule`` — a deterministic, operator-governed
document-classification rule (CD-6 Slice 5 WI-1). WI-2's future matcher
will use these rules to produce ``DETERMINISTIC_RULE``-sourced
``services.evidence.classification.EvidenceClassification`` rows for a
real mailbox message; WI-1 builds only the data model + persistence — no
live matcher/resolver function exists yet (see "Precedence — recorded,
not implemented" below).

Deliberately narrower than ``MailboxDomainRule``
------------------------------------------------------------------------
This module is a SEPARATE governed authority from
``services.mailbox.domain_rule.MailboxDomainRule`` — never an extension
of it, never sharing its identity space or its closed vocabularies.
``sender_scope_type`` (``EXACT_SENDER_DOMAIN``/``EXACT_SENDER_ADDRESS``)
is deliberately NOT named ``match_mode`` (that name belongs to the
mailbox module's own, larger vocabulary — reusing it here would suggest
a shared identity space that does not exist). ``subject_predicate_type``
(``EXACT``/``STARTS_WITH``) is the identical VOCABULARY the proven
``MailboxDomainRule`` ``EXACT_DOMAIN_SUBJECT`` tier already uses (no
``CONTAINS``, no regex, no glob — a real classification rule must always
be mechanically explainable to Matt), but it is this module's OWN
separate closed set (:data:`SUBJECT_PREDICATE_TYPES`), not an import of
``services.mailbox.domain_rule``'s own predicate constants.

No ``ATTACHMENT_FILENAME`` sender scope in V1 — deliberately, not an
oversight. There is no governed attachment-extraction pipeline yet;
exposing a rule shape the runtime cannot truthfully evaluate would be
dishonest (mirrors this codebase's "no fake buttons" doctrine, see
``ai.invocation``'s own module docstring citing the same principle for
a `CANCELLED` GUI trigger). Do not add it "for completeness" in a future
delivery without first building the pipeline it would depend on.

Normalization — reused, not re-implemented, from
``services.mailbox.domain_rule``
------------------------------------------------------------------------
:func:`services.mailbox.domain_rule.normalize_domain`,
:func:`services.mailbox.domain_rule.normalize_address`, and
:func:`services.mailbox.domain_rule.normalize_subject_for_policy` are
imported and reused DIRECTLY here (NFKC-normalise, casefold, strip,
collapse internal whitespace for subjects; strip+lowercase for
domains/addresses) — this is a deliberate, WO-authorised cross-module
import of pure, already-correct, dependency-free helper functions, never
a copy-paste. There is no live matcher in THIS module yet (that is
WI-2's job), so there is nothing here to refactor into a shared location
today. **A future delivery (WI-2) is expected to extract these three
helpers into a neutral shared module (e.g.
``services/shared/text_normalization.py``) and re-export them from
``services.mailbox.domain_rule`` for backward compatibility** — that
refactor is explicitly OUT of WI-1's scope; do not perform it now.

``document_type`` is imported FROM ``services.evidence.classification``
(the single source of truth for the closed V1 vocabulary both modules
share) — this module never re-declares that frozenset independently.
The dependency direction is deliberate: ``classification.py`` never
imports this module at all (its own ``rule_repository`` dependency is
duck-typed — see that module's own docstring), so importing
``DOCUMENT_TYPES`` here creates no import cycle.

Rule semantic immutability — only ``status``/``retired_at`` ever change
------------------------------------------------------------------------
``sender_scope_type``, ``sender_scope_value``, ``subject_predicate_type``,
``subject_predicate_value``, and ``document_type`` are set once at
creation and NEVER mutated by any repository method afterward. The only
mutable-in-place fields are the lifecycle pair ``status``/``retired_at``,
via :meth:`EvidenceClassificationRuleRepository.retire_rule` — a single
ONE-WAY ``ACTIVE`` -> ``RETIRED`` transition, never the reverse in V1
(unlike ``MailboxDomainRule.policy``'s own fully-reversible three-state
graph). To express "this rule's real answer changed": retire the old
rule, then create a brand NEW rule row, optionally pointing its own
``supersedes_rule_id`` back at the retired one. WI-1 deliberately does
NOT implement a single combined atomic retire+create method — no API
layer calls either primitive yet, so there is nothing to make atomic
across two calls today; a future API-layer caller is responsible for
performing them together as one governed operation when that becomes
real.

Active-rule identity — structurally must never collide
------------------------------------------------------------------------
``(sender_scope_type, normalized sender_scope_value,
subject_predicate_type, normalized subject_predicate_value)`` — at most
ONE ``ACTIVE`` rule may exist at a given identity (Postgres: a partial
unique index scoped ``WHERE status = 'ACTIVE'``; in-memory: a dict keyed
identically, checked only against currently-ACTIVE rows — see
``persistence/postgres/evidence_classification_rule_models.py``'s own
module docstring for the exact index). A creation attempt that collides
with an existing ACTIVE identity:

* if the new call's ``document_type`` is IDENTICAL to the existing
  ACTIVE rule's own ``document_type`` — an exact semantic replay of an
  already-governed decision — returns the EXISTING row unchanged
  (a harmless idempotent no-op, mirroring
  ``services.mailbox.domain_rule.MailboxDomainRuleRepository
  .upsert_rule``'s own "a redundant same-state action must never fail"
  doctrine);
* otherwise (a genuinely DIFFERENT ``document_type`` at an
  already-occupied ACTIVE identity) raises ``core.errors.ConflictError``
  — the caller is responsible for retiring the old rule FIRST (a
  separate call) before creating the replacement.

Precedence — recorded, not implemented
------------------------------------------------------------------------
WI-2 will implement the real matcher against live messages; WI-1 only
states the precedence doctrine clearly here so WI-2 has an unambiguous
starting point (and so a future test can exercise the DATA MODEL's
ability to represent it — no ``find_for_sender``-equivalent live
resolver function exists in this module). Most-specific-wins ordering,
identical in spirit to ``MailboxDomainRule.find_for_sender``'s own
proven ``EXACT_DOMAIN_SUBJECT`` tier:

1. ``EXACT_SENDER_ADDRESS`` beats ``EXACT_SENDER_DOMAIN``.
2. Within either sender tier, subject predicate ``EXACT`` beats
   ``STARTS_WITH``.
3. Among matching ``STARTS_WITH`` rules, the LONGER
   ``subject_predicate_value`` wins.
4. Two predicates of genuinely equal specificity that would both match
   the same subject must FAIL CLOSED (``core.errors.ConflictError``),
   never silently pick one — exactly
   ``MailboxDomainRule.find_for_sender``'s own documented "ambiguous
   resolution" doctrine for its own ``EXACT_DOMAIN_SUBJECT`` tier,
   applied here to a different rule table.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.evidence.classification import DOCUMENT_TYPES
from services.mailbox.domain_rule import normalize_address, normalize_domain, normalize_subject_for_policy

_SCHEMA = "evidence/bagman.evidence_classification_rule.v1.schema.json"
SCHEMA_VERSION = "bagman.evidence_classification_rule.v1"

SENDER_SCOPE_EXACT_SENDER_DOMAIN = "EXACT_SENDER_DOMAIN"
SENDER_SCOPE_EXACT_SENDER_ADDRESS = "EXACT_SENDER_ADDRESS"

#: Deliberately exactly two — see module docstring's "no
#: ATTACHMENT_FILENAME" section.
SENDER_SCOPE_TYPES = frozenset({SENDER_SCOPE_EXACT_SENDER_DOMAIN, SENDER_SCOPE_EXACT_SENDER_ADDRESS})

SUBJECT_PREDICATE_EXACT = "EXACT"
SUBJECT_PREDICATE_STARTS_WITH = "STARTS_WITH"

#: This module's OWN separate closed set — see module docstring.
SUBJECT_PREDICATE_TYPES = frozenset({SUBJECT_PREDICATE_EXACT, SUBJECT_PREDICATE_STARTS_WITH})

RULE_STATUS_ACTIVE = "ACTIVE"
RULE_STATUS_RETIRED = "RETIRED"
RULE_STATUSES = frozenset({RULE_STATUS_ACTIVE, RULE_STATUS_RETIRED})

RULE_SOURCE_OPERATOR = "OPERATOR"
#: No producer for BAGMAN_PROPOSED exists yet — forward-declared only,
#: exactly like ``services.mailbox.domain_rule.SOURCE_BAGMAN_PROPOSED``,
#: so a future automated-proposal producer never needs a contract
#: migration to start using it.
RULE_SOURCE_BAGMAN_PROPOSED = "BAGMAN_PROPOSED"
RULE_SOURCES = frozenset({RULE_SOURCE_OPERATOR, RULE_SOURCE_BAGMAN_PROPOSED})

#: The identity key shape every uniqueness check below uses — see
#: module docstring's "Active-rule identity" section.
_RuleIdentity = tuple[str, str, str, str]


def normalize_sender_scope_value(sender_scope_type: str, sender_scope_value: str) -> str:
    """The ONE place every caller normalises a ``sender_scope_value``
    before either an identity comparison or storage — dispatches to
    :func:`services.mailbox.domain_rule.normalize_address` for
    ``EXACT_SENDER_ADDRESS`` and
    :func:`services.mailbox.domain_rule.normalize_domain` for
    ``EXACT_SENDER_DOMAIN``. Reused by both the in-memory and Postgres
    repository implementations so their identity keys can never drift
    apart."""
    if sender_scope_type == SENDER_SCOPE_EXACT_SENDER_ADDRESS:
        return normalize_address(sender_scope_value)
    return normalize_domain(sender_scope_value)


@dataclass(frozen=True)
class EvidenceClassificationRule:
    """One deterministic document-classification rule row (CD-6 Slice 5
    WI-1). See module docstring for the full identity/lifecycle
    doctrine. Semantic fields are set once at creation; only
    ``status``/``retired_at`` ever change, via :meth:`retire_rule`."""

    rule_id: str
    sender_scope_type: str
    sender_scope_value: str
    subject_predicate_type: str
    subject_predicate_value: str
    document_type: str
    status: str
    source: str
    created_at: datetime
    approved_at: datetime
    supersedes_rule_id: Optional[str] = None
    retired_at: Optional[datetime] = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/evidence/bagman.evidence_classification_rule.v1.schema.json``."""
        return {
            "rule_id": self.rule_id,
            "sender_scope_type": self.sender_scope_type,
            "sender_scope_value": self.sender_scope_value,
            "subject_predicate_type": self.subject_predicate_type,
            "subject_predicate_value": self.subject_predicate_value,
            "document_type": self.document_type,
            "status": self.status,
            "source": self.source,
            "supersedes_rule_id": self.supersedes_rule_id,
            "created_at": to_contract_string(self.created_at),
            "approved_at": to_contract_string(self.approved_at),
            "retired_at": to_contract_string(self.retired_at) if self.retired_at is not None else None,
            "schema_version": self.schema_version,
        }


def validate_rule_fields_or_raise(
    *,
    sender_scope_type: str,
    sender_scope_value: str,
    subject_predicate_type: str,
    subject_predicate_value: str,
    document_type: str,
    status: str,
    source: str,
    retired_at: Optional[datetime],
) -> None:
    """Shared validation both repository implementations call before
    persisting a rule. ``sender_scope_value``/``subject_predicate_value``
    are expected ALREADY NORMALISED by the caller (mirrors
    ``services.mailbox.domain_rule.validate_match_fields_or_raise``'s
    own "caller normalises first" discipline).

    Raises:
        core.errors.ValidationError: on any violation.
    """
    if sender_scope_type not in SENDER_SCOPE_TYPES:
        raise ValidationError(f"'{sender_scope_type}' is not a governed sender_scope_type — must be one of {sorted(SENDER_SCOPE_TYPES)}")
    if not sender_scope_value:
        raise ValidationError("sender_scope_value must normalise to a non-empty string")
    if sender_scope_type == SENDER_SCOPE_EXACT_SENDER_ADDRESS and "@" not in sender_scope_value:
        raise ValidationError(
            f"sender_scope_value '{sender_scope_value}' does not look like an email address, required for "
            f"sender_scope_type='{SENDER_SCOPE_EXACT_SENDER_ADDRESS}'"
        )
    if sender_scope_type == SENDER_SCOPE_EXACT_SENDER_DOMAIN and "@" in sender_scope_value:
        raise ValidationError(
            f"sender_scope_value '{sender_scope_value}' looks like an email address, but "
            f"sender_scope_type='{SENDER_SCOPE_EXACT_SENDER_DOMAIN}' requires a bare domain"
        )

    if subject_predicate_type not in SUBJECT_PREDICATE_TYPES:
        raise ValidationError(
            f"'{subject_predicate_type}' is not a governed subject_predicate_type — must be one of "
            f"{sorted(SUBJECT_PREDICATE_TYPES)}"
        )
    if not subject_predicate_value:
        raise ValidationError("subject_predicate_value must normalise to a non-empty string")

    if document_type not in DOCUMENT_TYPES:
        raise ValidationError(f"'{document_type}' is not a governed document_type — must be one of {sorted(DOCUMENT_TYPES)}")

    if status not in RULE_STATUSES:
        raise ValidationError(f"'{status}' is not a governed rule status — must be one of {sorted(RULE_STATUSES)}")
    if source not in RULE_SOURCES:
        raise ValidationError(f"'{source}' is not a governed rule source — must be one of {sorted(RULE_SOURCES)}")

    if status == RULE_STATUS_RETIRED and retired_at is None:
        raise ValidationError(f"status='{RULE_STATUS_RETIRED}' requires a real retired_at")
    if status == RULE_STATUS_ACTIVE and retired_at is not None:
        raise ValidationError(f"status='{RULE_STATUS_ACTIVE}' must never carry a retired_at")


class EvidenceClassificationRuleRepository(abc.ABC):
    """Repository abstraction for EvidenceClassificationRule."""

    @abc.abstractmethod
    def create_rule(
        self,
        *,
        sender_scope_type: str,
        sender_scope_value: str,
        subject_predicate_type: str,
        subject_predicate_value: str,
        document_type: str,
        source: str,
        supersedes_rule_id: Optional[str] = None,
    ) -> "EvidenceClassificationRule":
        """Create a new ``ACTIVE`` rule, or return the EXISTING ACTIVE
        rule at the same identity unchanged if this is an exact semantic
        replay (see module docstring's "Active-rule identity" section).

        Raises:
            core.errors.ValidationError: field-shape violations.
            core.errors.NotFoundError: ``supersedes_rule_id`` does not
                reference a real row.
            core.errors.ConflictError: a DIFFERENT ``document_type`` at
                an already-occupied ACTIVE identity.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_rule(self, rule_id: str) -> "EvidenceClassificationRule":
        raise NotImplementedError

    @abc.abstractmethod
    def retire_rule(self, rule_id: str) -> "EvidenceClassificationRule":
        """The single one-way ``ACTIVE`` -> ``RETIRED`` transition (see
        module docstring). Stamps ``retired_at``; every other field is
        unchanged.

        Raises:
            core.errors.NotFoundError: no such rule.
            core.errors.InvalidStateTransitionError: ``rule_id`` is not
                currently ``ACTIVE``.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def find_active_rule_at_identity(
        self,
        sender_scope_type: str,
        sender_scope_value: str,
        subject_predicate_type: str,
        subject_predicate_value: str,
    ) -> Optional["EvidenceClassificationRule"]:
        """The currently-ACTIVE rule at this exact identity, if any.
        Returns ``None`` — never ``NotFoundError`` — if none exists."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_rules(
        self,
        *,
        status: Optional[str] = None,
        sender_scope_type: Optional[str] = None,
        document_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list["EvidenceClassificationRule"]:
        """List rules, ordered ``created_at`` ascending with ``rule_id``
        as a deterministic tie-breaker (mirrors
        ``MailboxDomainRuleRepository.list_rules``'s own shape — every
        filter optional/omitted == match all; ``limit=None`` returns
        every matching record)."""
        raise NotImplementedError


class InMemoryEvidenceClassificationRuleRepository(EvidenceClassificationRuleRepository):
    """Narrow in-memory reference implementation."""

    def __init__(self) -> None:
        self._by_id: dict[str, EvidenceClassificationRule] = {}
        #: identity tuple -> rule_id of the currently-ACTIVE rule at
        #: that identity, if any (mirrors the Postgres partial unique
        #: index scoped WHERE status = 'ACTIVE').
        self._active_id_by_identity: dict[_RuleIdentity, str] = {}

    @staticmethod
    def _identity_key(
        sender_scope_type: str, sender_scope_value: str, subject_predicate_type: str, subject_predicate_value: str
    ) -> _RuleIdentity:
        return (
            sender_scope_type,
            normalize_sender_scope_value(sender_scope_type, sender_scope_value),
            subject_predicate_type,
            normalize_subject_for_policy(subject_predicate_value) or "",
        )

    def create_rule(
        self,
        *,
        sender_scope_type: str,
        sender_scope_value: str,
        subject_predicate_type: str,
        subject_predicate_value: str,
        document_type: str,
        source: str,
        supersedes_rule_id: Optional[str] = None,
    ) -> EvidenceClassificationRule:
        normalized_scope_value = normalize_sender_scope_value(sender_scope_type, sender_scope_value)
        normalized_subject_value = normalize_subject_for_policy(subject_predicate_value)

        validate_rule_fields_or_raise(
            sender_scope_type=sender_scope_type, sender_scope_value=normalized_scope_value,
            subject_predicate_type=subject_predicate_type, subject_predicate_value=normalized_subject_value or "",
            document_type=document_type, status=RULE_STATUS_ACTIVE, source=source, retired_at=None,
        )

        if supersedes_rule_id is not None:
            self.get_rule(supersedes_rule_id)  # NotFoundError propagates if missing

        key = self._identity_key(sender_scope_type, normalized_scope_value, subject_predicate_type, normalized_subject_value)
        existing_id = self._active_id_by_identity.get(key)
        if existing_id is not None:
            existing = self._by_id[existing_id]
            if existing.document_type == document_type:
                return existing  # exact semantic replay -> idempotent no-op
            raise ConflictError(
                f"an ACTIVE EvidenceClassificationRule already exists at this identity with a different "
                f"document_type ('{existing.document_type}' != '{document_type}') — retire the existing rule "
                "first, then create the replacement (WI-1 does not implement a combined retire+create operation)"
            )

        now = utc_now()
        try:
            candidate = EvidenceClassificationRule(
                rule_id=identity.generate_id(),
                sender_scope_type=sender_scope_type,
                sender_scope_value=normalized_scope_value,
                subject_predicate_type=subject_predicate_type,
                subject_predicate_value=normalized_subject_value or "",
                document_type=document_type,
                status=RULE_STATUS_ACTIVE,
                source=source,
                supersedes_rule_id=supersedes_rule_id,
                created_at=now,
                approved_at=now,
                retired_at=None,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create EvidenceClassificationRule: {exc}") from exc

        self._by_id[candidate.rule_id] = candidate
        self._active_id_by_identity[key] = candidate.rule_id
        return candidate

    def get_rule(self, rule_id: str) -> EvidenceClassificationRule:
        try:
            return self._by_id[rule_id]
        except KeyError:
            raise NotFoundError(f"no EvidenceClassificationRule with rule_id '{rule_id}'") from None

    def retire_rule(self, rule_id: str) -> EvidenceClassificationRule:
        current = self.get_rule(rule_id)
        if current.status != RULE_STATUS_ACTIVE:
            raise InvalidStateTransitionError(
                f"EvidenceClassificationRule '{rule_id}' cannot be retired from status '{current.status}' — "
                "only an ACTIVE rule may be retired (one-way ACTIVE -> RETIRED transition, never the reverse)"
            )
        now = utc_now()
        try:
            updated = dataclasses.replace(current, status=RULE_STATUS_RETIRED, retired_at=now)
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not retire EvidenceClassificationRule: {exc}") from exc

        self._by_id[rule_id] = updated
        key = self._identity_key(
            updated.sender_scope_type, updated.sender_scope_value,
            updated.subject_predicate_type, updated.subject_predicate_value,
        )
        if self._active_id_by_identity.get(key) == rule_id:
            del self._active_id_by_identity[key]
        return updated

    def find_active_rule_at_identity(
        self,
        sender_scope_type: str,
        sender_scope_value: str,
        subject_predicate_type: str,
        subject_predicate_value: str,
    ) -> Optional[EvidenceClassificationRule]:
        key = self._identity_key(sender_scope_type, sender_scope_value, subject_predicate_type, subject_predicate_value)
        rule_id = self._active_id_by_identity.get(key)
        return self._by_id[rule_id] if rule_id is not None else None

    def list_rules(
        self,
        *,
        status: Optional[str] = None,
        sender_scope_type: Optional[str] = None,
        document_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[EvidenceClassificationRule]:
        rules = list(self._by_id.values())
        if status is not None:
            rules = [r for r in rules if r.status == status]
        if sender_scope_type is not None:
            rules = [r for r in rules if r.sender_scope_type == sender_scope_type]
        if document_type is not None:
            rules = [r for r in rules if r.document_type == document_type]
        rules.sort(key=lambda r: (r.created_at, r.rule_id))
        if limit is None:
            return rules[offset:]
        return rules[offset : offset + limit]
