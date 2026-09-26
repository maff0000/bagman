"""Canonical GovernedEntity domain model (PID §4) and its repository."""
from __future__ import annotations

import abc
import dataclasses
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
    #: CD-6 architect amendment (email historical-ingestion boundary).
    #: The RECURRING month-day (``MM-DD``) this entity's accounting/
    #: financial year starts on in every FUTURE year — see the
    #: contract's own field description for the full "why this is
    #: separate from ``historical_floor_override_at``" reasoning (the
    #: NoustAI incorporation-date example). ``None`` is a real, honest
    #: "not yet configured" state — never invented, never silently
    #: defaulted.
    fiscal_year_start_month_day: Optional[str] = None
    #: CD-6 architect amendment, RENAMED and RE-SCOPED following a
    #: second real conceptual-conflation bug the architect caught
    #: before any real historical sweep had run (only a superseded
    #: 128-message acceptance run under the OLD flat-7-day scheme had
    #: ever executed — no real data depended on the old value). This
    #: field was previously named ``email_bootstrap_floor_at`` and was
    #: WRONGLY treated as "the definitive literal historical-bootstrap
    #: date" — it was not; it is, and always should have been, an
    #: OPTIONAL CLAMP/OVERRIDE only.
    #:
    #: ``None`` (the correct, common case — true for Infosecurs and
    #: Matthew Scott Personal) means "no override needed; trust the
    #: period-derivation alone" — the real historical-bootstrap answer
    #: is DERIVED, never read directly off this field. When set, it is
    #: a real, independently-verified commencement/incorporation date
    #: (e.g. NoustAI Limited's 2025-12-05 incorporation) that the naive
    #: previous-completed-accounting-period derivation must never
    #: predate — see ``services.mailbox.bootstrap_policy
    #: .compute_entity_historical_bootstrap`` for the exact clamp
    #: (``max(override, naive_previous_period_start)``) and
    #: ``services.mailbox.sweep.compute_bootstrap_floor`` for how that
    #: per-entity value feeds a mailbox's own bootstrap floor (both of
    #: which refuse to run any historical sweep while any in-scope
    #: governed entity's ``fiscal_year_start_month_day`` — the field
    #: that is actually REQUIRED for derivation — is ``None``; this
    #: field being ``None`` is never itself an error).
    historical_floor_override_at: Optional[datetime] = None
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
            "fiscal_year_start_month_day": self.fiscal_year_start_month_day,
            "historical_floor_override_at": (
                to_contract_string(self.historical_floor_override_at)
                if self.historical_floor_override_at is not None
                else None
            ),
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
        fiscal_year_start_month_day: Optional[str] = None,
        historical_floor_override_at: Optional[datetime] = None,
    ) -> GovernedEntity:
        """Register a new GovernedEntity.

        ``entity_id`` is normally omitted — the repository generates a
        fresh UUIDv7 via ``core.identity``. Passing an explicit
        ``entity_id`` is supported only for deterministic registration
        of a pre-assigned identity (e.g. a future config-driven seeding
        of the PID §23 initial entity registry) and is rejected with
        ``ImmutabilityViolationError`` if that ``entity_id`` already
        exists — ``entity_id`` is never reassigned.

        ``fiscal_year_start_month_day``/``historical_floor_override_at``
        (CD-6 architect amendment, the latter renamed from
        ``email_bootstrap_floor_at`` after a real conceptual-conflation
        bug fix — see ``GovernedEntity.historical_floor_override_at``'s
        own field docstring) are both optional and default to ``None``.
        ``fiscal_year_start_month_day`` being ``None`` means an
        entity's email historical-ingestion boundary is not yet
        configured — a real, surfaced state (see
        ``services.mailbox.sweep.compute_bootstrap_floor``), never an
        error at registration time itself.
        ``historical_floor_override_at`` being ``None`` is separately,
        independently a normal, common, PERMANENT state (never itself
        "not yet configured") — see that field's own docstring.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def set_accounting_period_configuration(
        self,
        entity_id: str,
        *,
        fiscal_year_start_month_day: str,
        historical_floor_override_at: Optional[datetime],
    ) -> GovernedEntity:
        """PL-review finding, CD-6 architect amendment: an entity
        registered BEFORE ``fiscal_year_start_month_day``/
        ``historical_floor_override_at`` existed (every entity created
        during CD-6 Slice 1, including all three already live on the
        production appliance) has both fields permanently ``None`` —
        ``register_entity`` only ever sets them at creation time, and
        ``GovernedEntity`` is otherwise immutable, so without a real
        update path the architect's own verified accounting-period
        rule could never actually reach an entity that already
        exists, and ``services.mailbox.sweep.compute_bootstrap_floor``
        would refuse the real historical sweep forever (note:
        ``historical_floor_override_at`` itself is legitimately
        ``None`` for most entities — see that field's own docstring —
        so this method accepts ``None`` for it explicitly, unlike
        ``fiscal_year_start_month_day`` which is required here).

        This is a narrow, single-purpose update — it touches ONLY
        these two fields, never ``display_name``/``status``/anything
        else GovernedEntity carries, and it is a REPLACE, not a merge:
        the caller (``app/api/composition.py::ensure_seed_entities``)
        is responsible for only calling this when backfilling a
        currently-``None`` value with a real one — this method itself
        does not check "was it already set", since a governed config
        surface may legitimately need to update these values later too
        (e.g. a real operator correction), not only backfill a null.

        Raises:
            core.errors.NotFoundError: no such ``entity_id``.
            core.errors.ValidationError: the resulting entity fails
                contract validation (e.g. a malformed MM-DD string).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_entity(self, entity_id: str) -> GovernedEntity:
        raise NotImplementedError

    @abc.abstractmethod
    def list_entities(self) -> list[GovernedEntity]:
        raise NotImplementedError

    @abc.abstractmethod
    def find_by_canonical_name(self, canonical_name: str) -> Optional[GovernedEntity]:
        """Read-only lookup for the ``GovernedEntity`` with this exact
        ``canonical_name``, or ``None`` if none exists (CD-6 Slice 1,
        PID §98.3). Added so a caller (e.g.
        ``app/api/composition.py``'s idempotent canonical-entity seed —
        see that module's own "Stable canonical entity seed lifecycle"
        section) can resolve-or-create a well-known entity without
        creating a duplicate — the exact same role
        ``SourceRepository.find_by_provider`` already plays for the
        stable ``MANUAL_UPLOAD`` ``Source``. A query, not a fetch-by-ID,
        so it never raises ``NotFoundError``.
        """
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
        fiscal_year_start_month_day: Optional[str] = None,
        historical_floor_override_at: Optional[datetime] = None,
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
                fiscal_year_start_month_day=fiscal_year_start_month_day,
                historical_floor_override_at=historical_floor_override_at,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register GovernedEntity: {exc}") from exc

        self._entities[resolved_id] = candidate
        return candidate

    def set_accounting_period_configuration(
        self,
        entity_id: str,
        *,
        fiscal_year_start_month_day: str,
        historical_floor_override_at: Optional[datetime],
    ) -> GovernedEntity:
        current = self.get_entity(entity_id)
        try:
            updated = dataclasses.replace(
                current,
                fiscal_year_start_month_day=fiscal_year_start_month_day,
                historical_floor_override_at=historical_floor_override_at,
            )
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not update GovernedEntity accounting-period configuration: {exc}") from exc
        self._entities[entity_id] = updated
        return updated

    def get_entity(self, entity_id: str) -> GovernedEntity:
        try:
            return self._entities[entity_id]
        except KeyError:
            raise NotFoundError(f"no GovernedEntity with entity_id '{entity_id}'") from None

    def list_entities(self) -> list[GovernedEntity]:
        return list(self._entities.values())

    def find_by_canonical_name(self, canonical_name: str) -> Optional[GovernedEntity]:
        for entity in self._entities.values():
            if entity.canonical_name == canonical_name:
                return entity
        return None
