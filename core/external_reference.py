"""Canonical ExternalReference domain model (PID §10) and repository.

Maps a raw provider-native identifier to a BAGMAN canonical object
without contaminating BAGMAN's canonical identity model. Uniqueness
and idempotency are scoped to the composite tuple
``(provider, source_id, resource_type, external_id)`` (PID §10, §34):

* unseen tuple                                -> create a new record.
* seen tuple, same canonical target            -> idempotent replay:
  return the existing record unchanged, no duplicate created.
* seen tuple, a *different* canonical target   -> genuine conflict:
  raise ``DuplicateExternalReferenceError``.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import DuplicateExternalReferenceError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "source/bagman.external_reference.v1.schema.json"

# (provider, source_id, resource_type, external_id)
_TupleKey = Tuple[str, str, str, str]


@dataclass(frozen=True)
class ExternalReference:
    """Maps an external provider identifier to a BAGMAN canonical object (PID §10)."""

    external_reference_id: str
    provider: str
    resource_type: str
    external_id: str
    canonical_object_type: str
    canonical_object_id: str
    source_id: str
    first_observed_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/source/bagman.external_reference.v1.schema.json``."""
        return {
            "external_reference_id": self.external_reference_id,
            "provider": self.provider,
            "resource_type": self.resource_type,
            "external_id": self.external_id,
            "canonical_object_type": self.canonical_object_type,
            "canonical_object_id": self.canonical_object_id,
            "source_id": self.source_id,
            "first_observed_at": to_contract_string(self.first_observed_at),
            "metadata": dict(self.metadata),
        }


class ExternalReferenceRepository(abc.ABC):
    """Repository abstraction for ExternalReference (PID §22)."""

    @abc.abstractmethod
    def link_external_reference(
        self,
        *,
        provider: str,
        source_id: str,
        resource_type: str,
        external_id: str,
        canonical_object_type: str,
        canonical_object_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ExternalReference:
        raise NotImplementedError

    @abc.abstractmethod
    def get_external_reference(self, external_reference_id: str) -> ExternalReference:
        raise NotImplementedError

    @abc.abstractmethod
    def find_by_tuple(
        self, *, provider: str, source_id: str, resource_type: str, external_id: str
    ) -> Optional[ExternalReference]:
        """Read-only lookup by the composite uniqueness tuple.

        Returns ``None`` if no record exists for that tuple — this is
        a query, not a fetch-by-ID, so it never raises
        ``NotFoundError``. Used by ``services.evidence`` to implement
        idempotent evidence replay without itself writing a new
        ``ExternalReference`` row (only ``link_external_reference``
        writes one, per PID §33's operation naming).
        """
        raise NotImplementedError


class InMemoryExternalReferenceRepository(ExternalReferenceRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, ExternalReference] = {}
        self._by_tuple: dict[_TupleKey, str] = {}

    def link_external_reference(
        self,
        *,
        provider: str,
        source_id: str,
        resource_type: str,
        external_id: str,
        canonical_object_type: str,
        canonical_object_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ExternalReference:
        key: _TupleKey = (provider, source_id, resource_type, external_id)
        existing_id = self._by_tuple.get(key)

        if existing_id is not None:
            existing = self._by_id[existing_id]
            if (
                existing.canonical_object_type == canonical_object_type
                and existing.canonical_object_id == canonical_object_id
            ):
                # Idempotent replay of the same observation.
                return existing
            raise DuplicateExternalReferenceError(
                f"external reference tuple (provider={provider!r}, source_id={source_id!r}, "
                f"resource_type={resource_type!r}, external_id={external_id!r}) already maps to "
                f"{existing.canonical_object_type}:{existing.canonical_object_id}; cannot also "
                f"map it to {canonical_object_type}:{canonical_object_id}"
            )

        try:
            candidate = ExternalReference(
                external_reference_id=identity.generate_id(),
                provider=provider,
                resource_type=resource_type,
                external_id=external_id,
                canonical_object_type=canonical_object_type,
                canonical_object_id=canonical_object_id,
                source_id=source_id,
                first_observed_at=utc_now(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not link ExternalReference: {exc}") from exc

        self._by_id[candidate.external_reference_id] = candidate
        self._by_tuple[key] = candidate.external_reference_id
        return candidate

    def get_external_reference(self, external_reference_id: str) -> ExternalReference:
        try:
            return self._by_id[external_reference_id]
        except KeyError:
            raise NotFoundError(
                f"no ExternalReference with external_reference_id '{external_reference_id}'"
            ) from None

    def find_by_tuple(
        self, *, provider: str, source_id: str, resource_type: str, external_id: str
    ) -> Optional[ExternalReference]:
        key: _TupleKey = (provider, source_id, resource_type, external_id)
        existing_id = self._by_tuple.get(key)
        return self._by_id[existing_id] if existing_id is not None else None
