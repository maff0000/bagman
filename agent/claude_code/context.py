"""Governed context assembly for one Ask BAGMAN turn (CD-5 Gate-2
closure, PID §97's "BAGMAN should assemble governed context through
its own application/service layer" — never a live tool-call, never
"dump the entire database/filesystem into the prompt").

Fetches directly from BAGMAN's own canonical services/repositories —
exactly the same calls ``app/api/routers/ai.py``'s
``_resolve_evidence_content`` and
``app/api/composition.py``'s ``_ProductionBackgroundTaskRunner`` already
make for the BACKGROUND-task path — never an HTTP self-call, never a
live Claude-invoked tool. Bounded: at most ONE evidence item's content
is ever included (whichever ``evidence_id`` the caller/GUI supplied),
never a list of everything BAGMAN knows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.api import BagmanCanonicalAPI
from persistence.objects.store import EvidenceObjectStore
from services.evidence.intake.intake import IntakeRepository

#: A hard cap on how much of one evidence item's raw content is ever
#: embedded in a single operator prompt — bounded context (PID §97),
#: not the whole document regardless of size.
MAX_EVIDENCE_CONTENT_CHARS = 20_000


@dataclass(frozen=True)
class OperatorContext:
    """Everything :mod:`agent.claude_code.orchestrator` needs to build
    one bounded, clearly-sectioned operator prompt — already split
    into TRUSTED canonical facts (BAGMAN's own governed records) and
    UNTRUSTED content (the evidence document's own bytes), per PID
    §97's explicit separation requirement."""

    trusted_summary_lines: tuple[str, ...]
    untrusted_evidence_content: Optional[str]
    untrusted_evidence_truncated: bool


def assemble_operator_context(
    *,
    api: BagmanCanonicalAPI,
    object_store: EvidenceObjectStore,
    intake_repository: IntakeRepository,
    evidence_id: Optional[str],
    intake_id: Optional[str],
    entity_id: Optional[str],
) -> OperatorContext:
    """Resolve whichever of `evidence_id`/`intake_id`/`entity_id` was
    supplied into governed, trusted facts plus (for `evidence_id` only)
    the underlying document's own untrusted content.

    Raises:
        core.errors.NotFoundError: any supplied id does not resolve to
            a real canonical record — propagates uncaught (mirrors
            `app/api/routers/ai.py::_resolve_evidence_content`'s own
            documented "NotFoundError propagates -> 404" contract).
    """
    trusted: list[str] = []
    untrusted_content: Optional[str] = None
    truncated = False

    if evidence_id:
        evidence = api.get_evidence(evidence_id)
        hash_summary = ", ".join(f"{alg}:{digest}" for alg, digest in sorted(evidence.content_hash.items()))
        trusted.append(
            f"- Evidence {evidence.evidence_id}: type={evidence.evidence_type}, "
            f"status={evidence.status}, original_name={evidence.original_name!r}, "
            f"content_hash={{{hash_summary}}}"
        )
        if evidence.storage_reference:
            raw_bytes = object_store.get(evidence.storage_reference)
            decoded = raw_bytes.decode("utf-8", errors="replace")
            if len(decoded) > MAX_EVIDENCE_CONTENT_CHARS:
                untrusted_content = decoded[:MAX_EVIDENCE_CONTENT_CHARS]
                truncated = True
            else:
                untrusted_content = decoded

    if intake_id:
        intake = intake_repository.get_intake_record(intake_id)
        trusted.append(
            f"- Intake {intake.intake_id}: status={intake.status}, "
            f"failure_code={intake.failure_code!r}, original_filename={intake.original_filename!r}"
        )

    if entity_id:
        entity = api.entity_repository.get_entity(entity_id)
        trusted.append(
            f"- Entity {entity.entity_id}: canonical_name={entity.canonical_name}, "
            f"entity_type={entity.entity_type}, status={entity.status}"
        )

    return OperatorContext(
        trusted_summary_lines=tuple(trusted),
        untrusted_evidence_content=untrusted_content,
        untrusted_evidence_truncated=truncated,
    )
