"""``EvidenceClassification`` — the durable, append-only record of "what
kind of document is this evidence item" (CD-6 Slice 5 WI-1).

This module is the classification-record half of Slice 5's data model.
Its sibling, ``services.evidence.classification_rule``, is the
deterministic-RULE half (the thing that, in WI-2, will *produce*
``DETERMINISTIC_RULE``-sourced classifications for a live message). WI-1
builds only the persistence + domain model for both — no AI execution,
no Needs You producer, no HTTP endpoint, no GUI (see the WO's own
explicit prohibition list; this module never imports ``ai.gateway``,
``ai.providers``, or anything under ``app/``).

Architectural precedent this module deliberately mirrors
------------------------------------------------------------------------
``services.mailbox.domain_rule`` is the closest precedent in this
codebase for everything here: a frozen dataclass domain model, an ABC
repository + in-memory reference implementation, a
``validate_*_fields_or_raise`` function raising
``core.errors.ValidationError``, and a governed, closed-vocabulary
lifecycle. This module is built in that exact style, not a new one.

Why a classification is NEVER a field on ``EvidenceItem``
------------------------------------------------------------------------
``services.evidence.evidence.EvidenceItem`` (PID §6-8) already has a
hard-earned "original evidence is immutable" doctrine — the WO's own
explicit architectural invariant for this delivery is that
``EvidenceItem`` is NOT touched at all (no new column, no new field, no
migration) — a classification is a SEPARATE, append-only opinion ABOUT
an ``EvidenceItem``, addressed by a real (but never a hard ownership)
``evidence_id`` reference, never a mutation of the evidence row itself.
This mirrors how ``AIInvocation`` (CD-5) also never touches
``EvidenceItem`` — it merely references it via ``input_references``.

Why rows are immutable and append-only — no status-transition method
------------------------------------------------------------------------
Unlike ``MailboxDomainRule.policy`` (a real, governed, in-place state
machine — see that module's own ``transition_policy``) or
``AIInvocation.status`` (a real terminal-state machine — see
``ai.invocation.transition``), an ``EvidenceClassification`` row is
created once and NEVER changes again. There is deliberately no
``update``/``transition`` method anywhere on this repository. A
correction is always expressed as a brand NEW row whose own
``supersedes_classification_id`` points back at the row it replaces —
see "Supersession semantics" below. This is closer to
``core.audit.AuditEvent``'s own append-only doctrine than to either
state-machine precedent above.

Supersession semantics — the "current classification" is a QUERY, not
a stored flag
------------------------------------------------------------------------
``supersedes_classification_id`` (nullable) links a new classification
row back to the specific prior row (of the SAME ``evidence_id`` +
``classification_type`` — enforced before insert, never something a
plain foreign key alone can express) it corrects. There is deliberately
NO ``is_current``/``SUPERSEDED`` boolean or status value anywhere in
this schema: **the "current classification" for a given
``(evidence_id, classification_type)`` is defined structurally as "the
row no OTHER row's ``supersedes_classification_id`` points at"** — a
genuine ``NOT EXISTS``/anti-join query
(:meth:`EvidenceClassificationRepository.get_current_classification`),
never a stored, independently-updatable flag that could drift out of
sync with the rows it is supposed to describe. A row that HAS been
superseded still carries whatever ``status``/``document_type`` it was
originally created with, unchanged forever — supersession is expressed
purely by some OTHER row's ``supersedes_classification_id`` pointing at
it, never by mutating the superseded row itself (which cannot happen at
all — rows are immutable).

**Branch prevention.** At most one row may supersede any given
``classification_id`` — a real DB-level partial unique index on
``supersedes_classification_id`` (Postgres) plus the identical
application-level check (in-memory), so the lineage for a given
``(evidence_id, classification_type)`` is always a single linear chain,
never a tree. See
``persistence/postgres/evidence_classification_models.py``'s own module
docstring for the exact index.

**Concurrency-safe creation.** A caller creating a classification
supplies ``expected_current_classification_id`` — ``None`` for a
genuinely first/initial classification, or the ``classification_id`` the
caller BELIEVES is currently current when creating a superseding row.
:meth:`EvidenceClassificationRepository.create_classification` resolves
the ACTUAL current classification and raises
``core.errors.ConflictError`` if it does not match what the caller
expected — a real optimistic-concurrency check-then-act, backed on
Postgres by a row lock on the referenced ``EvidenceItem`` row (see
``persistence.postgres.evidence_classification_repository`` module
docstring for the exact transaction shape) so two genuinely concurrent
callers racing to supersede the SAME tip can never both win.

Producer idempotency — a SEPARATE concern from supersession-chain
integrity
------------------------------------------------------------------------
A given "producer" (one specific ``rule_id``, one specific
``ai_invocation_id``, or one specific ``operator_action_id``) creating a
classification for the same ``(evidence_id, classification_type)`` a
SECOND time (a genuine retry/replay of the exact same governed act) must
return the EXISTING row unchanged, never create a duplicate — this is
producer-level idempotent replay, enforced via three separate partial
unique indexes (one per ``source``) and mirrored in the in-memory
repository. This is deliberately independent of the supersession/branch
machinery above: a producer replay is "the same governed act happened
twice", not "a correction was made". "Same AI ``task_version``" is
NEVER sufficient identity for this — identity is the exact
``ai_invocation_id`` (one specific governed invocation row); a genuinely
new AI run gets a new ``AIInvocation`` row and therefore may legitimately
create a new classification (deciding WHEN to re-run is WI-3's concern,
not this module's).

Referenced-entity existence validation — "prove real before trusting"
------------------------------------------------------------------------
Before inserting, :meth:`create_classification` proves every reference
it is about to persist actually exists, by calling the OWNING
repository for that reference and letting its own ``NotFoundError``
propagate honestly — never a local, possibly-stale existence check of
its own (mirrors ``services.evidence.evidence`` and
``services.mailbox.domain_rule.validate_and_normalize_sender_address``'s
own "prove real before trusting" doctrine, and the "existence check
first" ordering ``assign_evidence_entity``-shaped callers use elsewhere
in this codebase):

* ``evidence_id`` -> ``EvidenceRepository.get_evidence``.
* ``source == DETERMINISTIC_RULE`` -> ``rule_id`` ->
  ``EvidenceClassificationRuleRepository.get_rule`` (see
  ``services.evidence.classification_rule``).
* ``source == AI_PROPOSAL`` -> ``ai_invocation_id`` ->
  ``AIInvocationRepository.get_invocation``, AND its ``.status`` must be
  exactly ``"SUCCEEDED"`` (the literal string — see
  ``ai.invocation.STATUSES``/``TERMINAL_STATUSES``) — a classification
  may never be linked to FAILED/REJECTED/TIMED_OUT/CANCELLED AI work.

These three repositories are injected (constructor parameters), never
imported as concrete classes — this module has zero imports from
``persistence.postgres.*``, ``ai.providers.*``, or any AI orchestration
code, exactly like ``services.mailbox.domain_rule`` never imports
``persistence.postgres.*``. In particular, this module does NOT import
``services.evidence.classification_rule`` at all — the
``rule_repository`` dependency is duck-typed (only
``.get_rule(rule_id)`` is ever called on it), the same "duck-typed,
never a type import back into the dependency's own module" discipline
``services.mailbox.domain_rule.validate_and_normalize_sender_address``
already establishes for its own ``message_repository`` parameter (and,
incidentally, keeps this module import-cycle-free against
``classification_rule``, which itself imports ``DOCUMENT_TYPES`` FROM
this module).

``classification_type`` — intentionally open, currently single-valued
------------------------------------------------------------------------
The WO's own instruction: ``classification_type`` is a plain string
field in the contract (never a closed JSON Schema enum) so a FUTURE
classification dimension (e.g. something beyond "what kind of document
is this") never needs a contract migration to be added — but, for V1,
nothing except ``"DOCUMENT_TYPE"`` is registered, so
:func:`validate_classification_fields_or_raise` enforces that exact
value at the Python layer until a second dimension is genuinely
registered.

``reason_codes`` — bounded short strings, never document content
------------------------------------------------------------------------
A list of short, closed-vocabulary-shaped machine reason codes (e.g.
what specifically drove a ``REVIEW_REQUIRED``/``UNCLASSIFIABLE``
outcome) — may be empty. This field must NEVER carry raw extracted
document text/body content (that belongs to the evidence's own stored
bytes, never to a classification row); V1 does not enforce a length
bound beyond "it is a list of strings" (no registered vocabulary of
codes exists yet to validate membership against), but the doctrine is
recorded here for whoever adds the first real producer.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "evidence/bagman.evidence_classification.v1.schema.json"
SCHEMA_VERSION = "bagman.evidence_classification.v1"

# ---------------------------------------------------------------------
# Closed V1 vocabularies
# ---------------------------------------------------------------------

DOCUMENT_TYPE_SUPPLIER_INVOICE = "SUPPLIER_INVOICE"
DOCUMENT_TYPE_RECEIPT = "RECEIPT"
DOCUMENT_TYPE_ORDER_CONFIRMATION = "ORDER_CONFIRMATION"
DOCUMENT_TYPE_REFUND_CONFIRMATION = "REFUND_CONFIRMATION"
DOCUMENT_TYPE_BROKER_STATEMENT = "BROKER_STATEMENT"
DOCUMENT_TYPE_BROKER_ACTIVITY_NOTICE = "BROKER_ACTIVITY_NOTICE"
DOCUMENT_TYPE_NON_ACCOUNTING_DOCUMENT = "NON_ACCOUNTING_DOCUMENT"
DOCUMENT_TYPE_UNKNOWN = "UNKNOWN"

#: The closed V1 ``document_type`` vocabulary — shared verbatim by
#: ``services.evidence.classification_rule`` (imported FROM here; see
#: that module's own docstring for why the dependency runs in this
#: direction and not the reverse).
DOCUMENT_TYPES = frozenset(
    {
        DOCUMENT_TYPE_SUPPLIER_INVOICE,
        DOCUMENT_TYPE_RECEIPT,
        DOCUMENT_TYPE_ORDER_CONFIRMATION,
        DOCUMENT_TYPE_REFUND_CONFIRMATION,
        DOCUMENT_TYPE_BROKER_STATEMENT,
        DOCUMENT_TYPE_BROKER_ACTIVITY_NOTICE,
        DOCUMENT_TYPE_NON_ACCOUNTING_DOCUMENT,
        DOCUMENT_TYPE_UNKNOWN,
    }
)

STATUS_CLASSIFIED = "CLASSIFIED"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_UNCLASSIFIABLE = "UNCLASSIFIABLE"

#: Exactly three — deliberately NO ``SUPERSEDED`` value (see module
#: docstring's "Supersession semantics" section: supersession is
#: expressed structurally, never as a status value).
CLASSIFICATION_STATUSES = frozenset({STATUS_CLASSIFIED, STATUS_REVIEW_REQUIRED, STATUS_UNCLASSIFIABLE})

SOURCE_DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
SOURCE_AI_PROPOSAL = "AI_PROPOSAL"
SOURCE_OPERATOR_ASSIGNED = "OPERATOR_ASSIGNED"

CLASSIFICATION_SOURCES = frozenset({SOURCE_DETERMINISTIC_RULE, SOURCE_AI_PROPOSAL, SOURCE_OPERATOR_ASSIGNED})

#: The only registered ``classification_type`` value in V1 — see module
#: docstring's "intentionally open, currently single-valued" section.
CLASSIFICATION_TYPE_DOCUMENT_TYPE = "DOCUMENT_TYPE"

#: The literal AIInvocation terminal status a AI_PROPOSAL classification
#: may be linked to — see ``ai.invocation.STATUSES``. Public (not
#: module-private) because
#: ``persistence.postgres.evidence_classification_repository`` reuses
#: this exact constant rather than re-declaring the literal string a
#: second time.
AI_INVOCATION_SUCCEEDED_STATUS = "SUCCEEDED"


@dataclass(frozen=True)
class EvidenceClassification:
    """One immutable, append-only classification row (CD-6 Slice 5
    WI-1). See module docstring for the full supersession/idempotency
    doctrine. Never mutated in place; a correction is always a NEW row."""

    classification_id: str
    evidence_id: str
    classification_type: str
    document_type: str
    status: str
    source: str
    created_at: datetime
    confidence: Optional[float] = None
    rule_id: Optional[str] = None
    ai_invocation_id: Optional[str] = None
    operator_action_id: Optional[str] = None
    reason_codes: Sequence[str] = field(default_factory=tuple)
    supersedes_classification_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/evidence/bagman.evidence_classification.v1.schema.json``."""
        return {
            "classification_id": self.classification_id,
            "evidence_id": self.evidence_id,
            "classification_type": self.classification_type,
            "document_type": self.document_type,
            "status": self.status,
            "source": self.source,
            "confidence": self.confidence,
            "rule_id": self.rule_id,
            "ai_invocation_id": self.ai_invocation_id,
            "operator_action_id": self.operator_action_id,
            "reason_codes": list(self.reason_codes),
            "supersedes_classification_id": self.supersedes_classification_id,
            "created_at": to_contract_string(self.created_at),
            "schema_version": self.schema_version,
        }


