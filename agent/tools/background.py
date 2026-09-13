"""The ``run_background_analysis`` tool's dispatch seam (CD-5 PID §35,
WI-3) — explicitly NOT this WI's job to implement for real (that is
WI-2, running in parallel and not visible to this worktree).

Why a structural ``Protocol``, not a concrete import
------------------------------------------------------
Mirrors exactly the pattern
``services.evidence.intake.validation_pipeline`` already established
for its own ``_ObjectStore`` Protocol (see that module's docstring):
rather than importing WI-2's real LiteLLM-backed orchestration function
(which does not exist in this worktree, and would create a hard,
premature coupling even once it does), this module declares the
narrowest possible structural contract for "the thing that actually
dispatches a BACKGROUND AI task" — :class:`BackgroundTaskRunner`. Any
callable object exposing a matching ``run_background_task`` method
satisfies this ``Protocol`` with zero inheritance and zero import in
either direction (duck typing).

The PL's reconciliation seam
-----------------------------
``app/api/composition.py`` (both branches) currently wires
:class:`DeterministicFakeBackgroundTaskRunner` below as the
``background_task_runner`` dependency for ``agent.tools``'
``run_background_analysis`` tool — in BOTH development/test AND
production composition, since WI-2's real gateway function does not
exist in this worktree at all. During reconciliation, the PL should
replace `DeterministicFakeBackgroundTaskRunner` in **production**
composition with whatever concrete object WI-2 actually delivers
(e.g. a thin adapter wrapping WI-2's real dispatch function) — the
only requirement is that it satisfies :class:`BackgroundTaskRunner`'s
one method. Development/test composition should very likely keep using
this deterministic fake regardless (PID §61 — ordinary tests must not
depend on a live LiteLLM/Mac-mini/Trinity call).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from ai.invocation import AIInvocation, AIInvocationRepository
from ai.tasks import get_task_contract, validate_task_output
from core.errors import ValidationError


class BackgroundTaskRunner(Protocol):
    """The one capability ``run_background_analysis`` needs: dispatch a
    registered ``BACKGROUND`` AI task and return the resulting,
    terminal :class:`ai.invocation.AIInvocation`. Structurally satisfied
    by :class:`DeterministicFakeBackgroundTaskRunner` below today, and
    by WI-2's real gateway-backed implementation once the PL wires it
    in — see module docstring.
    """

    def run_background_task(
        self,
        *,
        task_id: str,
        task_version: int,
        input_references: Mapping[str, Any],
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str],
    ) -> AIInvocation: ...


def _canned_output_for(task_id: str) -> dict:
    """A small, fixed, structurally-valid-per-task canned output —
    deliberately never phrased to look like a plausible real model
    answer (PID §61: a stub must never be mistaken for a genuine
    inference result)."""
    _stub_warning = [
        "stub background runner (WI-2's real LiteLLM gateway is not yet wired in "
        "this composition) — this is NOT a real model result"
    ]
    if task_id == "DOCUMENT_TYPE_PROPOSAL":
        return {
            "proposed_type": "UNKNOWN",
            "confidence": 0.0,
            "signals": [],
            "warnings": _stub_warning,
        }
    if task_id == "DOCUMENT_SUMMARY":
        return {
            "summary": "(stub) background summarisation is not yet wired to a real model.",
            "confidence": 0.0,
            "signals": [],
            "warnings": _stub_warning,
        }
    if task_id == "ENTITY_PROPOSAL":
        return {
            "proposed_entity_hint": None,
            "confidence": 0.0,
            "signals": [],
            "warnings": _stub_warning,
        }
    raise ValidationError(
        f"DeterministicFakeBackgroundTaskRunner has no canned output shape for task '{task_id}'"
    )


class DeterministicFakeBackgroundTaskRunner:
    """Deterministic stand-in for WI-2's real gateway (PID §61) — drives
    a genuine ``AIInvocation`` through the SAME ``ai.invocation``/
    ``ai.tasks`` machinery a real background gateway would use
    (``REQUESTED -> RUNNING -> SUCCEEDED``/``FAILED``), with a small,
    fixed, structurally-valid canned output instead of a real provider
    call. This makes ``run_background_analysis`` genuinely testable and
    demoable end-to-end today, without pretending to be a real
    inference result (every canned output's ``warnings`` says so
    explicitly).
    """

    def __init__(self, repository: AIInvocationRepository) -> None:
        self._repository = repository

    def run_background_task(
        self,
        *,
        task_id: str,
        task_version: int,
        input_references: Mapping[str, Any],
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str],
    ) -> AIInvocation:
        contract = get_task_contract(task_id, task_version)  # raises NotFoundError if unregistered
        if contract.role != "BACKGROUND":
            raise ValidationError(
                f"'{task_id}' v{task_version} is role={contract.role!r}, not BACKGROUND — "
                "run_background_analysis only dispatches BACKGROUND tasks"
            )

        invocation = self._repository.create_invocation(
            task_id=task_id,
            task_version=task_version,
            role="BACKGROUND",
            provider="LITELLM",
            capability_alias=contract.preferred_capability,
            input_references=input_references,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        invocation = self._repository.transition_status(invocation.ai_invocation_id, "RUNNING")

        output = _canned_output_for(task_id)
        validation_result = validate_task_output(contract, output)
        if not validation_result.valid:  # pragma: no cover - defensive; canned outputs are hand-verified
            return self._repository.transition_status(
                invocation.ai_invocation_id,
                "FAILED",
                output=output,
                validation_result=validation_result.to_dict(),
                error_code="OUTPUT_SCHEMA_VALIDATION_FAILED",
            )
        return self._repository.transition_status(
            invocation.ai_invocation_id,
            "SUCCEEDED",
            output=output,
            validation_result=validation_result.to_dict(),
            confidence=output.get("confidence"),
            provider_model="bagman-fast-stub-v1 (WI-3 deterministic stand-in for WI-2's gateway)",
        )
