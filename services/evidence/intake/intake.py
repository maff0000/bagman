"""Canonical IntakeRecord domain model, state machine, and repository
(PID §5-8, CD-4 WI-1).

An ``IntakeRecord`` represents a single untrusted upload/observation
ATTEMPT, not canonical evidence (PID §5). It is created the moment
BAGMAN receives an upload attempt and is durably updated as it moves
through the deterministic state machine below (PID §7) until it
reaches a terminal state — ``QUARANTINED``, ``REJECTED``,
``REGISTERED``, or ``FAILED``. Only ``ACCEPTED -> REGISTERED`` ever
sets ``evidence_id``; every other terminal path never produces
canonical evidence.

State machine (PID §7)
-----------------------
``ALLOWED_TRANSITIONS`` is the single source of truth for which edges
are valid::

    RECEIVED    -> {VALIDATING, REJECTED, FAILED}
    VALIDATING  -> {QUARANTINED, ACCEPTED, REJECTED, FAILED}
    ACCEPTED    -> {REGISTERED, FAILED}
    QUARANTINED -> {}   (terminal)
    REJECTED    -> {}   (terminal)
    REGISTERED  -> {}   (terminal)
    FAILED      -> {}   (terminal)

:func:`transition` is the only supported way to move a record between
states; it raises ``core.errors.InvalidStateTransitionError`` for any
edge not in this table, and automatically stamps ``completed_at`` the
moment a record reaches a terminal state (a caller may not supply
``completed_at`` for a non-terminal transition, and may not omit it —
implicitly or explicitly — for a terminal one; see the function's own
docstring).

Idempotency design decision (PID §25/§53) — READ BEFORE CHANGING
-------------------------------------------------------------------
PID §25 requires that a replay of the same idempotency key resolve to
the same ``IntakeRecord``, and that a conflicting reuse of a key with
*different content* fail loudly. At intake-*creation* time (this
module's scope — WI-1) BAGMAN does not yet have the uploaded bytes: no
``content_hash`` exists yet, because computing it is WI-2's streaming/
hashing responsibility, which runs strictly after a record already
exists in ``RECEIVED``. So "same content" cannot mean "same
content_hash" here — that proof is deferred, exactly as PID §25/§53
say it must be, to the later work items that actually have bytes.

What CD-4 WI-1 CAN and DOES decide, durably, is "same identifying
request": the tuple ``(source_id, entity_hint, original_filename,
reported_mime_type)`` supplied on the ``create_intake_record`` call
itself — the only request-shape information that exists before any
byte has been read. Given an ``idempotency_key`` that already maps to
an existing ``IntakeRecord``:

* identical identifying tuple  -> idempotent replay: return the
  existing record unchanged, no new record created (whatever state it
  is currently in — including a terminal one; the caller gets back the
  final outcome of the first attempt, exactly as PID §25 requires for
  "same valid key replay must resolve to the same IntakeRecord and
  final result").
* a DIFFERENT identifying tuple -> ``core.errors.IdempotencyConflictError``
  — a genuine, loud conflict, never a silent reuse of the key for
  different content (PID §25/§53).

This is a deliberately narrower guarantee than a full content-hash
comparison, and is intentionally so: the durable uniqueness constraint
and lookup mechanism this WI provides (the database's own partial
unique index on ``idempotency_key`` — see
``persistence/postgres/intake_models.py`` — plus
``find_by_idempotency_key``) is what makes the STRONGER content-hash-
based proof possible for WI-2/WI-5 to build on top of, once actual
bytes exist to hash. WI-1 does not, and must not, claim that stronger
proof itself.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "intake/bagman.intake_record.v1.schema.json"

#: CD-4 WI-1's only supported contract version for IntakeRecord; the
#: contract itself pins `schema_version` to this exact value via a
#: JSON Schema `const`, so it is never a caller-supplied parameter.
SCHEMA_VERSION = "bagman.intake_record.v1"

#: The seven intake states (PID §7). Matches the contract's closed
#: `status` enum exactly.
STATUSES = frozenset(
    {"RECEIVED", "VALIDATING", "QUARANTINED", "REJECTED", "ACCEPTED", "REGISTERED", "FAILED"}
)

#: States from which no further transition is possible (PID §7).
TERMINAL_STATUSES = frozenset({"QUARANTINED", "REJECTED", "REGISTERED", "FAILED"})

#: The single source of truth for valid intake state transitions (PID
#: §7). See the module docstring for the diagram this encodes.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED": frozenset({"VALIDATING", "REJECTED", "FAILED"}),
    "VALIDATING": frozenset({"QUARANTINED", "ACCEPTED", "REJECTED", "FAILED"}),
    "ACCEPTED": frozenset({"REGISTERED", "FAILED"}),
    "QUARANTINED": frozenset(),
    "REJECTED": frozenset(),
    "REGISTERED": frozenset(),
    "FAILED": frozenset(),
}

#: The fields WI-1 treats as "identifying request content" for
#: idempotency-conflict detection at creation time — see the module
#: docstring's "Idempotency design decision" section.
_IdentifyingTuple = tuple


def _identifying_fields(
    *,
    source_id: str,
    entity_hint: Optional[str],
    original_filename: Optional[str],
    reported_mime_type: Optional[str],
) -> _IdentifyingTuple:
    return (source_id, entity_hint, original_filename, reported_mime_type)


@dataclass(frozen=True)
class IntakeRecord:
    """A single untrusted upload/observation attempt (PID §5-8),
    distinct from canonical `EvidenceItem` identity. Immutable once
    constructed; every state transition produces a NEW `IntakeRecord`
    snapshot via :func:`transition` (frozen dataclasses are never
    mutated in place) that replaces the repository's current record
    for that `intake_id`.
    """

    intake_id: str
    source_id: str
    status: str
    received_at: datetime
    correlation_id: str
    entity_hint: Optional[str] = None
    completed_at: Optional[datetime] = None
    original_filename: Optional[str] = None
    reported_mime_type: Optional[str] = None
    detected_mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    content_hash: Optional[Mapping[str, str]] = None
    evidence_id: Optional[str] = None
    failure_code: Optional[str] = None
    quarantine_reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/intake/bagman.intake_record.v1.schema.json``."""
        return {
            "intake_id": self.intake_id,
            "source_id": self.source_id,
            "entity_hint": self.entity_hint,
            "status": self.status,
            "received_at": to_contract_string(self.received_at),
            "completed_at": to_contract_string(self.completed_at) if self.completed_at is not None else None,
            "original_filename": self.original_filename,
            "reported_mime_type": self.reported_mime_type,
            "detected_mime_type": self.detected_mime_type,
            "size_bytes": self.size_bytes,
            "content_hash": dict(self.content_hash) if self.content_hash is not None else None,
            "evidence_id": self.evidence_id,
            "failure_code": self.failure_code,
            "quarantine_reason": self.quarantine_reason,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }


