"""Canonical EvidenceItem domain model (PID §6-8) and its repository.

Original received evidence is immutable (PID §7): ``evidence_id``,
``content_hash``, ``source_id``, ``mime_type``, ``size_bytes``,
``original_name``, ``observed_at``, ``received_at``, and ``created_at``
never change once an ``EvidenceItem`` is created. The only two
mutations this repository performs are narrow and explicit:

* ``update_status`` — replaces the evidence workflow status only.
* ``assign_entity`` — resolves ``entity_id`` exactly once, from
  unresolved (``None``) to a concrete governed entity; reassignment of
  an already-resolved entity is out of CD-2 scope (a later delivery's
  correction pattern, not a mutation this repository performs).

Both produce a *new* ``EvidenceItem`` snapshot (frozen dataclasses are
never mutated in place) that replaces the repository's current record
for that ``evidence_id``.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple, Union

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ImmutabilityViolationError, NotFoundError, ValidationError
from core.external_reference import ExternalReferenceRepository
from core.text_matching import domain_from_address, normalize_address, normalize_domain
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "evidence/bagman.evidence.v1.schema.json"

#: Evidence workflow states documented in PID §25. The contract's own
#: `status` field is deliberately an open string (not a closed enum),
#: but `update_status` enforces this closed set at the service layer —
#: financial statuses (`PAID`, `RECONCILED`, `POSTED_TO_XERO`, ...)
#: must never appear here (PID §25) and are rejected by not being in
#: this set.
STATUSES = frozenset({"OBSERVED", "REGISTERED", "AVAILABLE", "SUPERSEDED", "QUARANTINED", "ERROR"})

#: (provider, resource_type, external_id) — `source_id` is supplied
#: separately by the `register_evidence` call itself and combined with
#: this triple to form the full composite uniqueness tuple (PID §10/§34).
ExternalReferenceHint = Tuple[str, str, str]

#: CD-6 Slice 5 WI-2 — the `evidence_type` value every real mailbox
#: ingestion path uses today (see
#: `services.mailbox.microsoft.evidence_ingest.ingest_email_evidence`'s
#: own `register_evidence(evidence_type="EMAIL", ...)` call; the IMAP/
#: Gmail adapters mirror this exactly). Used purely as an additional,
#: NARROWING SQL-level filter by
#: `find_candidate_evidence_for_sender` below — never trusted as the
#: sole/authoritative test of "is this evidence mailbox-shaped" (that
#: would require a real join to `sources.source_type`, which this
#: narrow WI-2 addition does not attempt — see that method's own
#: docstring for the full reasoning this is a documented, non-blocking
#: judgment call).
MAILBOX_EVIDENCE_TYPES = frozenset({"EMAIL"})

#: Default bound for `find_candidate_evidence_for_sender` when a caller
#: does not specify one — genuinely bounded, never "load everything".
_DEFAULT_CANDIDATE_LIMIT = 200


def _normalize_content_hash(content_hash: Union[str, Mapping[str, str]]) -> dict:
    """Accept either a bare SHA-256 hex digest string, or an explicit
    ``{"algorithm": ..., "value": ...}`` mapping (for a future
    non-default algorithm)."""
    if isinstance(content_hash, str):
        return {"algorithm": "SHA-256", "value": content_hash}
    if isinstance(content_hash, Mapping):
        return {
            "algorithm": content_hash.get("algorithm", "SHA-256"),
            "value": content_hash["value"],
        }
    raise ValidationError(
        "content_hash must be a hex-digest string or a mapping with 'value' "
        "(and optionally 'algorithm') keys"
    )


@dataclass(frozen=True)
class EvidenceItem:
    """Something BAGMAN has observed or received (PID §6). Original
    evidence is immutable; see the module docstring for the two narrow
    exceptions this repository allows."""

    evidence_id: str
    entity_id: Optional[str]
    evidence_type: str
    source_id: str
    observed_at: datetime
    received_at: datetime
    content_hash: Mapping[str, str]
    mime_type: str
    size_bytes: int
    status: str
    created_at: datetime
    original_name: Optional[str] = None
    storage_reference: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/evidence/bagman.evidence.v1.schema.json``."""
        result: dict = {
            "evidence_id": self.evidence_id,
            "entity_id": self.entity_id,
            "evidence_type": self.evidence_type,
            "source_id": self.source_id,
            "observed_at": to_contract_string(self.observed_at),
            "received_at": to_contract_string(self.received_at),
            "content_hash": dict(self.content_hash),
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": to_contract_string(self.created_at),
            "metadata": dict(self.metadata),
        }
        if self.original_name is not None:
            result["original_name"] = self.original_name
        if self.storage_reference is not None:
            result["storage_reference"] = self.storage_reference
        return result


