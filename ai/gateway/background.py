"""``run_background_task`` — the BACKGROUND-role task orchestration
function (CD-5 PID §6/§21-30/§57-58/§73-76, WI-2).

Turns one validated task request into a fully-lived ``AIInvocation``
lifecycle against the ``LITELLM`` provider adapter
(``ai.providers.litellm``): resolve the task contract, create the
durable invocation record (``REQUESTED``), transition to ``RUNNING``,
call the provider with the task's own versioned prompt, parse/validate
the structured output, and transition to a terminal state
(``SUCCEEDED``/``FAILED``) — emitting the PID §57 audit events at every
step, correlation/causation-chained exactly like
``app/api/routers/intake.py``'s own governed intake pipeline (that
module's own docstring/`_emit` helper is this module's closest and best
precedent).

Dependency-injected, not composition-coupled
----------------------------------------------
This function takes `repository` (an `ai.invocation.AIInvocationRepository`),
`litellm_client` (an `ai.providers.litellm.client.LiteLLMClientProtocol`),
and `record_audit_event` (a plain callable with EXACTLY
`core.api.BagmanCanonicalAPI.record_audit_event`'s keyword signature) as
parameters, rather than importing `app.api.composition` itself. This
keeps `ai/gateway/` free of any dependency on the HTTP/composition
layer (mirroring `core`/`services` never importing `app`/`persistence`
directly — see `tests/integration/test_architecture_boundaries.py`) and
makes every code path here exercisable in a plain unit test against
`ai.invocation.InMemoryAIInvocationRepository`,
`ai.providers.litellm.fake.FakeLiteLLMClient`, and a bare
`core.api.BagmanCanonicalAPI()`'s own `record_audit_event` — no HTTP,
no database, no live LLM (PID §61).

Evidence-content resolution is deliberately OUT of this function's own
scope
------------------------------------------------------------------------
This WI's own dispatch is explicit that real document-content
extraction (e.g. PDF text extraction) does not exist anywhere in
BAGMAN yet, and building it is not this WI's job. `evidence_content`
is therefore accepted here as an already-resolved plain-text string —
`app/api/routers/ai.py`'s HTTP handler is the one place that resolves
it (by reading the referenced `EvidenceItem`'s stored bytes and
decoding them as UTF-8, best-effort) before calling this function. A
plain-text evidence body is judged a reasonable, honestly-scoped
starting point for CD-5's first vertical slice (PID §36) — a
binary/non-text document will decode lossily rather than being
meaningfully understood, which is an accepted, documented limitation,
not a defect this WI silently papered over.

Four outcomes (see this module's own dispatch for the exact contract)
------------------------------------------------------------------------
1. **Success** — provider responds OK, content parses as JSON, output
   validates against the task's `output_schema` -> `SUCCEEDED`, with
   `output`/`confidence`/`validation_result`/`provider_model`/
   `usage_metadata`/`latency_ms`/`prompt_contract_version` all
   populated; one `AI_INVOCATION_SUCCEEDED` audit event.
2. **Malformed output** (not valid JSON at all, or valid JSON that
   fails `output_schema`) -> `FAILED`, with `validation_result`
   recording exactly what was wrong (PID §76 — never silently
   repaired) and a distinct `error_code`; one `AI_OUTPUT_REJECTED`
   audit event (causation-chained to the request) followed by one
   `AI_INVOCATION_FAILED` event (causation-chained to the rejection) —
   the non-conforming output itself is preserved on the invocation
   where it parsed to an object, per PID §76's "retained for audit, not
   discarded".
3. **Transport/timeout/auth/provider failure** (the LiteLLM client
   itself reports a non-OK `LiteLLMOutcomeStatus`) -> `FAILED` directly
   (never through the malformed-output path), with a
   `LITELLM_<STATUS>`-shaped `error_code`; one `AI_INVOCATION_FAILED`
   audit event.
4. **Concurrency conflict** — `repository.create_invocation` raises
   `core.errors.ActiveInvocationConflictError` before any invocation
   row is created at all; this function does not catch it — it
   propagates to the caller (`app/api/routers/ai.py` maps it to HTTP
   409), exactly per PID §73's "one active invocation per subject"
   concurrency guard, already fully implemented by WI-1.

No retry loop lives inside this function (PID §74): a "retry" is
simply calling `run_background_task` again once the prior attempt has
reached a terminal state — WI-1's own concurrency guard already
permits that cleanly and creates a genuinely new, distinct
`AIInvocation` row.

Structured-output request (CD-5 Gate-1 closure delta, 2026-09-16)
--------------------------------------------------------------------
This function passes `task_contract.output_schema` to
`litellm_client.complete()` as a per-request generation constraint
(see `ai.providers.litellm.client`'s own module docstring) — a
reliability improvement for real-provider completions found necessary
during Gate-1 acceptance (real appliance responses were sometimes
empty or malformed under plain "JSON mode"). This changes NOTHING
about outcome 2a/2b above: `json.loads` + `validate_task_output`
against that exact same schema remain mandatory and unconditional — a
provider claiming/attempting structured-output support is never
treated as sufficient proof of a conforming response.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from ai.invocation import AIInvocation, AIInvocationRepository
from ai.prompts.loader import load_system_prompt, resolve_prompt_contract_version
from ai.providers.litellm.client import LiteLLMClientProtocol, LiteLLMOutcomeStatus
from ai.tasks import ValidationResult, get_task_contract, validate_task_output
from core.contract_validation import describe_schema_errors
from core.errors import ValidationError

#: The exact keyword shape `core.api.BagmanCanonicalAPI.record_audit_event`
#: exposes — documented here (rather than imported, to avoid a
#: gateway -> app/api dependency) purely so a reader knows what a
#: caller must pass.
RecordAuditEvent = Callable[..., Any]


def run_background_task(
    *,
    task_id: str,
    task_version: int,
    input_references: Mapping[str, Any],
    evidence_content: str,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str],
    repository: AIInvocationRepository,
    litellm_client: LiteLLMClientProtocol,
    record_audit_event: RecordAuditEvent,
) -> AIInvocation:
    """Run one BACKGROUND task attempt end-to-end. See module docstring
    for the full four-outcome contract.

    Raises:
        core.errors.ValidationError: `task_id`/`task_version` names an
            `OPERATOR`-role task (this function only serves
            `BACKGROUND`), or `input_references` fails the task's own
            `input_schema`.
        core.errors.NotFoundError: `task_id`/`task_version` is not
            registered at all.
        core.errors.ActiveInvocationConflictError: a non-terminal
            invocation already exists for this exact subject (PID §73)
            — propagated uncaught; see module docstring outcome 4.
    """
    task_contract = get_task_contract(task_id, task_version)
    if task_contract.role != "BACKGROUND":
        raise ValidationError(
            f"run_background_task only serves BACKGROUND-role tasks (PID §6/§8); "
            f"task '{task_id}' v{task_version} is role={task_contract.role!r} — an OPERATOR task "
            "must be routed through the Claude operator gateway instead"
        )

    input_errors = describe_schema_errors(dict(input_references), dict(task_contract.input_schema))
    if input_errors:
        raise ValidationError(
            f"input_references for task '{task_id}' v{task_version} failed its own input_schema "
            f"(PID §22): {'; '.join(input_errors)}"
        )

    # core.errors.ActiveInvocationConflictError propagates uncaught here
    # — see module docstring outcome 4. Nothing is audited for a
    # rejected-before-creation conflict; there is no AIInvocation row to
    # attach an audit event to.
    invocation = repository.create_invocation(
        task_id=task_id,
        task_version=task_version,
        role="BACKGROUND",
        provider="LITELLM",
        capability_alias=task_contract.preferred_capability,
        input_references=input_references,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )

    requested_event = record_audit_event(
        event_type="AI_INVOCATION_REQUESTED",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="AIInvocation",
        subject_id=invocation.ai_invocation_id,
        correlation_id=invocation.correlation_id,
        causation_id=None,
        payload={
            "task_id": task_id,
            "task_version": task_version,
            "capability_alias": invocation.capability_alias,
        },
    )
    last_event_id = requested_event.audit_event_id

    invocation = repository.transition_status(invocation.ai_invocation_id, "RUNNING")

    prompt_contract_version = resolve_prompt_contract_version(task_id)
    system_instructions = load_system_prompt(task_id, prompt_contract_version)

    result = litellm_client.complete(
        capability_alias=invocation.capability_alias,
        system_instructions=system_instructions,
        evidence_content=evidence_content,
        # CD-5 Gate-1 closure delta: the task contract this function
        # already resolved above is the one and only schema authority
        # (PID §21-22) — passed straight through, never
        # hand-reconstructed, so the provider-side generation
        # constraint and the post-response validate_task_output call
        # below are always checking the SAME schema.
        output_schema=task_contract.output_schema,
        timeout_seconds=float(task_contract.timeout_seconds),
    )

    # -- outcome 3: transport/timeout/auth/provider failure --------------
    if result.status != LiteLLMOutcomeStatus.OK:
        error_code = f"LITELLM_{result.status.value}"
        invocation = repository.transition_status(
            invocation.ai_invocation_id,
            "FAILED",
            error_code=error_code,
            provider_model=result.provider_model,
            usage_metadata=dict(result.usage_metadata),
            latency_ms=result.latency_ms,
        )
        record_audit_event(
            event_type="AI_INVOCATION_FAILED",
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="AIInvocation",
            subject_id=invocation.ai_invocation_id,
            correlation_id=invocation.correlation_id,
            causation_id=last_event_id,
            payload={"error_code": error_code, "detail": (result.error_detail or "")[:500]},
        )
        return invocation

    # -- outcome 2a: not even valid JSON ---------------------------------
    try:
        parsed_output = json.loads(result.content or "")
    except (json.JSONDecodeError, TypeError) as exc:
        return _reject_output(
            invocation=invocation,
            repository=repository,
            record_audit_event=record_audit_event,
            actor_type=actor_type,
            actor_id=actor_id,
            last_event_id=last_event_id,
            error_code="OUTPUT_NOT_JSON",
            validation_result=ValidationResult(valid=False, errors=(f"output was not valid JSON: {exc}",)),
            output=None,
            result=result,
            prompt_contract_version=prompt_contract_version,
        )

    # -- outcome 2b: valid JSON, fails output_schema ---------------------
    validation_result = validate_task_output(task_contract, parsed_output)
    if not validation_result.valid:
        return _reject_output(
            invocation=invocation,
            repository=repository,
            record_audit_event=record_audit_event,
            actor_type=actor_type,
            actor_id=actor_id,
            last_event_id=last_event_id,
            error_code="OUTPUT_SCHEMA_INVALID",
            validation_result=validation_result,
            # PID §76 — preserve the raw non-conforming output for
            # audit rather than discarding it, when it is at least
            # shaped as an object (AIInvocation.output must serialise
            # via dict(...) — see ai.invocation.AIInvocation.to_dict()).
            output=parsed_output if isinstance(parsed_output, dict) else {"raw_output": parsed_output},
            result=result,
            prompt_contract_version=prompt_contract_version,
        )

    # -- outcome 1: success -----------------------------------------------
    confidence = parsed_output.get("confidence") if isinstance(parsed_output, dict) else None
    invocation = repository.transition_status(
        invocation.ai_invocation_id,
        "SUCCEEDED",
        output=parsed_output,
        confidence=confidence,
        validation_result=validation_result.to_dict(),
        provider_model=result.provider_model,
        usage_metadata=dict(result.usage_metadata),
        latency_ms=result.latency_ms,
        prompt_contract_version=prompt_contract_version,
    )
    record_audit_event(
        event_type="AI_INVOCATION_SUCCEEDED",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="AIInvocation",
        subject_id=invocation.ai_invocation_id,
        correlation_id=invocation.correlation_id,
        causation_id=last_event_id,
        payload={"task_id": task_id, "confidence": confidence},
    )
    return invocation


def _reject_output(
    *,
    invocation: AIInvocation,
    repository: AIInvocationRepository,
    record_audit_event: RecordAuditEvent,
    actor_type: str,
    actor_id: str,
    last_event_id: str,
    error_code: str,
    validation_result: ValidationResult,
    output: Optional[dict],
    result: Any,
    prompt_contract_version: str,
) -> AIInvocation:
    """Shared `RUNNING -> FAILED` handling for both malformed-output
    sub-cases (outcome 2a/2b) — one `AI_OUTPUT_REJECTED` event
    (causation-chained to the request), then one `AI_INVOCATION_FAILED`
    event (causation-chained to the rejection), matching PID §57's
    named vocabulary exactly.
    """
    field_updates: dict[str, Any] = {
        "error_code": error_code,
        "validation_result": validation_result.to_dict(),
        "provider_model": result.provider_model,
        "usage_metadata": dict(result.usage_metadata),
        "latency_ms": result.latency_ms,
        "prompt_contract_version": prompt_contract_version,
    }
    if output is not None:
        field_updates["output"] = output

    updated = repository.transition_status(invocation.ai_invocation_id, "FAILED", **field_updates)

    rejected_event = record_audit_event(
        event_type="AI_OUTPUT_REJECTED",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="AIInvocation",
        subject_id=updated.ai_invocation_id,
        correlation_id=updated.correlation_id,
        causation_id=last_event_id,
        payload={"error_code": error_code, "errors": list(validation_result.errors)[:10]},
    )
    record_audit_event(
        event_type="AI_INVOCATION_FAILED",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="AIInvocation",
        subject_id=updated.ai_invocation_id,
        correlation_id=updated.correlation_id,
        causation_id=rejected_event.audit_event_id,
        payload={"error_code": error_code},
    )
    return updated