def transition(record: IntakeRecord, new_status: str, **field_updates: Any) -> IntakeRecord:
    """Move ``record`` to ``new_status``, enforcing
    :data:`ALLOWED_TRANSITIONS` (PID §7).

    ``field_updates`` may set any other ``IntakeRecord`` field that is
    legitimately updated alongside a transition (e.g.
    ``detected_mime_type``, ``size_bytes``, ``content_hash``,
    ``evidence_id``, ``failure_code``, ``quarantine_reason``) — anyone
    not a real field raises :class:`core.errors.ValidationError`
    (never a raw ``TypeError``).

    ``completed_at`` is handled specially and must NOT be passed in
    ``field_updates``: it is stamped with :func:`core.timestamps.utc_now`
    automatically the moment ``new_status`` is one of
    :data:`TERMINAL_STATUSES`, and is otherwise left as ``record``'s
    existing value (which is always ``None`` before a terminal state).

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``record.status``.
        core.errors.ValidationError: if the resulting record fails
            contract validation, or ``field_updates`` names something
            that is not a real ``IntakeRecord`` field.
    """
    allowed = ALLOWED_TRANSITIONS.get(record.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"IntakeRecord '{record.intake_id}' cannot transition from "
            f"'{record.status}' to '{new_status}'; allowed transitions from "
            f"'{record.status}' are {sorted(allowed) or '(none — terminal state)'}"
        )

    if "completed_at" in field_updates:
        raise ValidationError(
            "completed_at is stamped automatically by transition() on reaching a "
            "terminal state and must not be supplied directly in field_updates"
        )

    completed_at = utc_now() if new_status in TERMINAL_STATUSES else record.completed_at

    try:
        updated = dataclasses.replace(
            record,
            status=new_status,
            completed_at=completed_at,
            **field_updates,
        )
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition IntakeRecord: {exc}") from exc

    return updated