class EvidenceRepository(abc.ABC):
    """Repository abstraction for EvidenceItem (PID §22)."""

    @abc.abstractmethod
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
        original_name: Optional[str] = None,
        storage_reference: Optional[str] = None,
        status: str = "OBSERVED",
        metadata: Optional[Mapping[str, Any]] = None,
        external_reference: Optional[ExternalReferenceHint] = None,
    ) -> EvidenceItem:
        raise NotImplementedError

    @abc.abstractmethod
    def get_evidence(self, evidence_id: str) -> EvidenceItem:
        raise NotImplementedError

    @abc.abstractmethod
    def list_evidence(
        self,
        *,
        entity_id: Optional[str] = None,
        evidence_type: Optional[str] = None,
        received_at_from: Optional[datetime] = None,
        received_at_to: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[EvidenceItem]:
        """List EvidenceItems, most-recently-received first (CD-4 WI-3,
        PID §44-46). Same filter/pagination doctrine as
        ``services.evidence.intake.intake.IntakeRepository.list_intake_records``:
        default ordering ``received_at DESC`` with ``evidence_id`` as
        deterministic tie-breaker; every filter is optional (omitted ==
        match all); ``limit=None`` (default) returns every matching
        record, preserving every existing caller's "no pagination"
        behaviour — an explicit page-size boundary is the HTTP layer's
        job (``app/api/routers/intake.py``'s ``GET /internal/evidence``).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def update_status(self, evidence_id: str, new_status: str) -> EvidenceItem:
        raise NotImplementedError

    @abc.abstractmethod
    def assign_entity(self, evidence_id: str, entity_id: str) -> EvidenceItem:
        raise NotImplementedError

    @abc.abstractmethod
    def find_candidate_evidence_for_sender(
        self,
        *,
        sender_domain: str,
        sender_address: Optional[str] = None,
        limit: int = _DEFAULT_CANDIDATE_LIMIT,
    ) -> list[EvidenceItem]:
        """CD-6 Slice 5 WI-2 — a bounded, NARROWING candidate query for
        the classification-rule observed-evidence guard/preview
        (``services.evidence.classification_matcher``): every
        ``EvidenceItem`` whose ``metadata`` carries both
        ``sender_address`` and ``subject`` keys, AND whose
        ``metadata['sender_address']`` plausibly belongs to
        ``sender_domain`` (exactly, normalised) — or, when
        ``sender_address`` is also supplied, whose
        ``metadata['sender_address']`` exactly equals it (normalised) —
        most-recently-``received_at``-first, bounded to ``limit`` rows.

        This is a NARROWING optimisation only, never the source of
        match truth: the Postgres implementation applies a loose
        SQL-level ``ILIKE``/JSONB-key-presence filter to avoid a full
        table scan as the evidence corpus grows, but the CALLER (the
        observed-evidence guard) is responsible for re-testing every
        returned candidate through the same normalisation/matching
        logic (``core.text_matching``) before counting it as a real
        match — a divergence between SQL ``ILIKE`` and the Python
        normaliser must never silently misclassify (WO's own explicit
        instruction). ``sender_domain`` itself is assumed already
        normalised by the caller (mirrors every other method on this
        repository's own "caller normalises first" discipline);
        ``sender_address``, if given, is normalised here defensively.

        Never returns more than ``limit`` rows — there is no
        ``limit=None`` "unbounded" mode on this method (unlike
        ``list_evidence``): a generic "list every EvidenceItem" query is
        deliberately NOT what this method is for.
        """
        raise NotImplementedError


class InMemoryEvidenceRepository(EvidenceRepository):
    """Narrow in-memory reference implementation (PID §21 Option A).

    Constructed with an ``ExternalReferenceRepository`` so
    ``register_evidence`` can look up an ``external_reference`` hint
    for idempotent replay detection, and — when that lookup finds
    nothing (a genuinely new observation) — write the tuple through to
    that repository itself, pointing at the newly created
    ``EvidenceItem``. This makes a single call to ``register_evidence``
    self-sufficient for idempotent replay (PID §34): a caller that
    simply retries the same call with the same tuple, with no separate
    call of its own, still gets back the same ``EvidenceItem`` on the
    second attempt. Both this write-through and the standalone
    ``link_external_reference`` operation write through the same
    underlying ``ExternalReferenceRepository.link_external_reference``
    method — there is only one place a row is actually created.
    """

    def __init__(self, external_reference_repository: ExternalReferenceRepository) -> None:
        self._external_reference_repository = external_reference_repository
        self._by_id: dict[str, EvidenceItem] = {}

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
        original_name: Optional[str] = None,
        storage_reference: Optional[str] = None,
        status: str = "OBSERVED",
        metadata: Optional[Mapping[str, Any]] = None,
        external_reference: Optional[ExternalReferenceHint] = None,
    ) -> EvidenceItem:
        if external_reference is not None:
            provider, resource_type, external_id = external_reference
            existing_ref = self._external_reference_repository.find_by_tuple(
                provider=provider,
                source_id=source_id,
                resource_type=resource_type,
                external_id=external_id,
            )
            if existing_ref is not None and existing_ref.canonical_object_type == "EvidenceItem":
                existing_evidence = self._by_id.get(existing_ref.canonical_object_id)
                if existing_evidence is not None:
                    # Idempotent replay of the same observation.
                    return existing_evidence

        try:
            candidate = EvidenceItem(
                evidence_id=identity.generate_id(),
                entity_id=entity_id,
                evidence_type=evidence_type,
                source_id=source_id,
                observed_at=observed_at,
                received_at=received_at,
                content_hash=_normalize_content_hash(content_hash),
                mime_type=mime_type,
                size_bytes=size_bytes,
                status=status,
                created_at=utc_now(),
                original_name=original_name,
                storage_reference=storage_reference,
                metadata=dict(metadata) if metadata is not None else {},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not register EvidenceItem: {exc}") from exc

        self._by_id[candidate.evidence_id] = candidate

        if external_reference is not None:
            provider, resource_type, external_id = external_reference
            # Write the tuple through to the ExternalReferenceRepository
            # now that a new EvidenceItem exists for it to point at,
            # so a bare retry of this same call (with no separate
            # link_external_reference call of its own) is idempotent
            # next time round (PID §34). If the tuple already maps to
            # some other canonical object, this raises
            # DuplicateExternalReferenceError, which is a genuine
            # conflict and must propagate rather than be swallowed.
            self._external_reference_repository.link_external_reference(
                provider=provider,
                source_id=source_id,
                resource_type=resource_type,
                external_id=external_id,
                canonical_object_type="EvidenceItem",
                canonical_object_id=candidate.evidence_id,
            )

        return candidate

    def get_evidence(self, evidence_id: str) -> EvidenceItem:
        try:
            return self._by_id[evidence_id]
        except KeyError:
            raise NotFoundError(f"no EvidenceItem with evidence_id '{evidence_id}'") from None

    def list_evidence(
        self,
        *,
        entity_id: Optional[str] = None,
        evidence_type: Optional[str] = None,
        received_at_from: Optional[datetime] = None,
        received_at_to: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[EvidenceItem]:
        items = list(self._by_id.values())
        if entity_id is not None:
            items = [i for i in items if i.entity_id == entity_id]
        if evidence_type is not None:
            items = [i for i in items if i.evidence_type == evidence_type]
        if received_at_from is not None:
            items = [i for i in items if i.received_at >= received_at_from]
        if received_at_to is not None:
            items = [i for i in items if i.received_at <= received_at_to]

        items.sort(key=lambda i: (i.received_at, i.evidence_id), reverse=True)

        if limit is None:
            return items[offset:]
        return items[offset : offset + limit]

    def update_status(self, evidence_id: str, new_status: str) -> EvidenceItem:
        if new_status not in STATUSES:
            raise ValidationError(
                f"'{new_status}' is not one of the documented PID §25 evidence status "
                f"values {sorted(STATUSES)}"
            )
        current = self.get_evidence(evidence_id)

        try:
            updated = dataclasses.replace(current, status=new_status)
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not update EvidenceItem status: {exc}") from exc

        self._by_id[evidence_id] = updated
        return updated

    def assign_entity(self, evidence_id: str, entity_id: str) -> EvidenceItem:
        current = self.get_evidence(evidence_id)
        if current.entity_id is not None:
            raise ImmutabilityViolationError(
                f"EvidenceItem '{evidence_id}' already has entity_id '{current.entity_id}' "
                "assigned; reassignment is out of CD-2 scope (a later delivery's correction "
                "pattern, not a mutation this repository performs)"
            )

        try:
            updated = dataclasses.replace(current, entity_id=entity_id)
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not assign entity to EvidenceItem: {exc}") from exc

        self._by_id[evidence_id] = updated
        return updated

    def find_candidate_evidence_for_sender(
        self,
        *,
        sender_domain: str,
        sender_address: Optional[str] = None,
        limit: int = _DEFAULT_CANDIDATE_LIMIT,
    ) -> list[EvidenceItem]:
        normalized_address = normalize_address(sender_address) if sender_address else None
        candidates: list[EvidenceItem] = []
        for item in self._by_id.values():
            raw_sender = item.metadata.get("sender_address")
            raw_subject = item.metadata.get("subject")
            if not raw_sender or not raw_subject:
                continue
            try:
                item_address = normalize_address(raw_sender)
                item_domain = domain_from_address(item_address)
            except ValidationError:
                continue
            if normalized_address is not None:
                if item_address != normalized_address:
                    continue
            elif item_domain != normalize_domain(sender_domain):
                continue
            candidates.append(item)

        candidates.sort(key=lambda i: (i.received_at, i.evidence_id), reverse=True)
        return candidates[:limit]