def validate_classification_fields_or_raise(
    *,
    classification_type: str,
    document_type: str,
    status: str,
    source: str,
    confidence: Optional[float],
    rule_id: Optional[str],
    ai_invocation_id: Optional[str],
    operator_action_id: Optional[str],
) -> None:
    """Enforce every §2 invariant the WO specifies — both repository
    implementations call this BEFORE any referenced-entity existence
    check or persistence attempt. Mirrors the exact structure of
    ``services.mailbox.domain_rule.validate_policy_fields_or_raise``.

    Raises:
        core.errors.ValidationError: on any violation.
    """
    if classification_type != CLASSIFICATION_TYPE_DOCUMENT_TYPE:
        raise ValidationError(
            f"classification_type must be '{CLASSIFICATION_TYPE_DOCUMENT_TYPE}' in this delivery "
            f"(the field is open in the contract for a future dimension, but nothing else is "
            f"registered yet); got {classification_type!r}"
        )
    if document_type not in DOCUMENT_TYPES:
        raise ValidationError(f"'{document_type}' is not a governed document_type — must be one of {sorted(DOCUMENT_TYPES)}")
    if status not in CLASSIFICATION_STATUSES:
        raise ValidationError(f"'{status}' is not a governed classification status — must be one of {sorted(CLASSIFICATION_STATUSES)}")
    if source not in CLASSIFICATION_SOURCES:
        raise ValidationError(f"'{source}' is not a governed classification source — must be one of {sorted(CLASSIFICATION_SOURCES)}")

    # status / document_type cross-field invariants.
    if status == STATUS_CLASSIFIED and document_type == DOCUMENT_TYPE_UNKNOWN:
        raise ValidationError(
            f"status='{STATUS_CLASSIFIED}' requires document_type != '{DOCUMENT_TYPE_UNKNOWN}'"
        )
    if status == STATUS_UNCLASSIFIABLE and document_type != DOCUMENT_TYPE_UNKNOWN:
        raise ValidationError(
            f"status='{STATUS_UNCLASSIFIABLE}' requires document_type == '{DOCUMENT_TYPE_UNKNOWN}' "
            f"(got {document_type!r})"
        )
    # STATUS_REVIEW_REQUIRED: document_type may be any valid value,
    # including UNKNOWN — no additional constraint.

    # source cross-field invariants (§2's DETERMINISTIC_RULE/AI_PROPOSAL/
    # OPERATOR_ASSIGNED rules).
    if source == SOURCE_DETERMINISTIC_RULE:
        if not rule_id:
            raise ValidationError(f"source='{SOURCE_DETERMINISTIC_RULE}' requires a real rule_id")
        if ai_invocation_id is not None or operator_action_id is not None or confidence is not None:
            raise ValidationError(
                f"source='{SOURCE_DETERMINISTIC_RULE}' must never carry ai_invocation_id/operator_action_id/"
                "confidence — never manufacture a confidence value for a deterministic rule match"
            )
    elif source == SOURCE_AI_PROPOSAL:
        if not ai_invocation_id:
            raise ValidationError(f"source='{SOURCE_AI_PROPOSAL}' requires a real ai_invocation_id")
        if rule_id is not None or operator_action_id is not None:
            raise ValidationError(f"source='{SOURCE_AI_PROPOSAL}' must never carry rule_id/operator_action_id")
        if confidence is None or not (0.0 <= confidence <= 1.0):
            raise ValidationError(
                f"source='{SOURCE_AI_PROPOSAL}' requires a real confidence in [0.0, 1.0] (got {confidence!r})"
            )
    else:  # source == SOURCE_OPERATOR_ASSIGNED
        if not operator_action_id:
            raise ValidationError(f"source='{SOURCE_OPERATOR_ASSIGNED}' requires a real, non-empty operator_action_id")
        if rule_id is not None or ai_invocation_id is not None or confidence is not None:
            raise ValidationError(
                f"source='{SOURCE_OPERATOR_ASSIGNED}' must never carry rule_id/ai_invocation_id/confidence"
            )


