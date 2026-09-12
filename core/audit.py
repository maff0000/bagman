"""Canonical AuditEvent domain model (PID §13-15) and its repository.

Append-only: only ``record_audit_event()`` and read/query methods
exist — there is no update or delete method at all, matching PID
§13's "immutable after creation" requirement.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import actor, identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "audit/bagman.audit_event.v1.schema.json"

#: This is CD-2's only supported contract version for AuditEvent; the
#: contract itself pins ``schema_version`` to this exact value via a
#: JSON Schema ``const``, so it is never a caller-supplied parameter —
#: the repository always stamps it.
SCHEMA_VERSION = "bagman.audit_event.v1"


@dataclass(frozen=True)
class AuditEvent:
    """An immutable, actor-attributed, correlation/causation-aware
    record of something that happened inside BAGMAN (PID §13-15)."""

    audit_event_id: str
    event_type: str
    occurred_at: datetime
    actor_type: str
    actor_id: str
    subject_type: str
    subject_id: str
    correlation_id: str
    causation_id: Optional[str]
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/audit/bagman.audit_event.v1.schema.json``."""
        return {
            "audit_event_id": self.audit_event_id,
            "event_type": self.event_type,
            "occurred_at": to_contract_string(self.occurred_at),
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": dict(self.payload),
            "schema_version": self.schema_version,
        }


class AuditRepository(abc.ABC):
    """Append-only repository abstraction for AuditEvent (PID §22)."""

    @abc.abstractmethod
    def record_audit_event(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        subject_type: str,
        subject_id: str,
        correlation_id: str,
        causation_id: Optional[str],
        occurred_at: Optional[datetime] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> AuditEvent:
        raise NotImplementedError

    @abc.abstractmethod
    def get_audit_event(self, audit_event_id: str) -> AuditEvent:
        raise NotImplementedError

    @abc.abstractmethod
    def list_by_correlation(self, correlation_id: str) -> list[AuditEvent]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_by_subject(self, subject_type: str, subject_id: str) -> list[AuditEvent]:
        raise NotImplementedError


class InMemoryAuditRepository(AuditRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, AuditEvent] = {}
        self._by_correlation: dict[str, list[str]] = {}
        self._by_subject: dict[tuple[str, str], list[str]] = {}

    def record_audit_event(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        subject_type: str,
        subject_id: str,
        correlation_id: str,
        causation_id: Optional[str],
        occurred_at: Optional[datetime] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> AuditEvent:
        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set "
                f"{sorted(actor.ALL)} (PID §14)"
            )

        try:
            candidate = AuditEvent(
                audit_event_id=identity.generate_id(),
                event_type=event_type,
                occurred_at=occurred_at if occurred_at is not None else utc_now(),
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=dict(payload) if payload is not None else {},
                schema_version=SCHEMA_VERSION,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not record AuditEvent: {exc}") from exc

        self._by_id[candidate.audit_event_id] = candidate
        self._by_correlation.setdefault(correlation_id, []).append(candidate.audit_event_id)
        self._by_subject.setdefault((subject_type, subject_id), []).append(
            candidate.audit_event_id
        )
        return candidate

    def get_audit_event(self, audit_event_id: str) -> AuditEvent:
        try:
            return self._by_id[audit_event_id]
        except KeyError:
            raise NotFoundError(f"no AuditEvent with audit_event_id '{audit_event_id}'") from None

    def list_by_correlation(self, correlation_id: str) -> list[AuditEvent]:
        return [self._by_id[i] for i in self._by_correlation.get(correlation_id, [])]

    def list_by_subject(self, subject_type: str, subject_id: str) -> list[AuditEvent]:
        return [self._by_id[i] for i in self._by_subject.get((subject_type, subject_id), [])]
