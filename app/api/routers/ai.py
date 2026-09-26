"""``/internal/ai/*`` — the BACKGROUND AI task HTTP surface (CD-5 PID
§48/§68-71, WI-2).

Thin HTTP wrapper over ``ai.gateway.background.run_background_task``
(PID §24/§33's existing "no domain logic in a router" doctrine — see
``app/api/routers/internal.py``'s own module docstring, this router's
closest precedent). The request body deliberately carries only
``task_id``/``task_version``/``input_references``/``actor_type``/
``actor_id``/``correlation_id`` (PID §69's own suggested shape) — there
is no "model"/"provider"/"capability_alias" override field anywhere on
this surface, structurally: a caller can never select a raw model or
bypass BAGMAN's own alias-only routing from here (PID §77's "no
prompt-controlled provider/model/alias name"). See
``tests/security/test_ai_litellm_alias_lockdown.py`` for the test that
proves this request shape stays that way.

Evidence-content resolution (a WI-2 judgment call, see
``ai/gateway/background.py``'s own module docstring for the full
rationale): this router — not the orchestration function — resolves
`input_references["evidence_id"]` to the actual stored bytes via
``composition.api.get_evidence`` + ``composition.object_store.get``,
decoding them as UTF-8 (best-effort, ``errors="replace"``) into a plain
text string. This is a deliberately bounded, honestly-scoped stand-in
for real document-content extraction (PDF/OCR/etc.), which does not
exist anywhere in BAGMAN yet and is not this WI's job to build.

``ActiveInvocationConflictError`` (PID §73) propagates uncaught from
``run_background_task`` — mapped to HTTP 409 by
``app/api/main.py``'s centralised ``_STATUS_BY_ERROR_TYPE`` (this WI's
own addition there).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai.gateway.background import run_background_task
from ai.invocation import BACKGROUND_CAPABILITY_ALIASES
from app.api.composition import get_composition
from core.errors import ValidationError

router = APIRouter(prefix="/internal/ai")

#: PID §45's "no unbounded return-everything endpoint" doctrine, reused
#: verbatim from app/api/routers/intake.py / internal.py.
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200

#: CD-6 Slice 5 WI-3 §22 — `DOCUMENT_TYPE_PROPOSAL` v2 must NEVER be
#: reachable through this generic surface. This router's own
#: `_resolve_evidence_content` (see its docstring above) is a
#: best-effort raw-byte UTF-8 decode — exactly the unsafe path v2's
#: whole reason for existing (a governed, versioned, bounded
#: `services.evidence.classification_context` builder) replaces. v1
#: remains fully callable here, unchanged (WI-3 §22's own explicit
#: "V1 remains unchanged" instruction) — only this one, exact
#: `(task_id, task_version)` pair is refused; see
#: `tests/integration/test_architecture_boundaries.py`'s
#: request-level proof of this rejection.
_GENERIC_PATH_FORBIDDEN_TASKS: frozenset[tuple[str, int]] = frozenset({("DOCUMENT_TYPE_PROPOSAL", 2)})


# ---------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------


class RunBackgroundTaskRequest(BaseModel):
    """`POST /internal/ai/tasks` request body (PID §69). Deliberately
    has NO field that could name a physical model, a provider, or a
    capability_alias directly — `task_id` is the only routing input; a
    task's `preferred_capability` (`ai.tasks.TaskContract`) decides the
    alias, never the caller (PID §77)."""

    task_id: str
    task_version: int
    input_references: dict[str, Any]
    actor_type: str
    actor_id: str
    correlation_id: Optional[str] = None


# ---------------------------------------------------------------------
# evidence-content resolution (see module docstring)
# ---------------------------------------------------------------------


def _resolve_evidence_content(composition, input_references: dict[str, Any]) -> str:
    evidence_id = input_references.get("evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id:
        # No canonical evidence_id was supplied. Every CD-5 BACKGROUND
        # task's own input_schema requires one (see
        # ai/tasks.py::_DOCUMENT_INPUT_SCHEMA) — run_background_task's
        # own input_schema validation is the one place that reports
        # this cleanly as a ValidationError, so this function returns
        # an empty string here rather than raising a second time.
        return ""

    evidence = composition.api.get_evidence(evidence_id)  # NotFoundError propagates -> 404
    if not evidence.storage_reference:
        return ""

    raw_bytes = composition.object_store.get(evidence.storage_reference)
    # Best-effort UTF-8 decode — see module docstring: real
    # document-content extraction (PDF/OCR/binary formats) does not
    # exist in BAGMAN yet; a non-text document decodes lossily rather
    # than being meaningfully understood. This is a documented
    # limitation, not a silent defect.
    return raw_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------
# POST /internal/ai/tasks
# ---------------------------------------------------------------------


@router.post("/tasks")
async def create_background_task(payload: RunBackgroundTaskRequest) -> dict:
    if (payload.task_id, payload.task_version) in _GENERIC_PATH_FORBIDDEN_TASKS:
        raise ValidationError(
            f"task '{payload.task_id}' v{payload.task_version} may not be invoked through this "
            "generic POST /internal/ai/tasks surface (CD-6 Slice 5 WI-3 §22) — it requires the "
            "governed Slice-5 classification surface instead: POST "
            "/internal/evidence/{evidence_id}/classifications/ai-preview or "
            ".../orchestrated, which build the bounded, versioned evidence-classification "
            "context this task's input_schema requires, rather than this router's own "
            "best-effort raw-byte UTF-8 decode"
        )

    composition = get_composition()
    evidence_content = _resolve_evidence_content(composition, payload.input_references)

    invocation = run_background_task(
        task_id=payload.task_id,
        task_version=payload.task_version,
        input_references=payload.input_references,
        evidence_content=evidence_content,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        correlation_id=payload.correlation_id,
        repository=composition.ai_invocation_repository,
        litellm_client=composition.litellm_client,
        record_audit_event=composition.api.record_audit_event,
    )
    return invocation.to_dict()


# ---------------------------------------------------------------------
# GET /internal/ai/invocations, GET /internal/ai/invocations/{id}
# ---------------------------------------------------------------------


def _validate_pagination(limit: int, offset: int) -> None:
    if limit <= 0 or limit > _MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=422, detail=f"limit must be between 1 and {_MAX_PAGE_SIZE} (got {limit})"
        )
    if offset < 0:
        raise HTTPException(status_code=422, detail=f"offset must be >= 0 (got {offset})")


@router.get("/invocations")
async def list_ai_invocations(
    task_id: Optional[str] = None,
    task_version: Optional[int] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    correlation_id: Optional[str] = None,
    started_at_from: Optional[datetime] = None,
    started_at_to: Optional[datetime] = None,
    primary_input_reference: Optional[str] = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated `AIInvocation` listing — reuses
    `AIInvocationRepository.list_invocations`'s own filter/pagination
    parameters directly as query params (PID §68), same
    `{"items", "limit", "offset", "count"}` response shape CD-4 already
    established for `GET /internal/intake` / `GET /internal/evidence`.

    `primary_input_reference` (WI-4 addition, PID §45): filters to every
    invocation — of any `task_id`, in any status, terminal or not —
    whose subject (`ai.invocation.derive_primary_input_reference`)
    equals this value. In practice this is almost always an
    `evidence_id` (the Documents AI panel's own use: "every AI analysis
    that has ever run for this document"), but is named after the
    domain concept rather than `evidence_id` specifically because the
    same field also matches an `intake_id`- or `entity_id`-keyed
    invocation (PID §29/§73's generalised "subject").
    """
    _validate_pagination(limit, offset)
    composition = get_composition()
    records = composition.ai_invocation_repository.list_invocations(
        task_id=task_id,
        task_version=task_version,
        role=role,
        status=status,
        correlation_id=correlation_id,
        started_at_from=started_at_from,
        started_at_to=started_at_to,
        primary_input_reference=primary_input_reference,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [r.to_dict() for r in records],
        "limit": limit,
        "offset": offset,
        "count": len(records),
    }


