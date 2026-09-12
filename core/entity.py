"""Canonical GovernedEntity domain model (PID §4) and its repository."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ImmutabilityViolationError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "entity/bagman.entity.v1.schema.json"


@dataclass(frozen=True)
class GovernedEntity:
    """A stable BAGMAN identity (PID §4) — a company, person, or other
    governed party that canonical financial/evidential records are
    ultimately associated with, or explicitly left unresolved.

    Immutable once constructed: this is a frozen dataclass, and neither
    this type nor its repository ever mutates or replaces a field in
    place — ``entity_id`` in particular is never reassigned.
    """

    entity_id: str
    entity_type: str
    canonical_name: str
    display_name: str
    status: str
    created_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/entity/bagman.entity.v1.schema.json``."""
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "canonical_name": self.canonical_name,
            "display_name": self.display_name,
            "status": self.status,
            "created_at": to_contract_string(self.created_at),
            "metadata": dict(self.metadata),
        }


class EntityRepository(abc.ABC):
    """Repository abstraction for GovernedEntity (PID §22)."""

    @abc.abstractmethod
    def register_entity(
        self,
        *,
        entity_type: str,
        canonical_name: str,
        display_name: str,
        status: str,
        metadata: Optional[Mapping[str, Any]] = None,
        entity_id: Optional[str] = None,
    ) -> GovernedEntity:
        """Register a new GovernedEntity.

        ``entity_id`` is normally omitted — the repository generates a
        fresh UUIDv7 via ``core.identity``. Passing an explicit
        ``entity_id`` is supported only for deterministic registration
        of a pre-assigned identity (e.g. a future config-driven seeding
        of the PID §23 initial entity registry) and is rejected with
        ``ImmutabilityViolationError`` if that ``entity_id`` already
        exists — ``entity_id`` is never reassigned.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_entity(self, entity_id: str) -> GovernedEntity:
        raise NotImplementedError

    @abc.abstractmethod
    def list_entities(self) -> list[GovernedEntity]:
        raise NotImplementedError


class InMemoryEntityRepository(EntityRepository):
    """Narrow in-memory reference implementation of EntityRepository
    (PID §21 Option A — no database in CD-2)."""

    def __init__(self) -> None:
        self._entities: dict[str, GovernedEntity] = {}

    def register_entity(
        self,
        *,
        entity_type: str,
        canonical_name: str,
        display_name: str,
        status: str,
        metadata: Optional[Mapping[str, Any]] = None,
        entity_id: Optional[str] = None,
    ) -> GovernedEntity:
        resolved_id = entity_id if entity_id is not None else identity.generate_id()
        if resolved_id in self._entities:
            raise ImmutabilityViolationError(
                f"entity_id '{resolved_id}' already exists; entity_id is never reassigned"
            )

        try:
            candidate = GovernedEntity(
                entity_id=resolved_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
                display_name=display_name,
                status=status,
                created_at=utc_now(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register GovernedEntity: {exc}") from exc

        self._entities[resolved_id] = candidate
        return candidate

    def get_entity(self, entity_id: str) -> GovernedEntity:
        try:
            return self._entities[entity_id]
        except KeyError:
            raise NotFoundError(f"no GovernedEntity with entity_id '{entity_id}'") from None

    def list_entities(self) -> list[GovernedEntity]:
        return list(self._entities.values())
