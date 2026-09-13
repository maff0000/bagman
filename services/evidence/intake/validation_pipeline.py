"""The CD-4 WI-2 orchestrating pipeline — content validation & quarantine
(PID §68 WI-2).

:func:`run_intake_validation` is the single entry point WI-3's future
governed HTTP endpoint calls once it already has an ``IntakeRecord`` in
``RECEIVED`` (created via WI-1's
``IntakeRepository.create_intake_record``) and an untrusted byte
stream. It ties together every other WI-2 module — filename safety,
streaming/bounded ingest, MIME detection, content-type policy, the
safety scanner, and quarantine/staging storage — against the intake
state machine WI-1 already built (``VALIDATING`` ->
``{QUARANTINED, ACCEPTED, REJECTED, FAILED}``), calling
``IntakeRepository.transition_status``/:func:`services.evidence.intake.intake.transition`
for every state change rather than ever mutating an ``IntakeRecord``
directly.

Handoff boundary — READ BEFORE EXTENDING THIS MODULE
-------------------------------------------------------
This pipeline's job ends the moment a record reaches ``ACCEPTED``. It
populates ``detected_mime_type``/``size_bytes``/``content_hash`` and
stores the validated original bytes via the object store's
:meth:`persistence.objects.store.EvidenceObjectStore.put_prefixed`
under the ``intake-staging`` prefix (PID §22) — a temporary, pending
location, NOT the canonical ``evidence/<evidence_id>/<hash>`` scheme
``put()`` uses. It does **not**:

* register a canonical ``EvidenceItem`` (``ACCEPTED -> REGISTERED`` is
  PID §26/§27 territory — the governed API orchestration WI-3 builds);
* move/copy the staged bytes into canonical evidence storage;
* emit any audit event (PID §30/§31 audit-event emission is WI-3's
  orchestration-layer responsibility, exactly as WI-1's
  ``component.yaml`` already documents for intake-record creation
  itself).

It would be easy to accidentally reach past ``ACCEPTED`` into that
territory while wiring this up end-to-end — resist that; WI-3 is what
reads an ``ACCEPTED`` ``IntakeRecord`` plus its staged
``storage_reference`` (recorded in ``metadata``) and performs the
actual registration handoff.

Why this module depends on a structural Protocol, not a persistence
import
----------------------------------------------------------------------
Nowhere under ``core/`` or ``services/`` imports ``persistence/``
anywhere in this codebase (``persistence/postgres/intake_repository.py``
imports FROM ``services.evidence.intake.intake``, never the reverse) —
that is the established layering direction: infrastructure
(``persistence/``) depends on domain (``services/``/``core/``)
contracts, never the other way round. Rather than being the first
module to invert that by importing
``persistence.objects.store.EvidenceObjectStore`` directly, this
module declares its own minimal structural ``_ObjectStore`` ``Protocol``
below, naming exactly the one method it needs
(``put_prefixed``). Any real ``EvidenceObjectStore`` implementation
already satisfies this structurally (``typing.Protocol`` is duck-typed)
— a caller passes a real ``MinIOObjectStore``/``InMemoryObjectStore``
instance without either module needing to import the other.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from core.errors import (
    ContentTypeMismatchError,
    FileTooLargeError,
    ScanFailedError,
    UnsupportedContentTypeError,
    ValidationError,
)
from services.evidence.intake.content_sniffing import SNIFF_SAMPLE_SIZE, sniff
from services.evidence.intake.filename_safety import assert_filename_safe
from services.evidence.intake.intake import IntakeRecord, IntakeRepository
from services.evidence.intake.policy import DEFAULT_INTAKE_POLICY, IntakePolicy
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanVerdict
from services.evidence.intake.streaming import StreamLike, spool_stream


class _ObjectStore(Protocol):
    """The one storage capability this pipeline needs — see module
    docstring for why this is a structural ``Protocol`` rather than an
    import of ``persistence.objects.store.EvidenceObjectStore``."""

    def put_prefixed(self, prefix: str, object_id: str, content_hash: Mapping[str, str], data: bytes) -> str: ...


def run_intake_validation(
    *,
    intake_id: str,
    stream: StreamLike,
    repository: IntakeRepository,
    object_store: _ObjectStore,
    scanner: EvidenceSafetyScanner,
    policy: IntakePolicy = DEFAULT_INTAKE_POLICY,
) -> IntakeRecord:
    """Validate the untrusted content behind ``stream`` against an
    already-``RECEIVED`` ``IntakeRecord`` identified by ``intake_id``,
    driving it through ``VALIDATING`` to its terminal-or-``ACCEPTED``
    outcome (see module docstring for the ``ACCEPTED`` handoff
    boundary).

    Returns the final ``IntakeRecord`` snapshot. Never raises for an
    ordinary validation/quarantine/scan outcome — REJECTED/QUARANTINED/
    FAILED are all legitimate return values, not exceptions (mirroring
    every other repository method in this codebase: a domain OUTCOME is
    a value, an exception is reserved for a genuine programming/
    infrastructure error, e.g. ``intake_id`` not existing at all, or a
    contract-validation failure on a field this pipeline itself sets
    incorrectly).
    """
    record = repository.get_intake_record(intake_id)
    record = repository.transition_status(intake_id, "VALIDATING")

    try:
        assert_filename_safe(record.original_filename, max_length=policy.max_filename_length)
    except ValidationError:
        return repository.transition_status(intake_id, "REJECTED", failure_code="UNSAFE_FILENAME")

    try:
        with spool_stream(stream, max_size_bytes=policy.max_file_size_bytes) as spooled:
            content_hash = {"algorithm": "SHA-256", "value": spooled.sha256_hex}

            with open(spooled.path, "rb") as handle:
                header_sample = handle.read(SNIFF_SAMPLE_SIZE)
            sniff_result = sniff(header_sample)
            detected_mime_type = sniff_result.mime_type

            reported_mime_type = record.reported_mime_type
            mime_mismatch = (
                reported_mime_type is not None and reported_mime_type != detected_mime_type
            )
            if mime_mismatch and policy.mime_mismatch_policy == "REJECT":
                raise ContentTypeMismatchError(
                    f"reported_mime_type '{reported_mime_type}' does not match "
                    f"detected_mime_type '{detected_mime_type}' and policy "
                    f"'{policy.policy_id}' requires rejection on mismatch (PID §15)"
                )

            def _quarantine(reason: str) -> IntakeRecord:
                data = spooled.path.read_bytes()
                object_store.put_prefixed("quarantine", intake_id, content_hash, data)
                return repository.transition_status(
                    intake_id,
                    "QUARANTINED",
                    quarantine_reason=reason,
                    detected_mime_type=detected_mime_type,
                    size_bytes=spooled.size_bytes,
                    content_hash=content_hash,
                )

            if sniff_result.is_archive:
                if policy.archive_treatment == "QUARANTINE":
                    return _quarantine(
                        f"detected archive content ('{detected_mime_type}') is not a "
                        "supported evidence container (PID §17)"
                    )
                raise UnsupportedContentTypeError(
                    f"detected archive content ('{detected_mime_type}') is not a "
                    "supported evidence container (PID §17)"
                )

            if sniff_result.is_executable:
                if policy.executable_treatment == "QUARANTINE":
                    return _quarantine(
                        f"detected executable/script content ('{detected_mime_type}') "
                        "is not accepted as ordinary evidence (PID §18)"
                    )
                raise UnsupportedContentTypeError(
                    f"detected executable/script content ('{detected_mime_type}') is "
                    "not accepted as ordinary evidence (PID §18)"
                )

            if detected_mime_type in policy.rejected_mime_types:
                raise UnsupportedContentTypeError(
                    f"detected content type '{detected_mime_type}' is explicitly "
                    f"reject-listed by policy '{policy.policy_id}'"
                )

            if detected_mime_type in policy.quarantine_mime_types:
                return _quarantine(
                    f"detected content type '{detected_mime_type}' is explicitly "
                    f"quarantine-listed by policy '{policy.policy_id}'"
                )

            if detected_mime_type not in policy.accepted_mime_types:
                if policy.unsupported_content_treatment == "QUARANTINE":
                    return _quarantine(
                        f"detected content type '{detected_mime_type}' is not in the "
                        f"accepted set (PID §16), quarantined per policy "
                        f"'{policy.policy_id}'"
                    )
                raise UnsupportedContentTypeError(
                    f"detected content type '{detected_mime_type}' is not one of the "
                    f"accepted types {sorted(policy.accepted_mime_types)} (PID §16)"
                )

            if policy.scanner_required:
                try:
                    scan_result = scanner.scan(spooled.path)
                except Exception as exc:  # noqa: BLE001 - a raising scanner is still an infra failure
                    raise ScanFailedError(
                        f"scanner raised during scan (fail-closed, PID §20): {exc}"
                    ) from exc

                if scan_result.verdict == ScanVerdict.CLEAN:
                    pass
                elif scan_result.verdict in (ScanVerdict.SUSPICIOUS, ScanVerdict.MALICIOUS):
                    return _quarantine(
                        f"content safety scanner verdict {scan_result.verdict.value}: "
                        f"{scan_result.detail}"
                    )
                elif scan_result.verdict == ScanVerdict.UNSUPPORTED:
                    if policy.scan_unsupported_treatment == "FAIL":
                        raise ScanFailedError(
                            f"scanner could not evaluate this content (UNSUPPORTED): "
                            f"{scan_result.detail}"
                        )
                    return _quarantine(
                        "content safety scanner could not evaluate this content "
                        f"(UNSUPPORTED, fail-closed per PID §20): {scan_result.detail}"
                    )
                else:  # ScanVerdict.SCAN_ERROR
                    if policy.scan_error_treatment == "QUARANTINE":
                        return _quarantine(
                            f"content safety scanner failed (fail-closed, PID §20): "
                            f"{scan_result.detail}"
                        )
                    raise ScanFailedError(
                        f"content safety scanner failed (fail-closed, PID §20): "
                        f"{scan_result.detail}"
                    )

            data = spooled.path.read_bytes()
            storage_reference = object_store.put_prefixed("intake-staging", intake_id, content_hash, data)

            metadata: dict[str, Any] = dict(record.metadata)
            metadata["storage_reference"] = storage_reference
            if mime_mismatch:
                metadata["mime_mismatch_observed"] = True
                metadata["reported_mime_type_at_validation"] = reported_mime_type

            return repository.transition_status(
                intake_id,
                "ACCEPTED",
                detected_mime_type=detected_mime_type,
                size_bytes=spooled.size_bytes,
                content_hash=content_hash,
                metadata=metadata,
            )
    except FileTooLargeError:
        return repository.transition_status(intake_id, "REJECTED", failure_code="FILE_TOO_LARGE")
    except UnsupportedContentTypeError:
        return repository.transition_status(intake_id, "REJECTED", failure_code="UNSUPPORTED_CONTENT_TYPE")
    except ContentTypeMismatchError:
        return repository.transition_status(intake_id, "REJECTED", failure_code="CONTENT_TYPE_MISMATCH")
    except ScanFailedError:
        return repository.transition_status(intake_id, "FAILED", failure_code="SCAN_FAILED")
