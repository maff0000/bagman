"""The fixed, read-only BAGMAN tool registry Claude may invoke (CD-5
PID §33-35/§62-63, WI-3).

This module owns the MECHANISM — a closed mapping of tool name ->
:class:`ToolSpec`, with mechanical fail-closed dispatch (PID §63): an
unregistered/invalid tool name is looked up in this fixed mapping ONLY
and raises immediately if absent, never falling through to any
dynamic/reflective lookup against BAGMAN internals. ``agent/tools/handlers.py``
owns the CONTENT — the actual 8 tool definitions/handlers.

Authority classes (PID §62)
-----------------------------
Every :class:`ToolSpec` declares an ``authority_class`` of either
``"READ"`` (a pure read of existing canonical/AI-invocation state) or
``"ANALYSE"`` (``run_background_analysis`` — requests a governed
background PROPOSAL; it never itself writes canonical state). No third
value exists, and no CD-5 tool may declare anything resembling a write/
mutate authority class — see ``FORBIDDEN_TOOL_NAMES`` below and
``tests/security/test_agent_tool_registry_safety.py`` for the
mechanical proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ai.providers.claude.client import ToolDefinition
from core.contract_validation import describe_schema_errors
from core.errors import NotFoundError, ValidationError

#: Tool names PID §28/§34/§63 explicitly forbid Claude from ever having
#: — asserted absent by a dedicated security test (never merely "not
#: present by omission"). ``ToolSpec.__post_init__`` also refuses to
#: construct a spec under one of these names, so even a future/careless
#: addition to ``handlers.py`` fails immediately and loudly at import
#: time, not silently at test time alone.
FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
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

#: The only authority classes a CD-5 tool may declare (PID §62) — never
#: a write/mutate class.
AUTHORITY_CLASSES: frozenset[str] = frozenset({"READ", "ANALYSE"})


@dataclass(frozen=True)
class ToolExecutionContext:
    """Request-scoped context the orchestration loop injects into every
    tool call — actor identity and correlation, NEVER exposed to
    Claude's own ``input_schema``. Claude chooses a tool name and a
    schema-conformant input; it can never choose/forge who is making
    the request or which workflow it correlates to."""

    actor_type: str
    actor_id: str
    correlation_id: str


ToolHandler = Callable[[Mapping[str, Any], ToolExecutionContext], Mapping[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """One registered BAGMAN tool (PID §62): name, description (also
    the description Claude itself sees), a JSON Schema input contract,
    an authority class, a short side-effects statement, and the
    implementation function."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    authority_class: str
    side_effects: str
    handler: ToolHandler

    def __post_init__(self) -> None:
        if self.name in FORBIDDEN_TOOL_NAMES:
            raise ValidationError(
                f"tool name '{self.name}' is on BAGMAN's forbidden tool list "
                "(PID §28/§34/§63) and can never be registered"
            )
        if self.authority_class not in AUTHORITY_CLASSES:
            raise ValidationError(
                f"tool '{self.name}': authority_class {self.authority_class!r} is not one "
                f"of the closed set {sorted(AUTHORITY_CLASSES)} (PID §62) — no CD-5 tool "
                "may declare a write/mutate authority class"
            )


class ToolRegistry:
    """A fixed, closed set of :class:`ToolSpec`. Constructed once (by
    ``agent.tools.handlers.build_default_tool_registry``) and never
    mutated afterwards — there is no ``register()``/``add()`` method,
    deliberately: the registry's membership is decided entirely at
    process-composition time, never at request time.
    """

    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ValidationError(f"duplicate tool name '{spec.name}' in ToolRegistry")
            by_name[spec.name] = spec
        self._by_name: Mapping[str, ToolSpec] = dict(by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(sorted(self._by_name.values(), key=lambda s: s.name))

    def list_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Provider-neutral tool definitions
        (:class:`ai.providers.claude.client.ToolDefinition`) ready to
        pass straight to ``ClaudeClient.send_message(tools=...)``."""
        return tuple(
            ToolDefinition(name=s.name, description=s.description, input_schema=s.input_schema)
            for s in self.list_specs()
        )

    def execute(
        self, name: str, tool_input: Mapping[str, Any], *, context: ToolExecutionContext
    ) -> Mapping[str, Any]:
        """Execute the registered tool ``name`` with ``tool_input``.

        Fails CLOSED (PID §63): an unregistered/invalid tool name is
        looked up in this fixed mapping ONLY — never a dynamic/
        reflective dispatch against BAGMAN internals — and raises
        ``core.errors.NotFoundError`` immediately if absent, before any
        handler code runs. ``tool_input`` is then validated against the
        tool's own ``input_schema`` (``core.errors.ValidationError`` if
        it fails) before the handler is ever invoked.
        """
        spec = self._by_name.get(name)
        if spec is None:
            raise NotFoundError(
                f"'{name}' is not a registered BAGMAN tool — registered tools: "
                f"{sorted(self._by_name)}. Invalid/unregistered tool calls fail closed "
                "(PID §63); BAGMAN never performs a dynamic/arbitrary dispatch."
            )
        errors = describe_schema_errors(dict(tool_input), dict(spec.input_schema))
        if errors:
            raise ValidationError(
                f"tool '{name}' input failed validation against its own input_schema: "
                f"{'; '.join(errors)}"
            )
        return spec.handler(tool_input, context)
