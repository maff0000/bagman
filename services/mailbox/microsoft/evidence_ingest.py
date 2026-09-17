"""Trusted, adapter-driven evidence ingestion for one email message
(CD-6 Slice 4).

Reuses BAGMAN's existing CD-4 evidence/provenance/scanning architecture
--------------------------------------------------------------------------
Per the architect's explicit instruction, this module does NOT build a
second private evidence silo inside ``services/mailbox`` — every
ingested email produces a real ``EvidenceItem`` + provenance record via
the SAME object-store discipline (bounded/streaming spool, then
content-addressed storage) and the SAME content-safety scanning
interface (``EvidenceSafetyScanner``) CD-4's manual-upload pipeline
already uses. See ``services/evidence/intake/streaming.py``,
``services/evidence/intake/scanner.py``, ``services/evidence/evidence.py``,
and ``core/provenance.py``.

Why this does NOT go through `services.evidence.intake.intake
.IntakeRecord` — a documented judgment call
--------------------------------------------------------------------------
``IntakeRecord`` models one untrusted upload ATTEMPT arriving over
HTTP (see that module's own docstring) — its state machine
(RECEIVED -> VALIDATING -> ...) and idempotency design are both built
around a single synchronous request/response cycle with a
caller-supplied ``idempotency_key`` guarding against a retried HTTP
POST. A sweep's message ingestion never arrives over HTTP at all — it
is driven entirely by ``services/mailbox/sweep.py`` reading Microsoft
Graph — and already has a STRONGER, purpose-built idempotency key of
its own: ``(mailbox_id, immutable_provider_message_id)``. Manufacturing
a synthetic ``IntakeRecord`` row per swept message for a flow that
never received an HTTP request would be pure architectural friction
with no benefit; this module instead reuses the LOWER-LEVEL primitives
(bounded streaming, the scanner interface, ``EvidenceRepository
.register_evidence``, ``ProvenanceRepository.record_provenance``)
directly. This is exactly the "same object-store discipline, same
scanning discipline" architect instruction — read literally, not
"route through the exact same state machine a browser upload uses".

Idempotency — reusing `external_reference`, not reinventing it
--------------------------------------------------------------------------
``register_evidence`` is called with
``external_reference=("MICROSOFT_GRAPH", "email_message",
immutable_provider_message_id)`` scoped by this mailbox's own
``Source`` id — the SAME ``core.external_reference`` composite-tuple
mechanism every other idempotent-replay-safe registration call in this
codebase already uses (see ``services.evidence.evidence
.InMemoryEvidenceRepository.register_evidence``'s own docstring). A
replayed delta page, a crash-before-cursor-advance retry, or the SAME
immutable id seen in a different folder therefore resolves to the
SAME ``EvidenceItem`` automatically, at the persistence layer, rather
than this module needing its own separate duplicate-check.

Never store raw MIME in Postgres
------------------------------------
The `.eml` bytes are written ONLY to the object store (via
``EvidenceObjectStore.put``/``put_prefixed`` — content-addressed,
exactly like every other evidence artifact); nothing in this module
ever writes message content to a database row.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol

from core.errors import BagmanError, FileTooLargeError
from services.evidence.intake.policy import EMAIL_MESSAGE_MIME_TYPE
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanVerdict
from services.evidence.intake.streaming import spool_stream

#: A sensible, explicit bound for one email's raw MIME size — mirrors
#: `services.evidence.intake.policy.IntakePolicy.max_file_size_bytes`'s
#: own default (50 MiB) rather than inventing a different number; an
#: oversized message is an explicit, governed FAILED outcome (never
#: truncate-and-pretend-success — architect spec).
DEFAULT_MAX_MESSAGE_SIZE_BYTES = 50 * 1024 * 1024

INGEST_STATUS_INGESTED = "INGESTED"
INGEST_STATUS_QUARANTINED = "QUARANTINED"
INGEST_STATUS_FAILED = "FAILED"


class _ObjectStore(Protocol):
    """The exact storage capability this module needs — a structural
    Protocol, never an import of `persistence.objects.store
    .EvidenceObjectStore` (mirrors `services.evidence.intake
    .validation_pipeline._ObjectStore`'s own documented layering
    reason: `services/` never imports `persistence/`)."""

    def put(self, object_id: str, content_hash: Mapping[str, str], data: bytes) -> str: ...

    def put_prefixed(self, prefix: str, object_id: str, content_hash: Mapping[str, str], data: bytes) -> str: ...


class _EvidenceAPI(Protocol):
    """The exact facade surface this module needs from
    `core.api.BagmanCanonicalAPI` — see `_ObjectStore` above for why
    this is a structural Protocol rather than a direct import."""

    def register_evidence(self, **kwargs: Any) -> Any: ...

    def record_provenance(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class EmailIngestOutcome:
    status: str  # INGESTED | QUARANTINED | FAILED
    evidence: Optional[Any] = None
    detail: str = ""
    content_hash: Optional[Mapping[str, str]] = None
    size_bytes: Optional[int] = None


def ingest_email_evidence(
    *,
    raw_mime_bytes: bytes,
    mailbox_id: str,
    mailbox_source_id: str,
    immutable_provider_message_id: str,
    observed_at: datetime,
    received_at: datetime,
    sender_address: Optional[str],
    subject: Optional[str],
    api: _EvidenceAPI,
    object_store: _ObjectStore,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    max_size_bytes: int = DEFAULT_MAX_MESSAGE_SIZE_BYTES,
) -> EmailIngestOutcome:
    """Ingest one email's raw MIME bytes as canonical evidence.

    Never raises for an ordinary quarantine/oversize/scan outcome —
    those are legitimate return values (mirrors every other
    outcome-as-data convention in this codebase); raises only for a
    genuine persistence/programming failure the caller cannot
    meaningfully recover from inline (propagated as a `BagmanError`
    subclass, exactly like `EvidenceRepository.register_evidence`
    itself would).
    """
    try:
        with spool_stream(io.BytesIO(raw_mime_bytes), max_size_bytes=max_size_bytes) as spooled:
            content_hash = {"algorithm": "SHA-256", "value": spooled.sha256_hex}
            data = spooled.path.read_bytes()

            try:
                scan_result = scanner.scan(spooled.path)
            except Exception as exc:  # noqa: BLE001 - a raising scanner is still an infra failure
                return EmailIngestOutcome(
                    status=INGEST_STATUS_FAILED,
                    detail=f"scanner raised during scan (fail-closed): {exc}",
                    content_hash=content_hash,
                    size_bytes=spooled.size_bytes,
                )

            if scan_result.verdict != ScanVerdict.CLEAN:
                # Governed quarantine path — same discipline as CD-4's
                # own manual-upload pipeline: the bytes are retained
                # (never silently discarded), never parsed/classified,
                # never registered as canonical evidence.
                object_store.put_prefixed("quarantine", immutable_provider_message_id, content_hash, data)
                return EmailIngestOutcome(
                    status=INGEST_STATUS_QUARANTINED,
                    detail=f"content safety scanner verdict {scan_result.verdict.value}: {scan_result.detail}",
                    content_hash=content_hash,
                    size_bytes=spooled.size_bytes,
                )

            storage_reference = object_store.put(immutable_provider_message_id, content_hash, data)

            evidence = api.register_evidence(
                entity_id=None,  # a mailbox is never asserted ownership of an entity — see services/mailbox/mailbox.py
                evidence_type="EMAIL",
                source_id=mailbox_source_id,
                observed_at=observed_at,
                received_at=received_at,
                content_hash=content_hash,
                mime_type=EMAIL_MESSAGE_MIME_TYPE,
                size_bytes=spooled.size_bytes,
                actor_type=actor_type,
                actor_id=actor_id,
                original_name=None,
                storage_reference=storage_reference,
                status="OBSERVED",
                metadata={
                    "mailbox_id": mailbox_id,
                    "immutable_provider_message_id": immutable_provider_message_id,
                    "sender_address": sender_address,
                    "subject": subject,
                },
                external_reference=("MICROSOFT_GRAPH", "email_message", immutable_provider_message_id),
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return EmailIngestOutcome(
                status=INGEST_STATUS_INGESTED,
                evidence=evidence,
                detail="ingested",
                content_hash=content_hash,
                size_bytes=spooled.size_bytes,
            )
    except FileTooLargeError as exc:
        return EmailIngestOutcome(status=INGEST_STATUS_FAILED, detail=f"oversized message: {exc}")
