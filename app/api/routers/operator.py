"""``POST /internal/operator/chat`` — the Ask BAGMAN HTTP surface (CD-5
PID §42-44/§68, WI-3).

Kept in its own file, deliberately never adding routes to
``app/api/routers/ai.py`` (WI-2's own, in-parallel-created file this
worktree cannot see) — see this WI's dispatch instructions for the
merge-collision-avoidance rationale.

Thin HTTP wrapper, same discipline as every other router in this
package (PID §24): parse the request, call one function
(``agent.bagman.orchestrator.handle_operator_message``), render the
result. Every domain rule (`AIInvocation` lifecycle, tool-calling loop,
audit emission, prompt-injection structural defence) lives in
``agent/bagman``/``agent/tools`` — nothing here. Every
``core.errors.BagmanError`` this raises (e.g. `ValidationError` for an
empty message or a missing canonical subject reference — see
``agent.bagman.orchestrator``'s own "general chat has no evidence_id"
docstring section; `ActiveInvocationConflictError` for a duplicate
in-flight Ask BAGMAN request about the same subject) propagates
uncaught, translated to the correct HTTP status by ``app/api/main.py``'s
centralised exception handlers, exactly like every other router.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from agent.bagman.orchestrator import handle_operator_message
from app.api.composition import get_composition

router = APIRouter(prefix="/internal/operator")


class OperatorChatRequest(BaseModel):
    message: str
    actor_type: str
    actor_id: str
    correlation_id: Optional[str] = None
    evidence_id: Optional[str] = None
    intake_id: Optional[str] = None
    entity_id: Optional[str] = None


@router.post("/chat")
async def operator_chat(payload: OperatorChatRequest) -> dict[str, Any]:
    """Response shape (PID §68 — "exact routing may vary", documented
    here as this WI's call): the full ``AIInvocation.to_dict()`` (PID
    §26-30 provenance), plus two GUI-convenience top-level fields the
    WI-4 GUI needs to render Ask BAGMAN's reply without re-deriving them
    from ``output`` itself — ``response_text`` (the final answer, or
    ``null`` if this turn failed before producing one) and
    ``referenced_evidence_ids`` (so the GUI can render clickable
    evidence links per PID §44, even when `output` is `None` on a
    failure).
    """
    composition = get_composition()
    result = handle_operator_message(
        message=payload.message,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        correlation_id=payload.correlation_id,
        evidence_id=payload.evidence_id,
        intake_id=payload.intake_id,
        entity_id=payload.entity_id,
        repository=composition.ai_invocation_repository,
        claude_client=composition.claude_client,
        tool_registry=composition.tool_registry,
        record_audit_event=composition.api.record_audit_event,
    )
    body = result.invocation.to_dict()
    body["response_text"] = result.response_text
    body["referenced_evidence_ids"] = list(result.referenced_evidence_ids)
    return body