class IntakeRepository(abc.ABC):
    """Repository abstraction for IntakeRecord (PID §8/§22)."""

    @abc.abstractmethod
    def create_intake_record(
        self,
        *,
        source_id: str,
        entity_hint: Optional[str] = None,
        original_filename: Optional[str] = None,
        reported_mime_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> IntakeRecord:
        """Create a new `IntakeRecord` in `RECEIVED` (PID §7).

        If `idempotency_key` is given and already maps to an existing
        record, this resolves per the module docstring's "Idempotency
        design decision": an identical identifying request tuple
        returns the EXISTING record (idempotent replay, no new record
        created); a different one raises
        `core.errors.IdempotencyConflictError`.

        `correlation_id` is generated fresh if not supplied — an
        intake creation is normally the first step of a new workflow
        correlation (PID §31), though a caller that already has one
        (e.g. a governed API layer that started the correlation
        earlier) may supply it explicitly.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_intake_record(self, intake_id: str) -> IntakeRecord:
        raise NotImplementedError

    @abc.abstractmethod
    def transition_status(self, intake_id: str, new_status: str, **field_updates: Any) -> IntakeRecord:
        """Look up `intake_id`, apply :func:`transition`, persist and
        return the resulting `IntakeRecord`."""
        raise NotImplementedError

    @abc.abstractmethod
    def find_by_idempotency_key(self, idempotency_key: str) -> Optional[IntakeRecord]:
        """Read-only lookup by `idempotency_key`. Returns `None` if no
        record exists for that key — a query, not a fetch-by-ID, so it
        never raises `NotFoundError`."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_intake_records(self) -> list[IntakeRecord]:
        raise NotImplementedError


class InMemoryIntakeRepository(IntakeRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, IntakeRecord] = {}
        self._by_idempotency_key: dict[str, str] = {}

    def create_intake_record(
        self,
        *,
        source_id: str,
        entity_hint: Optional[str] = None,
        original_filename: Optional[str] = None,
        reported_mime_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> IntakeRecord:
        if idempotency_key is not None:
            existing_id = self._by_idempotency_key.get(idempotency_key)
            if existing_id is not None:
                existing = self._by_id[existing_id]
                requested = _identifying_fields(
                    source_id=source_id,
                    entity_hint=entity_hint,
                    original_filename=original_filename,
                    reported_mime_type=reported_mime_type,
                )
                existing_tuple = _identifying_fields(
                    source_id=existing.source_id,
                    entity_hint=existing.entity_hint,
                    original_filename=existing.original_filename,
                    reported_mime_type=existing.reported_mime_type,
                )
                if requested == existing_tuple:
                    # Idempotent replay of the same request.
                    return existing
                raise IdempotencyConflictError(
                    f"idempotency_key '{idempotency_key}' already maps to IntakeRecord "
                    f"'{existing.intake_id}' with different identifying request content "
                    "(source_id/entity_hint/original_filename/reported_mime_type); "
                    "refusing to silently reuse the key for different content (PID §25/§53)"
                )

        try:
            candidate = IntakeRecord(
                intake_id=identity.generate_id(),
                source_id=source_id,
                status="RECEIVED",
                received_at=utc_now(),
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                entity_hint=entity_hint,
                original_filename=original_filename,
                reported_mime_type=reported_mime_type,
                idempotency_key=idempotency_key,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create IntakeRecord: {exc}") from exc

        self._by_id[candidate.intake_id] = candidate
        if idempotency_key is not None:
            self._by_idempotency_key[idempotency_key] = candidate.intake_id
        return candidate

    def get_intake_record(self, intake_id: str) -> IntakeRecord:
        try:
            return self._by_id[intake_id]
        except KeyError:
            raise NotFoundError(f"no IntakeRecord with intake_id '{intake_id}'") from None

    def transition_status(self, intake_id: str, new_status: str, **field_updates: Any) -> IntakeRecord:
        current = self.get_intake_record(intake_id)
        updated = transition(current, new_status, **field_updates)
        self._by_id[intake_id] = updated
        return updated

    def find_by_idempotency_key(self, idempotency_key: str) -> Optional[IntakeRecord]:
        existing_id = self._by_idempotency_key.get(idempotency_key)
        return self._by_id[existing_id] if existing_id is not None else None

    def list_intake_records(self) -> list[IntakeRecord]:
        return sorted(self._by_id.values(), key=lambda r: r.intake_id)
