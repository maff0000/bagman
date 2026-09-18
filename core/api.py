"""``BagmanCanonicalAPI`` — the minimal internal facade for CD-2's
canonical operations (PID §33).

Implements exactly the PID §33 operation names: ``register_entity``,
``register_source``, ``register_evidence``, ``link_external_reference``,
``record_provenance``, ``record_audit_event``, ``get_evidence``,
``trace_provenance``. These are internal canonical operations only —
no external-provider endpoints are introduced here.

Every canonical *write* operation (``register_entity``,
``register_source``, ``register_evidence``, ``link_external_reference``,
``record_provenance``) also appends a corresponding ``AuditEvent`` via
``record_audit_event()``, using the event_type names WI-1 documented:
``ENTITY_REGISTERED``, ``SOURCE_REGISTERED``, ``EVIDENCE_OBSERVED``,
``EXTERNAL_REFERENCE_LINKED``, ``PROVENANCE_RECORDED``. Each accepts an
optional ``correlation_id``/``causation_id`` so a caller can thread a
single workflow story through multiple calls (PID §15); a fresh
``correlation_id`` is generated via ``core.identity`` when the caller
doesn't supply one. ``actor_type``/``actor_id`` are explicit, required
parameters on every write method — there is no silent "system" default
(PID §14).

``get_evidence`` and ``trace_provenance`` are pure reads and do not
themselves append audit events (only the five write operations above
do, per the literal PID §33/WI-2 instruction).

Idempotent replay handling
--------------------------
``register_evidence`` (when given an ``external_reference`` hint) and
``link_external_reference`` can both resolve to an *existing* record
rather than creating a new one (PID §34). In both cases this facade
detects the replay *before* delegating to the repository (via a
read-only ``ExternalReferenceRepository.find_by_tuple`` lookup) so that
a replayed call never appends a duplicate audit event — only a
genuine new creation is audited.

Import-boundary note (flagged for the PL / WI-4)
-------------------------------------------------
The WI-2 instruction states "core/ must not import from
services/evidence/". This module is the one deliberate, necessary
exception: PID §33 requires a single facade exposing both
``register_evidence``/``get_evidence`` (which compose
``services.evidence.evidence.EvidenceRepository``) alongside the pure
``core/`` operations, and that facade is specified to live at
``core/api.py``. Every *other* file in ``core/`` imports only
``core/`` primitives and the standard library/``jsonschema``/
``rfc3339-validator`` — this composition-root facade is the sole
exception, by necessity, not by drift. If WI-4's import-boundary test
checks "nothing under core/ imports services/", it should exclude this
one file (or the rule should be re-scoped to "core/*.py domain models",
excluding this composition root) — flagging this for the PL/Auditor
rather than silently deciding it alone.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Union

from core import identity
from core.audit import AuditEvent, AuditRepository, InMemoryAuditRepository
from core.entity import EntityRepository, GovernedEntity, InMemoryEntityRepository
from core.errors import ConflictError
from core.external_reference import (
    ExternalReference,
    ExternalReferenceRepository,
    InMemoryExternalReferenceRepository,
)
from core.provenance import InMemoryProvenanceRepository, Provenance, ProvenanceRepository
from core.source import InMemorySourceRepository, Source, SourceRepository
from services.evidence.evidence import (
    EvidenceItem,
    EvidenceRepository,
    ExternalReferenceHint,
    InMemoryEvidenceRepository,
)


class BagmanCanonicalAPI:
    """Composition-root facade over the CD-2 canonical repositories.

    Constructs its own ``InMemory*`` repositories by default (PID §21
    Option A); a caller may instead supply any/all of them explicitly
    (e.g. to share repositories across multiple facade instances, or
    to substitute a future non-in-memory implementation without
    changing this class).
    """

    def __init__(
        self,
        *,
        entity_repository: Optional[EntityRepository] = None,
        source_repository: Optional[SourceRepository] = None,
        external_reference_repository: Optional[ExternalReferenceRepository] = None,
        evidence_repository: Optional[EvidenceRepository] = None,
        provenance_repository: Optional[ProvenanceRepository] = None,
        audit_repository: Optional[AuditRepository] = None,
    ) -> None:
        self.entity_repository: EntityRepository = entity_repository or InMemoryEntityRepository()
        self.source_repository: SourceRepository = source_repository or InMemorySourceRepository()
        self.external_reference_repository: ExternalReferenceRepository = (
            external_reference_repository or InMemoryExternalReferenceRepository()
        )
        self.evidence_repository: EvidenceRepository = (
            evidence_repository or InMemoryEvidenceRepository(self.external_reference_repository)
        )
        self.provenance_repository: ProvenanceRepository = (
            provenance_repository or InMemoryProvenanceRepository(self.evidence_repository)
        )
        self.audit_repository: AuditRepository = audit_repository or InMemoryAuditRepository()

    # -- writes -----------------------------------------------------

    def register_entity(
        self,
        *,
        entity_type: str,
        canonical_name: str,
        display_name: str,
        status: str,
        actor_type: str,
        actor_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        fiscal_year_start_month_day: Optional[str] = None,
        historical_floor_override_at: Optional[datetime] = None,
    ) -> GovernedEntity:
        resolved_correlation_id = correlation_id or identity.generate_id()

        entity = self.entity_repository.register_entity(
            entity_type=entity_type,
            canonical_name=canonical_name,
            display_name=display_name,
            status=status,
            metadata=metadata,
            fiscal_year_start_month_day=fiscal_year_start_month_day,
            historical_floor_override_at=historical_floor_override_at,
        )

        self.record_audit_event(
            event_type="ENTITY_REGISTERED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="GovernedEntity",
            subject_id=entity.entity_id,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            payload={"entity_type": entity_type, "canonical_name": canonical_name},
        )
        return entity

    def register_source(
        self,
        *,
        source_type: str,
        provider: str,
        status: str,
        actor_type: str,
        actor_id: str,
        external_source_ref: Optional[str] = None,
        governed_entity_hint: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> Source:
        resolved_correlation_id = correlation_id or identity.generate_id()

        source = self.source_repository.register_source(
            source_type=source_type,
            provider=provider,
            status=status,
            external_source_ref=external_source_ref,
            governed_entity_hint=governed_entity_hint,
            metadata=metadata,
        )

        self.record_audit_event(
            event_type="SOURCE_REGISTERED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="Source",
            subject_id=source.source_id,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            payload={"source_type": source_type, "provider": provider},
        )
        return source

    def register_evidence(
        self,
        *,
        entity_id: Optional[str],
        evidence_type: str,
        source_id: str,
        observed_at: datetime,
        received_at: datetime,
        content_hash: Union[str, Mapping[str, str]],
        mime_type: str,
        size_bytes: int,
        actor_type: str,
        actor_id: str,
        original_name: Optional[str] = None,
        storage_reference: Optional[str] = None,
        status: str = "OBSERVED",
        metadata: Optional[Mapping[str, Any]] = None,
        external_reference: Optional[ExternalReferenceHint] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> EvidenceItem:
        # Determine ahead of time (read-only) whether this call will
        # resolve to an idempotent replay, so we never double-log an
        # audit event for the same observation.
        is_replay = False
        if external_reference is not None:
            provider, resource_type, external_id = external_reference
            existing_ref = self.external_reference_repository.find_by_tuple(
                provider=provider,
                source_id=source_id,
                resource_type=resource_type,
                external_id=external_id,
            )
            is_replay = existing_ref is not None and existing_ref.canonical_object_type == "EvidenceItem"

        evidence = self.evidence_repository.register_evidence(
            entity_id=entity_id,
            evidence_type=evidence_type,
            source_id=source_id,
            observed_at=observed_at,
            received_at=received_at,
            content_hash=content_hash,
            mime_type=mime_type,
            size_bytes=size_bytes,
            original_name=original_name,
            storage_reference=storage_reference,
            status=status,
            metadata=metadata,
            external_reference=external_reference,
        )

        if not is_replay:
            resolved_correlation_id = correlation_id or identity.generate_id()
            self.record_audit_event(
                event_type="EVIDENCE_OBSERVED",
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type="EvidenceItem",
                subject_id=evidence.evidence_id,
                correlation_id=resolved_correlation_id,
                causation_id=causation_id,
                payload={"evidence_type": evidence_type, "source_id": source_id},
            )
        return evidence

    def link_external_reference(
        self,
        *,
        provider: str,
        source_id: str,
        resource_type: str,
        external_id: str,
        canonical_object_type: str,
        canonical_object_id: str,
        actor_type: str,
        actor_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> ExternalReference:
        existing = self.external_reference_repository.find_by_tuple(
            provider=provider,
            source_id=source_id,
            resource_type=resource_type,
            external_id=external_id,
        )
        is_replay = (
            existing is not None
            and existing.canonical_object_type == canonical_object_type
            and existing.canonical_object_id == canonical_object_id
        )

        reference = self.external_reference_repository.link_external_reference(
            provider=provider,
            source_id=source_id,
            resource_type=resource_type,
            external_id=external_id,
            canonical_object_type=canonical_object_type,
            canonical_object_id=canonical_object_id,
            metadata=metadata,
        )

        if not is_replay:
            resolved_correlation_id = correlation_id or identity.generate_id()
            self.record_audit_event(
                event_type="EXTERNAL_REFERENCE_LINKED",
                actor_type=actor_type,
                actor_id=actor_id,
                subject_type="ExternalReference",
                subject_id=reference.external_reference_id,
                correlation_id=resolved_correlation_id,
                causation_id=causation_id,
                payload={
                    "provider": provider,
                    "resource_type": resource_type,
                    "canonical_object_type": canonical_object_type,
                    "canonical_object_id": canonical_object_id,
                },
            )
        return reference

    def assign_evidence_entity(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> EvidenceItem:
        """CD-6 GUI-operations-foundation follow-on WO (item E) — the
        narrow, audited canonical operation that actually wires
        ``EvidenceRepository.assign_entity`` (already implemented,
        previously unwired by any real caller anywhere) into use.

        Validates ``entity_id`` is a REAL, existing ``GovernedEntity``
        first (mirrors every other "never trust a caller-supplied
        entity_id without proving it real" check already established at
        this same call boundary elsewhere in this codebase, e.g.
        ``app/api/routers/xero.py::_require_entity``), then loads the
        target ``EvidenceItem`` (``core.errors.NotFoundError`` if it does
        not exist).

        * ``entity_id`` currently ``None`` on the evidence item -> assign
          it (via the existing ``EvidenceRepository.assign_entity``) and
          record a real ``EVIDENCE_ENTITY_ASSIGNED`` audit event
          (``subject_type="EvidenceItem"``, ``subject_id=evidence_id``,
          payload carrying ``entity_id``).
        * the SAME ``entity_id`` already assigned -> idempotent no-op;
          returns the current, unchanged ``EvidenceItem`` (a genuine
          double-submit/retry of the same assignment must complete
          cleanly, and no audit event is re-emitted for a no-op).
        * a DIFFERENT ``entity_id`` already assigned -> raises
          ``core.errors.ConflictError`` loudly — reassignment/correction
          is an explicit, NOT-built-here governed workflow (architect's
          own instruction), never silently overwritten here.
        """
        # Real existence check, before anything else — never trust a
        # caller-supplied entity_id without proving it real first.
        self.entity_repository.get_entity(entity_id)

        current = self.evidence_repository.get_evidence(evidence_id)

        if current.entity_id == entity_id:
            # Idempotent no-op — a genuine retry/double-submit of the
            # exact same assignment must complete cleanly.
            return current

        if current.entity_id is not None:
            raise ConflictError(
                f"EvidenceItem '{evidence_id}' already has a DIFFERENT entity_id "
                f"('{current.entity_id}') assigned; reassignment to '{entity_id}' is an explicit, "
                "separate, NOT-built-here governed correction workflow — refusing to silently "
                "overwrite an already-resolved entity assignment"
            )

        updated = self.evidence_repository.assign_entity(evidence_id, entity_id)

        resolved_correlation_id = correlation_id or identity.generate_id()
        self.record_audit_event(
            event_type="EVIDENCE_ENTITY_ASSIGNED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="EvidenceItem",
            subject_id=evidence_id,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            payload={"entity_id": entity_id},
        )
        return updated

    def record_provenance(
        self,
        *,
        subject_type: str,
        subject_id: str,
        evidence_id: str,
        relationship: str,
        actor_type: str,
        actor_id: str,
        transform_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> Provenance:
        resolved_correlation_id = correlation_id or identity.generate_id()

        provenance = self.provenance_repository.record_provenance(
            subject_type=subject_type,
            subject_id=subject_id,
            evidence_id=evidence_id,
            relationship=relationship,
            transform_id=transform_id,
            metadata=metadata,
        )

        self.record_audit_event(
            event_type="PROVENANCE_RECORDED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            payload={"evidence_id": evidence_id, "relationship": relationship},
        )
        return provenance

    def record_audit_event(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        subject_type: str,
        subject_id: str,
        payload: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> AuditEvent:
        resolved_correlation_id = correlation_id or identity.generate_id()
        return self.audit_repository.record_audit_event(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            payload=payload,
        )

    # -- reads --------------------------------------------------------

    def get_evidence(self, evidence_id: str) -> EvidenceItem:
        return self.evidence_repository.get_evidence(evidence_id)

    def trace_provenance(self, *, subject_type: str, subject_id: str) -> list[dict]:
        """The full "evidence lineage query" (PID §11/§42): every
        Provenance edge recorded for this subject, each resolved all
        the way back to its EvidenceItem and that evidence's Source.

        Returns a list of ``{"provenance": Provenance, "evidence":
        EvidenceItem, "source": Source}`` dicts, one per edge, in the
        order the edges were recorded.
        """
        edges = self.provenance_repository.trace_provenance(
            subject_type=subject_type, subject_id=subject_id
        )
        lineage: list[dict] = []
        for edge in edges:
            evidence = self.evidence_repository.get_evidence(edge.evidence_id)
            source = self.source_repository.get_source(evidence.source_id)
            lineage.append({"provenance": edge, "evidence": evidence, "source": source})
        return lineage
