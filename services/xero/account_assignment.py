"""``XeroAccountAssignment`` — the authoritative, write-once record of
which Xero chart-of-accounts entry a piece of evidence was actually
coded to (BAGMAN accounting platform, `xero/account-suggestion-producer`
WO).

Deliberately a SEPARATE table/domain object from
``services.xero.account_suggestion.XeroAccountSuggestion`` (architect
instruction: "do NOT use the suggestion row itself as both proposal and
authoritative resolution") — the suggestion is an append-only, never-
authoritative AI ledger entry; this is the real, write-once human
decision, created ONLY by
``services.xero.account_suggestion_resolution.resolve_account_suggestion``
(the ``XERO_ACCOUNT_REQUIRED`` Needs You resolution handler), never by
the suggestion producer itself.

Write-once discipline — mirrors ``EvidenceItem.assign_entity`` exactly
------------------------------------------------------------------------
Exactly one row may ever exist per ``evidence_id`` (a real database-level
unique constraint on Postgres, mirrored in application logic here for
the in-memory reference implementation — same "application check first,
database constraint is the real proof under a race" discipline every
other repository in this codebase follows). A second
:meth:`XeroAccountAssignmentRepository.create_assignment` call for the
same ``evidence_id``:

* with the SAME ``account_id`` — an idempotent no-op, returns the
  existing row unchanged (a genuine retry/double-submit of the exact
  same decision must complete cleanly).
* with a DIFFERENT ``account_id`` — raises ``core.errors.ConflictError``
  loudly. Reassignment/correction is an explicit, NOT-built-here
  governed workflow (same judgment call ``core.api.BagmanCanonicalAPI
  .assign_evidence_entity`` already makes for ``EvidenceItem.entity_id``
  — this module mirrors that precedent deliberately, not by accident).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "xero/bagman.xero_account_assignment.v1.schema.json"
SCHEMA_VERSION = "bagman.xero_account_assignment.v1"

SOURCE_AI_ACCEPTED = "AI_ACCEPTED"
SOURCE_OPERATOR_SELECTED = "OPERATOR_SELECTED"
ASSIGNMENT_SOURCES = frozenset({SOURCE_AI_ACCEPTED, SOURCE_OPERATOR_SELECTED})


@dataclass(frozen=True)
class XeroAccountAssignment:
    """One immutable, write-once authoritative coding decision. Never
    mutated in place; see module docstring for the write-once/conflict
    discipline every repository implementation must honour identically."""

    assignment_id: str
    evidence_id: str
    entity_id: str
    tenant_id: str
    account_id: str
    source: str
    suggestion_id: Optional[str]
    assigned_by_actor_type: str
    assigned_by_actor_id: str
    assigned_at: datetime
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/xero/bagman.xero_account_assignment.v1.schema.json``."""
        return {
            "assignment_id": self.assignment_id,
            "evidence_id": self.evidence_id,
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "account_id": self.account_id,
            "source": self.source,
            "suggestion_id": self.suggestion_id,
            "assigned_by_actor_type": self.assigned_by_actor_type,
            "assigned_by_actor_id": self.assigned_by_actor_id,
            "assigned_at": to_contract_string(self.assigned_at),
            "schema_version": self.schema_version,
        }


def validate_assignment_fields_or_raise(*, source: str, account_id: str, evidence_id: str, entity_id: str, tenant_id: str) -> None:
    if source not in ASSIGNMENT_SOURCES:
        raise ValidationError(f"'{source}' is not one of {sorted(ASSIGNMENT_SOURCES)}")
    if not account_id:
        raise ValidationError("account_id must be a real, non-empty Xero AccountID")
    if not evidence_id or not entity_id or not tenant_id:
        raise ValidationError("evidence_id, entity_id, and tenant_id are all required")


class XeroAccountAssignmentRepository(abc.ABC):
    """Repository abstraction for XeroAccountAssignment."""

    @abc.abstractmethod
    def create_assignment(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        tenant_id: str,
        account_id: str,
        source: str,
        suggestion_id: Optional[str],
        assigned_by_actor_type: str,
        assigned_by_actor_id: str,
    ) -> XeroAccountAssignment:
        """Create the one, write-once assignment row for ``evidence_id``
        — or return the EXISTING row unchanged if ``account_id`` matches
        it exactly (idempotent replay). See module docstring.

        Raises:
            core.errors.ValidationError: field-shape violations.
            core.errors.ConflictError: an assignment already exists for
                ``evidence_id`` with a DIFFERENT ``account_id``.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_assignment(self, assignment_id: str) -> XeroAccountAssignment:
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_evidence(self, evidence_id: str) -> Optional[XeroAccountAssignment]:
        """Read-only lookup, never ``NotFoundError`` — an evidence item
        with no assignment yet is an ordinary, expected state."""
        raise NotImplementedError


class InMemoryXeroAccountAssignmentRepository(XeroAccountAssignmentRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, XeroAccountAssignment] = {}
        self._id_by_evidence: dict[str, str] = {}

    def create_assignment(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        tenant_id: str,
        account_id: str,
        source: str,
        suggestion_id: Optional[str],
        assigned_by_actor_type: str,
        assigned_by_actor_id: str,
    ) -> XeroAccountAssignment:
        validate_assignment_fields_or_raise(
            source=source, account_id=account_id, evidence_id=evidence_id, entity_id=entity_id, tenant_id=tenant_id,
        )

        existing_id = self._id_by_evidence.get(evidence_id)
        if existing_id is not None:
            existing = self._by_id[existing_id]
            if existing.account_id == account_id:
                # Idempotent no-op — a genuine retry/double-submit of the
                # exact same decision must complete cleanly.
                return existing
            raise ConflictError(
                f"evidence '{evidence_id}' already has a DIFFERENT account_id "
                f"('{existing.account_id}') assigned via XeroAccountAssignment "
                f"'{existing.assignment_id}'; reassignment to '{account_id}' is an explicit, separate, "
                "NOT-built-here governed correction workflow — refusing to silently overwrite an "
                "already-resolved coding decision"
            )

        try:
            candidate = XeroAccountAssignment(
                assignment_id=identity.generate_id(),
                evidence_id=evidence_id,
                entity_id=entity_id,
                tenant_id=tenant_id,
                account_id=account_id,
                source=source,
                suggestion_id=suggestion_id,
                assigned_by_actor_type=assigned_by_actor_type,
                assigned_by_actor_id=assigned_by_actor_id,
                assigned_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroAccountAssignment: {exc}") from exc

        self._by_id[candidate.assignment_id] = candidate
        self._id_by_evidence[evidence_id] = candidate.assignment_id
        return candidate

    def get_assignment(self, assignment_id: str) -> XeroAccountAssignment:
        try:
            return self._by_id[assignment_id]
        except KeyError:
            raise NotFoundError(f"no XeroAccountAssignment with assignment_id '{assignment_id}'") from None

    def get_by_evidence(self, evidence_id: str) -> Optional[XeroAccountAssignment]:
        existing_id = self._id_by_evidence.get(evidence_id)
        return self._by_id[existing_id] if existing_id is not None else None
