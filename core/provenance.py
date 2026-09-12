"""Canonical Provenance domain model (PID §11) and its repository.

Append-only: this repository exposes only ``record_provenance()`` and
read methods (``get_provenance``, ``trace_provenance``) — there is no
update or delete method at all.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import InvalidProvenanceError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "provenance/bagman.provenance.v1.schema.json"

#: The closed, platform-level relationship vocabulary (PID §11). The
#: contract itself enforces this as a JSON Schema ``enum``; this
#: constant is documentation/reuse for callers and tests, not an extra
#: enforcement layer.
RELATIONSHIPS = frozenset(
    {
        "OBSERVED_FROM",
        "EXTRACTED_FROM",
        "DERIVED_FROM",
        "GENERATED_FROM",
        "SUPERSEDES",
        "SUPPORTS",
        "CONTRADICTS",
    }
)


@dataclass(frozen=True)
class Provenance:
    """A single-evidence lineage edge (PID §11): traces one canonical
    ``subject`` back to the ONE ``EvidenceItem`` it was observed,
    extracted, derived from, or otherwise relates to. Where a subject
    has multiple evidence sources, that is represented as multiple
    ``Provenance`` records, never as an array on one record.
    """

    provenance_id: str
    subject_type: str
    subject_id: str
    evidence_id: str
    relationship: str
    created_at: datetime
    transform_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/provenance/bagman.provenance.v1.schema.json``."""
        return {
            "provenance_id": self.provenance_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "evidence_id": self.evidence_id,
            "relationship": self.relationship,
            "transform_id": self.transform_id,
            "created_at": to_contract_string(self.created_at),
            "metadata": dict(self.metadata),
        }


class ProvenanceRepository(abc.ABC):
    """Append-only repository abstraction for Provenance (PID §22)."""

    @abc.abstractmethod
    def record_provenance(
        self,
        *,
        subject_type: str,
        subject_id: str,
        evidence_id: str,
        relationship: str,
        transform_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Provenance:
        raise NotImplementedError

    @abc.abstractmethod
    def get_provenance(self, provenance_id: str) -> Provenance:
        raise NotImplementedError

    @abc.abstractmethod
    def trace_provenance(self, *, subject_type: str, subject_id: str) -> list[Provenance]:
        """Return every Provenance edge recorded for this subject, in
        the order they were recorded.

        Each edge's ``evidence_id`` can be resolved to its
        ``EvidenceItem`` (and that evidence's ``Source``) via the
        ``EvidenceRepository``/``SourceRepository`` this repository
        was constructed with. This method itself returns the raw edges
        only — see ``core.api.BagmanCanonicalAPI.trace_provenance`` for
        the fully composed subject -> evidence -> source lineage query
        (PID §11/§42's "evidence lineage query" requirement).
        """
        raise NotImplementedError


class InMemoryProvenanceRepository(ProvenanceRepository):
    """Narrow in-memory reference implementation (PID §21 Option A).

    Constructed with an ``EvidenceRepository`` so that
    ``record_provenance`` can verify a referenced ``evidence_id``
    actually exists before accepting the edge — invalid/orphan
    provenance is rejected (PID §31) with ``InvalidProvenanceError``.
    """

    def __init__(self, evidence_repository) -> None:
        self._evidence_repository = evidence_repository
        self._by_id: dict[str, Provenance] = {}
        self._by_subject: dict[tuple[str, str], list[str]] = {}

    def record_provenance(
        self,
        *,
        subject_type: str,
        subject_id: str,
        evidence_id: str,
        relationship: str,
        transform_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Provenance:
        try:
            self._evidence_repository.get_evidence(evidence_id)
        except NotFoundError:
            raise InvalidProvenanceError(
                f"cannot record provenance: evidence_id '{evidence_id}' does not exist "
                "(invalid/orphan provenance rejected per PID §31)"
            ) from None

        try:
            candidate = Provenance(
                provenance_id=identity.generate_id(),
                subject_type=subject_type,
                subject_id=subject_id,
                evidence_id=evidence_id,
                relationship=relationship,
                transform_id=transform_id,
                created_at=utc_now(),
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not record Provenance: {exc}") from exc

        self._by_id[candidate.provenance_id] = candidate
        self._by_subject.setdefault((subject_type, subject_id), []).append(
            candidate.provenance_id
        )
        return candidate

    def get_provenance(self, provenance_id: str) -> Provenance:
        try:
            return self._by_id[provenance_id]
        except KeyError:
            raise NotFoundError(f"no Provenance with provenance_id '{provenance_id}'") from None

    def trace_provenance(self, *, subject_type: str, subject_id: str) -> list[Provenance]:
        ids = self._by_subject.get((subject_type, subject_id), [])
        return [self._by_id[i] for i in ids]
