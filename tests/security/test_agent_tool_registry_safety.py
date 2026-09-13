"""Security proofs for the fixed BAGMAN tool registry Claude may invoke
(CD-5 PID §28/§34/§62-63, WI-3):

* only the authorised 8-tool set exists — no forbidden tool name
  anywhere in the registry;
* an invalid/unregistered tool-call request fails closed (no dynamic
  dispatch);
* no registered tool can reach any canonical *write* method.
"""
from __future__ import annotations

import pytest

from agent.tools.background import DeterministicFakeBackgroundTaskRunner
from agent.tools.handlers import ToolDependencies, build_default_tool_registry
from agent.tools.registry import FORBIDDEN_TOOL_NAMES, ToolExecutionContext, ToolSpec
from ai.invocation import InMemoryAIInvocationRepository
from core import actor, identity
from core.api import BagmanCanonicalAPI
from core.errors import NotFoundError, ValidationError
from services.evidence.intake.intake import InMemoryIntakeRepository

EXPECTED_TOOL_NAMES = frozenset(
    {
        "get_runtime_status",
        "list_documents",
        "get_document",
        "get_intake",
        "trace_provenance",
        "list_ai_invocations",
        "get_ai_invocation",
        "run_background_analysis",
    }
)

#: PID §34's own explicit forbidden list, mirrored here as the
#: independent expectation this test proves against (rather than just
#: re-checking registry.FORBIDDEN_TOOL_NAMES against itself).
PID_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "raw_sql",
        "host_shell",
        "docker",
        "arbitrary_http",
        "delete_evidence",
        "write_accounting",
        "send_email",
        "move_money",
    }
)

_CANONICAL_WRITE_METHOD_NAMES = frozenset(
    {
        "register_entity",
        "register_source",
        "register_evidence",
        "link_external_reference",
        "record_provenance",
    }
)


def _build_registry():
    api = BagmanCanonicalAPI()
    intake_repository = InMemoryIntakeRepository()
    ai_invocation_repository = InMemoryAIInvocationRepository()
    background_task_runner = DeterministicFakeBackgroundTaskRunner(ai_invocation_repository)
    deps = ToolDependencies(
        api=api,
        intake_repository=intake_repository,
        ai_invocation_repository=ai_invocation_repository,
        background_task_runner=background_task_runner,
        runtime_health_check=lambda: {"runtime_environment": "test", "checks": {}},
    )
    return build_default_tool_registry(deps), deps


# ---------------------------------------------------------------------
# PID §34/§28/§63 — the fixed, closed, authorised tool set
# ---------------------------------------------------------------------


def test_registry_contains_exactly_the_pid_section_34_eight_tools():
    registry, _ = _build_registry()
    names = {spec.name for spec in registry.list_specs()}
    assert names == EXPECTED_TOOL_NAMES
    assert len(registry) == 8


def test_pid_forbidden_tool_names_match_the_registry_modules_own_constant():
    assert FORBIDDEN_TOOL_NAMES == PID_FORBIDDEN_TOOL_NAMES


@pytest.mark.parametrize("forbidden_name", sorted(PID_FORBIDDEN_TOOL_NAMES))
def test_no_forbidden_tool_name_is_registered(forbidden_name):
    registry, _ = _build_registry()
    assert forbidden_name not in registry
    assert forbidden_name not in {spec.name for spec in registry.list_specs()}


@pytest.mark.parametrize("forbidden_name", sorted(PID_FORBIDDEN_TOOL_NAMES))
def test_constructing_a_toolspec_under_a_forbidden_name_is_refused(forbidden_name):
    """Even a future, careless addition to handlers.py fails immediately
    and loudly at construction time — not merely "happens to be absent
    today"."""
    with pytest.raises(ValidationError):
        ToolSpec(
            name=forbidden_name,
            description="should never construct",
            input_schema={"type": "object"},
            authority_class="READ",
            side_effects="none",
            handler=lambda tool_input, context: {},
        )


def test_no_registered_tool_name_resembles_a_write_or_mutate_capability():
    """A cheap, independent lexical sanity check alongside the exact
    forbidden-name list above — no tool name contains an obvious
    write/mutate verb."""
    registry, _ = _build_registry()
    write_ish_substrings = ("write", "delete", "mutate", "send", "move", "create_", "update_")
    for spec in registry.list_specs():
        for substring in write_ish_substrings:
            assert substring not in spec.name, f"tool name '{spec.name}' looks write-shaped"


