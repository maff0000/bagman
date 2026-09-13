"""Canonical Source domain model (PID §9) and its repository."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, ValidationError

_SCHEMA = "source/bagman.source.v1.schema.json"


@dataclass(frozen=True)
class Source:
    """Describes where information originated (PID §9).

    ``governed_entity_hint`` is a HINT only, never asserted ownership:
    per PID §9, "a source may provide an entity hint without asserting
    canonical ownership" — e.g. a mailbox provider hint of
    ``matt@infosecurs.com`` does not mean all records observed from
    that mailbox automatically belong to Infosecurs. Nothing in this
    module (or elsewhere in ``core``/``services``) uses this hint to
    auto-populate any ``entity_id`` anywhere; entity resolution is
    always a separate, explicit act.
    """

    source_id: str
    source_type: str
    provider: str
    status: str
    external_source_ref: Optional[str] = None
    governed_entity_hint: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/source/bagman.source.v1.schema.json``.

        ``external_source_ref`` is omitted entirely when ``None``
        (the contract types it as a plain optional string, no ``null``
        branch); ``governed_entity_hint`` is always included, since the
        contract explicitly allows ``null`` there for "no hint".
        """
        result: dict = {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "provider": self.provider,
            "status": self.status,
            "governed_entity_hint": self.governed_entity_hint,
            "metadata": dict(self.metadata),
        }
        if self.external_source_ref is not None:
            result["external_source_ref"] = self.external_source_ref
        return result


class SourceRepository(abc.ABC):
    """Repository abstraction for Source (PID §22)."""

    @abc.abstractmethod
    def register_source(
        self,
        *,
        source_type: str,
        provider: str,
        status: str,
        external_source_ref: Optional[str] = None,
        governed_entity_hint: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Source:
        raise NotImplementedError

    @abc.abstractmethod
    def get_source(self, source_id: str) -> Source:
        raise NotImplementedError

    @abc.abstractmethod
    def list_sources(self) -> list[Source]:
        raise NotImplementedError

    @abc.abstractmethod
    def find_by_provider(self, *, source_type: str, provider: str) -> Optional[Source]:
        """Read-only lookup for the first ``Source`` matching the exact
        (``source_type``, ``provider``) pair, or ``None`` if none exists
        (CD-4 WI-3, PID §9). Added so a caller (e.g.
        ``app/api/composition.py``'s stable ``MANUAL_UPLOAD`` source
        resolve-or-create helper) can idempotently discover an
        already-registered well-known ``Source`` without creating a new
        one for every intake attempt — a query, not a fetch-by-ID, so it
        never raises ``NotFoundError``. Never raises for "not found";
        only genuine infrastructure failure propagates.
        """
        raise NotImplementedError


class InMemorySourceRepository(SourceRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}

    def register_source(
        self,
        *,
        source_type: str,
        provider: str,
        status: str,
        external_source_ref: Optional[str] = None,
        governed_entity_hint: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Source:
        try:
            candidate = Source(
                source_id=identity.generate_id(),
                source_type=source_type,
                provider=provider,
                status=status,
                external_source_ref=external_source_ref,
                governed_entity_hint=governed_entity_hint,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register Source: {exc}") from exc

        self._sources[candidate.source_id] = candidate
        return candidate

    def get_source(self, source_id: str) -> Source:
        try:
            return self._sources[source_id]
        except KeyError:
            raise NotFoundError(f"no Source with source_id '{source_id}'") from None

    def list_sources(self) -> list[Source]:
        return list(self._sources.values())

    def find_by_provider(self, *, source_type: str, provider: str) -> Optional[Source]:
        for source in self._sources.values():
            if source.source_type == source_type and source.provider == provider:
                return source
        return None
