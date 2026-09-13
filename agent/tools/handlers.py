"""The 8 read-only/analyse-only BAGMAN tools Claude may invoke (CD-5
PID §34, WI-3).

Every handler calls straight into BAGMAN's own existing internal
APIs/repositories (``core.api.BagmanCanonicalAPI``, the intake/AI-
invocation repositories) — exactly like ``app/api/routers/*.py``
already do — never an HTTP self-call to BAGMAN's own API. No handler
here ever calls a canonical *write* method
(``register_entity``/``register_source``/``register_evidence``/
``link_external_reference``/``record_provenance``/evidence status
mutation); ``tests/security/test_agent_tool_registry_safety.py``
mechanically proves this by inspecting the actual objects each
handler's closure captures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from agent.tools.background import BackgroundTaskRunner
from agent.tools.registry import ToolExecutionContext, ToolRegistry, ToolSpec
from ai.invocation import AIInvocationRepository
from core.api import BagmanCanonicalAPI
from services.evidence.intake.intake import IntakeRepository

_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 100


def _bounded_limit(raw: Optional[int]) -> int:
    if raw is None:
        return _DEFAULT_LIST_LIMIT
    return max(1, min(int(raw), _MAX_LIST_LIMIT))


@dataclass(frozen=True)
class ToolDependencies:
    """Everything :func:`build_default_tool_registry` needs, wired by
    the caller (ordinarily ``app/api/composition.py``) — deliberately
    plain, concrete objects rather than a whole ``RuntimeComposition``,
    so ``agent/tools`` never depends on the ``app/api`` layer (avoids
    an upward/circular dependency: ``app/api`` depends on ``agent/``,
    never the reverse)."""

    api: BagmanCanonicalAPI
    intake_repository: IntakeRepository
    ai_invocation_repository: AIInvocationRepository
    background_task_runner: BackgroundTaskRunner
    #: Zero-arg callable returning a plain, JSON-able runtime status
    #: dict (PID §46-48) — see ``app/api/composition.get_runtime_status_summary``
    #: for the real production/dev-mode implementation this is bound to.
    runtime_health_check: Callable[[], Mapping[str, Any]]


# ---------------------------------------------------------------------
# input schemas (PID §62 — each tool declares its own JSON Schema
# input contract; Claude sees these via ToolRegistry.list_tool_definitions())
# ---------------------------------------------------------------------

_EMPTY_OBJECT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_LIST_DOCUMENTS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {"type": "string", "minLength": 1},
        "evidence_type": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT},
        "offset": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

_GET_DOCUMENT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"evidence_id": {"type": "string", "minLength": 1}},
    "required": ["evidence_id"],
    "additionalProperties": False,
}

_GET_INTAKE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"intake_id": {"type": "string", "minLength": 1}},
    "required": ["intake_id"],
    "additionalProperties": False,
}

_TRACE_PROVENANCE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "subject_type": {"type": "string", "minLength": 1},
        "subject_id": {"type": "string", "minLength": 1},
    },
    "required": ["subject_type", "subject_id"],
    "additionalProperties": False,
}

_LIST_AI_INVOCATIONS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "role": {"type": "string", "enum": ["OPERATOR", "BACKGROUND"]},
        "status": {
            "type": "string",
            "enum": ["REQUESTED", "RUNNING", "SUCCEEDED", "FAILED", "REJECTED"],
        },
        "correlation_id": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT},
        "offset": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

_GET_AI_INVOCATION_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"ai_invocation_id": {"type": "string", "minLength": 1}},
    "required": ["ai_invocation_id"],
    "additionalProperties": False,
}

_RUN_BACKGROUND_ANALYSIS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "pattern": "^[A-Z][A-Z_]*$",
            "description": "A registered BACKGROUND task_id, e.g. DOCUMENT_TYPE_PROPOSAL.",
        },
        "task_version": {"type": "integer", "minimum": 1},
        "input_references": {
            "type": "object",
            "description": "e.g. {\"evidence_id\": \"...\"} — the canonical subject this "
            "background task reasons about.",
        },
    },
    "required": ["task_id", "input_references"],
    "additionalProperties": False,
}


def build_default_tool_registry(deps: ToolDependencies) -> ToolRegistry:
    """Build the fixed, closed 8-tool registry (PID §34, WI-3) — the
    exact set named in the PID, no more, no less.
    """

    def get_runtime_status(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        return dict(deps.runtime_health_check())

    def list_documents(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        items = deps.api.evidence_repository.list_evidence(
            entity_id=tool_input.get("entity_id"),
            evidence_type=tool_input.get("evidence_type"),
            limit=_bounded_limit(tool_input.get("limit")),
            offset=int(tool_input.get("offset") or 0),
        )
        return {"items": [item.to_dict() for item in items], "count": len(items)}

    def get_document(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        return deps.api.get_evidence(tool_input["evidence_id"]).to_dict()

    def get_intake(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        return deps.intake_repository.get_intake_record(tool_input["intake_id"]).to_dict()

    def trace_provenance(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        lineage = deps.api.trace_provenance(
            subject_type=tool_input["subject_type"], subject_id=tool_input["subject_id"]
        )
        return {
            "lineage": [
                {
                    "provenance": edge["provenance"].to_dict(),
                    "evidence": edge["evidence"].to_dict(),
                    "source": edge["source"].to_dict(),
                }
                for edge in lineage
            ]
        }

    def list_ai_invocations(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        invocations = deps.ai_invocation_repository.list_invocations(
            task_id=tool_input.get("task_id"),
            role=tool_input.get("role"),
            status=tool_input.get("status"),
            correlation_id=tool_input.get("correlation_id"),
            limit=_bounded_limit(tool_input.get("limit")),
            offset=int(tool_input.get("offset") or 0),
        )
        return {"items": [inv.to_dict() for inv in invocations], "count": len(invocations)}

    def get_ai_invocation(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        return deps.ai_invocation_repository.get_invocation(tool_input["ai_invocation_id"]).to_dict()

    def run_background_analysis(
        tool_input: Mapping[str, Any], context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        invocation = deps.background_task_runner.run_background_task(
            task_id=tool_input["task_id"],
            task_version=int(tool_input.get("task_version") or 1),
            input_references=tool_input["input_references"],
            actor_type=context.actor_type,
            actor_id=context.actor_id,
            correlation_id=context.correlation_id,
        )
        return invocation.to_dict()

    specs = [
        ToolSpec(
            name="get_runtime_status",
            description=(
                "Get a structured summary of BAGMAN's current runtime health: runtime "
                "environment, and per-dependency status (postgres, object_store, scanner, "
                "claude_operator). Read-only, no arguments."
            ),
            input_schema=_EMPTY_OBJECT_SCHEMA,
            authority_class="READ",
            side_effects="none — reads live dependency-health status only",
            handler=get_runtime_status,
        ),
        ToolSpec(
            name="list_documents",
            description=(
                "List canonical BAGMAN EvidenceItems, most-recently-received first. Optional "
                "filters: entity_id, evidence_type, limit (default 20, max 100), offset."
            ),
            input_schema=_LIST_DOCUMENTS_SCHEMA,
            authority_class="READ",
            side_effects="none — read-only evidence listing",
            handler=list_documents,
        ),
        ToolSpec(
            name="get_document",
            description="Get one canonical EvidenceItem's full metadata by evidence_id.",
            input_schema=_GET_DOCUMENT_SCHEMA,
            authority_class="READ",
            side_effects="none — read-only",
            handler=get_document,
        ),
        ToolSpec(
            name="get_intake",
            description="Get one IntakeRecord's full state/history by intake_id.",
            input_schema=_GET_INTAKE_SCHEMA,
            authority_class="READ",
            side_effects="none — read-only",
            handler=get_intake,
        ),
        ToolSpec(
            name="trace_provenance",
            description=(
                "Trace the full evidence lineage recorded for a canonical subject "
                "(subject_type, subject_id) — every Provenance edge, resolved to its "
                "EvidenceItem and that evidence's Source."
            ),
            input_schema=_TRACE_PROVENANCE_SCHEMA,
            authority_class="READ",
            side_effects="none — read-only",
            handler=trace_provenance,
        ),
        ToolSpec(
            name="list_ai_invocations",
            description=(
                "List AIInvocation records (BAGMAN's own AI-task audit trail), "
                "most-recently-started first. Optional filters: task_id, role "
                "(OPERATOR/BACKGROUND), status, correlation_id, limit (default 20, max 100), offset."
            ),
            input_schema=_LIST_AI_INVOCATIONS_SCHEMA,
            authority_class="READ",
            side_effects="none — read-only",
            handler=list_ai_invocations,
        ),
        ToolSpec(
            name="get_ai_invocation",
            description="Get one AIInvocation's full record by ai_invocation_id.",
            input_schema=_GET_AI_INVOCATION_SCHEMA,
            authority_class="READ",
            side_effects="none — read-only",
            handler=get_ai_invocation,
        ),
        ToolSpec(
            name="run_background_analysis",
            description=(
                "Request a governed BAGMAN background AI task (e.g. DOCUMENT_TYPE_PROPOSAL, "
                "DOCUMENT_SUMMARY, ENTITY_PROPOSAL) against a canonical subject "
                "(input_references, e.g. {\"evidence_id\": \"...\"}). Returns the resulting "
                "AIInvocation — a structured PROPOSAL, never a canonical write. Only "
                "task_ids registered with role=BACKGROUND may be requested."
            ),
            input_schema=_RUN_BACKGROUND_ANALYSIS_SCHEMA,
            authority_class="ANALYSE",
            side_effects=(
                "creates a new AIInvocation and dispatches one governed background task "
                "attempt; never writes canonical evidence/entity/accounting state"
            ),
            handler=run_background_analysis,
        ),
    ]
    return ToolRegistry(specs)