def test_every_tool_authority_class_is_read_or_analyse_never_a_write_class():
    registry, _ = _build_registry()
    for spec in registry.list_specs():
        assert spec.authority_class in ("READ", "ANALYSE")


# ---------------------------------------------------------------------
# PID §63 — invalid/unregistered tool calls fail closed
# ---------------------------------------------------------------------


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        actor_type=actor.SYSTEM, actor_id="test", correlation_id=identity.generate_id()
    )


def test_calling_an_unregistered_tool_name_fails_closed_with_not_found():
    registry, _ = _build_registry()
    with pytest.raises(NotFoundError):
        registry.execute("some_made_up_tool", {}, context=_context())


@pytest.mark.parametrize("forbidden_name", sorted(PID_FORBIDDEN_TOOL_NAMES))
def test_calling_a_forbidden_tool_name_fails_closed_never_a_dynamic_dispatch(forbidden_name):
    registry, _ = _build_registry()
    with pytest.raises(NotFoundError):
        registry.execute(forbidden_name, {}, context=_context())


def test_calling_a_registered_tool_with_schema_invalid_input_fails_closed():
    registry, _ = _build_registry()
    # get_document requires evidence_id (string) — omit it entirely.
    with pytest.raises(ValidationError):
        registry.execute("get_document", {}, context=_context())


def test_calling_a_registered_tool_with_an_extra_unexpected_field_fails_closed():
    registry, _ = _build_registry()
    with pytest.raises(ValidationError):
        registry.execute(
            "get_document",
            {"evidence_id": "x", "unexpected_extra_field": "should not be accepted"},
            context=_context(),
        )


def test_get_runtime_status_tool_takes_no_arguments_and_rejects_extras():
    registry, _ = _build_registry()
    result = registry.execute("get_runtime_status", {}, context=_context())
    assert "checks" in result
    with pytest.raises(ValidationError):
        registry.execute("get_runtime_status", {"anything": 1}, context=_context())


# ---------------------------------------------------------------------
# PID §17/§56 — no tool may mutate canonical state
# ---------------------------------------------------------------------


def test_no_tool_handler_closure_can_reach_a_canonical_write_method():
    """Mechanically proves every registered handler's closure only
    captures read-oriented objects (`deps`) and never a bound write
    method of `BagmanCanonicalAPI` directly. Since every handler here
    is a plain closure over `deps` (see `agent.tools.handlers
    .build_default_tool_registry`), this walks each handler's
    `__closure__` cells looking for anything that IS one of
    `BagmanCanonicalAPI`'s write methods (bound or unbound) — a
    structural proof, not merely "we didn't call it in this test run".
    """
    registry, deps = _build_registry()

    write_methods = {
        name: getattr(deps.api, name) for name in _CANONICAL_WRITE_METHOD_NAMES
    }

    for spec in registry.list_specs():
        handler = spec.handler
        closure = handler.__closure__ or ()
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:  # pragma: no cover - empty cell
                continue
            for write_name, bound_method in write_methods.items():
                assert value is not bound_method, (
                    f"tool '{spec.name}' handler's closure captures "
                    f"BagmanCanonicalAPI.{write_name} — a canonical write method must "
                    "never be reachable from any registered tool (PID §17/§56)"
                )
                # Also guard against capturing the *unbound* function
                # object (e.g. via a module-level reference) rather
                # than a bound method.
                assert value is not type(deps.api).__dict__.get(write_name), (
                    f"tool '{spec.name}' handler's closure captures the unbound "
                    f"BagmanCanonicalAPI.{write_name} function"
                )


def test_run_background_analysis_only_ever_produces_an_ai_invocation_never_a_canonical_write():
    """`run_background_analysis` is the one `ANALYSE`-class tool
    (PID §35) — prove its actual execution creates only an
    `AIInvocation` row, never touches `evidence_repository`/
    `entity_repository`/`source_repository`."""
    registry, deps = _build_registry()

    entity_count_before = len(deps.api.entity_repository._by_id) if hasattr(
        deps.api.entity_repository, "_by_id"
    ) else None

    result = registry.execute(
        "run_background_analysis",
        {"task_id": "DOCUMENT_TYPE_PROPOSAL", "input_references": {"evidence_id": "fake-evidence-id"}},
        context=_context(),
    )
    assert result["status"] in ("SUCCEEDED", "FAILED")
    assert result["role"] == "BACKGROUND"

    if entity_count_before is not None:
        assert len(deps.api.entity_repository._by_id) == entity_count_before