class EvidenceClassificationRepository(abc.ABC):
    """Repository abstraction for EvidenceClassification. See module
    docstring for the full supersession/idempotency/existence-validation
    doctrine every implementation must honour identically."""

    @abc.abstractmethod
    def create_classification(
        self,
        *,
        evidence_id: str,
        classification_type: str,
        document_type: str,
        status: str,
        source: str,
        confidence: Optional[float] = None,
        rule_id: Optional[str] = None,
        ai_invocation_id: Optional[str] = None,
        operator_action_id: Optional[str] = None,
        reason_codes: Optional[Sequence[str]] = None,
        supersedes_classification_id: Optional[str] = None,
        expected_current_classification_id: Optional[str] = None,
    ) -> "EvidenceClassification":
        """Create a new classification row, or return the EXISTING row
        unchanged on a genuine producer-identity replay (see module
        docstring's "Producer idempotency" section).

        ``expected_current_classification_id``: ``None`` for a
        genuinely first/initial classification of this
        ``(evidence_id, classification_type)``; otherwise the
        ``classification_id`` the caller believes is currently current.
        Raises ``core.errors.ConflictError`` if the actual current
        classification does not match (see module docstring's
        "Concurrency-safe creation" section).

        Raises:
            core.errors.ValidationError: field-shape/cross-field
                violations (see :func:`validate_classification_fields_or_raise`).
            core.errors.NotFoundError: ``evidence_id``/``rule_id``/
                ``ai_invocation_id``/``supersedes_classification_id``
                does not reference a real row.
            core.errors.ConflictError: stale ``expected_current_classification_id``,
                a ``supersedes_classification_id`` that is already
                superseded by another row (branch prevention), or a
                ``supersedes_classification_id`` from a different
                ``evidence_id``/``classification_type``.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_classification(self, classification_id: str) -> "EvidenceClassification":
        raise NotImplementedError

    @abc.abstractmethod
    def get_current_classification(
        self, evidence_id: str, classification_type: str
    ) -> Optional["EvidenceClassification"]:
        """The row no OTHER row's ``supersedes_classification_id``
        points at, for this ``(evidence_id, classification_type)`` — a
        genuine query, never a stored flag (see module docstring).
        Returns ``None`` if no classification of this type exists yet
        for this evidence item."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_classification_history(
        self, evidence_id: str, classification_type: str
    ) -> list["EvidenceClassification"]:
        """The full lineage for this ``(evidence_id, classification_type)``,
        OLDEST FIRST (i.e. the original classification first, the
        current tip last)."""
        raise NotImplementedError