@router.get("/invocations/{ai_invocation_id}")
async def get_ai_invocation(ai_invocation_id: str) -> dict:
    composition = get_composition()
    invocation = composition.ai_invocation_repository.get_invocation(ai_invocation_id)
    return invocation.to_dict()


# ---------------------------------------------------------------------
# GET /internal/ai/health
# ---------------------------------------------------------------------


@router.get("/health")
async def ai_health() -> dict:
    """PID §48 — a SEPARATE readiness surface from `/ready`: canonical
    BAGMAN evidence/runtime services must remain usable even when AI is
    unavailable, so this is never folded into `/ready`.

    Honest granularity (this WI's own dispatch requires this be stated
    plainly): `LiteLLMClient.is_available()` (see that module's own
    docstring) is a GATEWAY-WIDE reachability check — the existing
    LiteLLM installation exposes no per-alias health endpoint BAGMAN can
    probe cheaply. Every `bagman-*` key below therefore currently
    reflects the SAME underlying signal, not independently-measured
    per-alias health; this is a real limitation of what is cheaply
    checkable today, not a fabricated per-alias distinction. `checks` is
    a plain, open dict for exactly that reason, not a fixed/closed
    model. `claude_code` (CD-5 Gate-2 closure, PID §97) is a genuinely
    SEPARATE signal, not folded into the gateway-wide caveat above:
    `ClaudeCodeOperatorRunner.is_available()` probes the bounded
    headless Claude Code operator path — the sole, authoritative
    operator-intelligence surface — and can fail/recover completely
    independently of the LiteLLM background gateway (PID §46-48's own
    "distinct failure classes" doctrine).
    """
    composition = get_composition()
    gateway_reachable = composition.litellm_client.is_available()
    status_value = "ok" if gateway_reachable else "unreachable"
    checks: dict[str, str] = {
        alias.replace("-", "_"): status_value for alias in sorted(BACKGROUND_CAPABILITY_ALIASES)
    }
    checks["claude_code"] = (
        "ok" if composition.claude_code_operator_runner.is_available() else "unreachable"
    )
    return {
        "checks": checks,
        "granularity": (
            "gateway-wide for bagman-*: all three keys reflect one shared LiteLLM-gateway "
            "reachability signal, not independently-measured per-alias health "
            "(see ai/providers/litellm/client.py::LiteLLMClient.is_available); 'claude_code' is "
            "a genuinely separate, independently-measured signal for the sole authoritative "
            "Claude operator provider"
        ),
    }
