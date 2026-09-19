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
``ai/providers/claude/``) was subsequently removed entirely (2026-09-16
final cleanup delta, once proven to have zero live dependents) — see
the CD-5 evidence file §6n for the removal record.

**CD-6 reliability delta (2026-09-17, PID §98/§100.14/§100.16) — real
root-cause fix, not merely a defensive addition:** ``handle_operator_message``
is a fully SYNCHRONOUS function whose own call chain ends in a real,
blocking OS-level subprocess wait (``agent.claude_code.runner``'s
``subprocess.Popen(...).communicate(timeout=...)``, up to
``ASK_BAGMAN_V1``'s 90-second bound). Calling it directly from an
``async def`` handler with no ``await`` runs it ON the single asyncio
event-loop thread — meaning the ENTIRE ``bagman-api`` process (one
Uvicorn worker, no ``--workers``, see
``deployment/docker/api/entrypoint.sh``) is unresponsive to every other
request, health check, and its OWN graceful-shutdown signal handling
for the full duration of any single Ask BAGMAN call. This was proven,
live, against the real deployed Mac mini appliance, to be the
precondition for the stuck-``RUNNING`` ``AIInvocation`` PID §100.14
first recorded: the row this delivery's root-cause investigation traced
(``01a0af4f-6b57-779a-ae44-9a04e41d1365``, started
``2026-09-17T12:20:12Z``) was abandoned mid-flight by a deliberate,
UNRELATED ``bagman-api`` restart (Ask-BAGMAN-credential rotation, PID
§99.13) landing ~96 seconds later — squarely inside/just past the
runner's own 90s subprocess timeout window — while the process was
still synchronously blocked servicing this call, with no code left
running anywhere to ever record a terminal state afterward. A
controlled, isolated reproduction (a client that disconnected at 2s,
well before the server-side bound, with NO process restart) proved a
client disconnect ALONE does not strand a row — the server-side call
still runs to completion and reaches a terminal state, because Python
cannot be pre-empted mid-synchronous-call by a remote socket event with
no cooperative yield point in the call path; the actual precondition is
ANY event that terminates the `bagman-api` process itself while it is
blocked inside this call.

This router now offloads the call via
``starlette.concurrency.run_in_threadpool`` — the event loop stays free
to serve other requests/health checks while an Ask BAGMAN call is in
flight, which removes the single-worker-wide blocking behaviour this
root cause depends on. This is deliberately NOT presented as a complete
fix on its own (a background thread finishing normally still needs the
PROCESS to survive long enough to run its `finally`/return path) — the
durable half of the fix is the bounded, deterministic stale-`RUNNING`
recovery backstop in ``ai.invocation`` (covers this AND every other
possible cause of mid-flight process death: crash, OOM, host reboot),
and the new `TIMED_OUT` terminal state (see
``agent.claude_code.orchestrator``'s own module docstring) for a
genuine, non-abandoned runner timeout. See this delivery's own final
report for the full root-cause write-up and live acceptance evidence.

Kept in its own file, deliberately never adding routes to
``app/api/routers/ai.py`` (WI-2's own, in-parallel-created file this
worktree cannot see) — see CD-5 WI-3's original dispatch instructions
for the merge-collision-avoidance rationale, still honoured here.

Thin HTTP wrapper, same discipline as every other router in this
package (PID §24): parse the request, call one function
(``agent.claude_code.orchestrator.handle_operator_message``, via
``run_in_threadpool``), render the result. Every domain rule
(`AIInvocation` lifecycle, bounded context assembly, the headless
Claude Code invocation itself, audit emission, prompt-injection
structural defence) lives in ``agent/claude_code`` — nothing here.
Every ``core.errors.BagmanError`` this raises (e.g. `ValidationError`
for an empty message or a missing canonical subject reference;
`NotFoundError` for a supplied evidence_id/intake_id/entity_id that
does not resolve; `ActiveInvocationConflictError` for a duplicate
in-flight Ask BAGMAN request about the same subject) propagates
uncaught THROUGH the threadpool (`run_in_threadpool` re-raises the
worker thread's own exception in the calling coroutine — nothing here
needs to catch/re-translate it), to the correct HTTP status via
``app/api/main.py``'s centralised exception handlers, exactly like
every other router.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

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
    #: CD-6 reliability delta (PID §98/§100) — see
    #: `agent.claude_code.orchestrator.handle_operator_message`'s own
    #: docstring and `ai.invocation.derive_primary_input_reference`'s
    #: module docstring for the full design/precedence reasoning.
    conversation_id: Optional[str] = None
    #: A simple, honest literal naming the UI surface this call
    #: originated from (e.g. `"ask_bagman_drawer"`) — defaults to
    #: `"unknown"` inside the orchestrator when omitted, never required
    #: here (a direct/future non-GUI caller should not be forced to
    #: invent one).
    source: Optional[str] = None


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

    CD-6 reliability delta: the call is offloaded via
    ``run_in_threadpool`` (module docstring) so this coroutine's own
    ``await`` here is the event loop's one cooperative yield point for
    the whole request — everything else in this handler is unchanged.
    """
    composition = get_composition()
    result = await run_in_threadpool(
        handle_operator_message,
        message=payload.message,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        correlation_id=payload.correlation_id,
        evidence_id=payload.evidence_id,
        intake_id=payload.intake_id,
        entity_id=payload.entity_id,
        conversation_id=payload.conversation_id,
        source=payload.source,
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