class InMemoryEvidenceClassificationRepository(EvidenceClassificationRepository):
    """Narrow in-memory reference implementation. This is NOT a
    simplified stub — it implements the identical branch-prevention/
    expected-current/producer-idempotency semantics the Postgres
    implementation enforces via real database constraints, entirely in
    application code, so both backends behave identically to callers
    (and to the shared test suite that exercises both).

    Dependencies (``evidence_repository``, ``rule_repository``,
    ``ai_invocation_repository``) are constructor-injected and
    duck-typed — this module never imports a concrete implementation of
    any of them (see module docstring).
    """

    def __init__(self, *, evidence_repository, rule_repository, ai_invocation_repository) -> None:
        self._evidence_repository = evidence_repository
        self._rule_repository = rule_repository
        self._ai_invocation_repository = ai_invocation_repository

        self._by_id: dict[str, EvidenceClassification] = {}
        #: (evidence_id, classification_type) -> ordered list of
        #: classification_ids, oldest first — the full lineage.
        self._history: dict[tuple[str, str], list[str]] = {}
        #: superseded classification_id -> the classification_id that
        #: supersedes it. A classification_id present here as a KEY has
        #: been superseded; one absent is the current tip of its lineage.
        self._superseded_by: dict[str, str] = {}
        #: producer identity tuple -> classification_id, one dict per
        #: source (mirrors the three separate partial unique indexes on
        #: Postgres — see persistence/postgres/evidence_classification_models.py).
        self._id_by_producer_key: dict[tuple, str] = {}

    @staticmethod
    def _producer_key(
        *, evidence_id: str, classification_type: str, source: str,
        rule_id: Optional[str], ai_invocation_id: Optional[str], operator_action_id: Optional[str],
    ) -> tuple:
        if source == SOURCE_DETERMINISTIC_RULE:
            return (evidence_id, classification_type, source, rule_id)
        if source == SOURCE_AI_PROPOSAL:
            return (evidence_id, classification_type, source, ai_invocation_id)
        return (evidence_id, classification_type, source, operator_action_id)

    def create_classification(
        self,
        *,
        evidence_id: str,
        classification_type: str,
        document_type: str,
        status: str,
        source: str,
        confidence: Optional[float] = None,
        rule_id: Optional[str] = None,
        ai_invocation_id: Optional[str] = None,
        operator_action_id: Optional[str] = None,
        reason_codes: Optional[Sequence[str]] = None,
        supersedes_classification_id: Optional[str] = None,
        expected_current_classification_id: Optional[str] = None,
    ) -> EvidenceClassification:
        validate_classification_fields_or_raise(
            classification_type=classification_type, document_type=document_type, status=status, source=source,
            confidence=confidence, rule_id=rule_id, ai_invocation_id=ai_invocation_id,
            operator_action_id=operator_action_id,
        )

        # Referenced-entity existence validation — "prove real before
        # trusting" (see module docstring). Let each owning repository's
        # own NotFoundError propagate honestly.
        self._evidence_repository.get_evidence(evidence_id)
        if source == SOURCE_DETERMINISTIC_RULE:
            self._rule_repository.get_rule(rule_id)
        elif source == SOURCE_AI_PROPOSAL:
            invocation = self._ai_invocation_repository.get_invocation(ai_invocation_id)
            if invocation.status != AI_INVOCATION_SUCCEEDED_STATUS:
                raise ValidationError(
                    f"AIInvocation '{ai_invocation_id}' has status '{invocation.status}', not "
                    f"'{AI_INVOCATION_SUCCEEDED_STATUS}' — a classification may never be linked to "
                    "FAILED/REJECTED/TIMED_OUT/CANCELLED AI work"
                )

        # Producer idempotency — BEFORE any supersession/expected-current
        # check, so a genuine replay short-circuits cleanly regardless
        # of what the caller believed the current tip was.
        producer_key = self._producer_key(
            evidence_id=evidence_id, classification_type=classification_type, source=source,
            rule_id=rule_id, ai_invocation_id=ai_invocation_id, operator_action_id=operator_action_id,
        )
        existing_id = self._id_by_producer_key.get(producer_key)
        if existing_id is not None:
            return self._by_id[existing_id]

        if supersedes_classification_id is not None:
            superseded = self.get_classification(supersedes_classification_id)
            if superseded.evidence_id != evidence_id or superseded.classification_type != classification_type:
                raise ValidationError(
                    f"supersedes_classification_id '{supersedes_classification_id}' does not share this "
                    f"classification's own evidence_id/classification_type (superseded row has "
                    f"evidence_id={superseded.evidence_id!r}, classification_type={superseded.classification_type!r})"
                )

        current = self.get_current_classification(evidence_id, classification_type)
        current_id = current.classification_id if current is not None else None
        if current_id != expected_current_classification_id:
            raise ConflictError(
                f"expected_current_classification_id={expected_current_classification_id!r} does not match the "
                f"actual current EvidenceClassification for (evidence_id={evidence_id!r}, "
                f"classification_type={classification_type!r}), which is {current_id!r} — refusing to create a "
                "classification against a stale view of the current lineage tip"
            )

        # Branch prevention — at most one row may supersede any given
        # classification_id.
        if supersedes_classification_id is not None and supersedes_classification_id in self._superseded_by:
            raise ConflictError(
                f"classification_id '{supersedes_classification_id}' is already superseded by "
                f"'{self._superseded_by[supersedes_classification_id]}' — at most one row may supersede "
                "any given classification"
            )

        try:
            candidate = EvidenceClassification(
                classification_id=identity.generate_id(),
                evidence_id=evidence_id,
                classification_type=classification_type,
                document_type=document_type,
                status=status,
                source=source,
                created_at=utc_now(),
                confidence=confidence,
                rule_id=rule_id,
                ai_invocation_id=ai_invocation_id,
                operator_action_id=operator_action_id,
                reason_codes=tuple(reason_codes) if reason_codes else (),
                supersedes_classification_id=supersedes_classification_id,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create EvidenceClassification: {exc}") from exc

        self._by_id[candidate.classification_id] = candidate
        self._id_by_producer_key[producer_key] = candidate.classification_id
        if supersedes_classification_id is not None:
            self._superseded_by[supersedes_classification_id] = candidate.classification_id
        self._history.setdefault((evidence_id, classification_type), []).append(candidate.classification_id)
        return candidate

    def get_classification(self, classification_id: str) -> EvidenceClassification:
        try:
            return self._by_id[classification_id]
        except KeyError:
            raise NotFoundError(f"no EvidenceClassification with classification_id '{classification_id}'") from None

    def get_current_classification(
        self, evidence_id: str, classification_type: str
    ) -> Optional[EvidenceClassification]:
        ids = self._history.get((evidence_id, classification_type), [])
        tips = [cid for cid in ids if cid not in self._superseded_by]
        if not tips:
            return None
        # By construction (branch prevention + the expected-current
        # check), exactly one tip exists per lineage in ordinary
        # operation.
        return self._by_id[tips[-1]]

    def list_classification_history(
        self, evidence_id: str, classification_type: str
    ) -> list[EvidenceClassification]:
        ids = self._history.get((evidence_id, classification_type), [])
        return [self._by_id[cid] for cid in ids]
