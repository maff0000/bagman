"""``POST /internal/operator/chat`` — the Ask BAGMAN HTTP surface (CD-5
PID §42-44/§68/§97).

**Architecture correction (2026-09-16, CD-5 Gate-2 closure):** this
router originally called ``agent.bagman.orchestrator
.handle_operator_message`` (a direct-Anthropic-API tool-calling loop
under BAGMAN's own Anthropic key). The architect ruled that assumption
wrong — BAGMAN does not hold an Anthropic API key; the authoritative
operator path is a bounded, headless Claude Code invocation. This
router now calls ``agent.claude_code.orchestrator
.handle_operator_message`` instead. The request/response contract
below is UNCHANGED — the new orchestrator returns the exact same
``AskBagmanResult`` shape the superseded one did, so no GUI change was
needed for this correction (PID §97's own "reuse the existing Ask
BAGMAN UI, do not create another chat application" instruction). See
``PID.md`` §97 and the CD-5 evidence file for the full history; the
superseded implementation (``agent/bagman/``, ``agent/tools/``,
``ai/providers/claude/``) is NOT deleted, only no longer called from
here — see those packages' own manifests for the architect's
classification note.

Kept in its own file, deliberately never adding routes to
``app/api/routers/ai.py`` (WI-2's own, in-parallel-created file this
worktree cannot see) — see CD-5 WI-3's original dispatch instructions
for the merge-collision-avoidance rationale, still honoured here.

Thin HTTP wrapper, same discipline as every other router in this
package (PID §24): parse the request, call one function
(``agent.claude_code.orchestrator.handle_operator_message``), render
the result. Every domain rule (`AIInvocation` lifecycle, bounded
context assembly, the headless Claude Code invocation itself, audit
emission, prompt-injection structural defence) lives in
``agent/claude_code`` — nothing here. Every ``core.errors.BagmanError``
this raises (e.g. `ValidationError` for an empty message or a missing
canonical subject reference; `NotFoundError` for a supplied
evidence_id/intake_id/entity_id that does not resolve;
`ActiveInvocationConflictError` for a duplicate in-flight Ask BAGMAN
request about the same subject) propagates uncaught, translated to the
correct HTTP status by ``app/api/main.py``'s centralised exception
handlers, exactly like every other router.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from agent.claude_code.orchestrator import handle_operator_message
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
    here as this WI's call, UNCHANGED by the Gate-2 architecture
    correction): the full ``AIInvocation.to_dict()`` (PID §26-30
    provenance), plus two GUI-convenience top-level fields the GUI
    needs to render Ask BAGMAN's reply without re-deriving them from
    ``output`` itself — ``response_text`` (the final answer, or
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
        runner=composition.claude_code_operator_runner,
        api=composition.api,
        object_store=composition.object_store,
        intake_repository=composition.intake_repository,
        record_audit_event=composition.api.record_audit_event,
    )
    body = result.invocation.to_dict()
    body["response_text"] = result.response_text
    body["referenced_evidence_ids"] = list(result.referenced_evidence_ids)
    return body
