"""``FakeClaudeClient`` — the deterministic Claude double every ordinary
test and development-mode composition uses (CD-5 PID §61, WI-3).

Never performs network I/O. Satisfies
:class:`ai.providers.claude.client.ClaudeClientProtocol` structurally
(same ``model`` attribute, ``send_message``/``is_available`` methods)
so it plugs into ``agent.bagman.orchestrator`` and
``app/api/composition.py`` with no caller-side branching.

Two ways to script a response:

* ``text_turn(...)`` / ``tool_use_turn(...)`` build a
  :class:`~ai.providers.claude.client.ClaudeTurnResult` to hand back
  verbatim — this is what lets a test genuinely exercise the
  orchestration loop's tool-call handling (a scripted ``tool_use_turn``
  followed by a scripted ``text_turn`` reproduces exactly the two-turn
  shape a real tool-calling conversation has).
* Any ``Exception`` instance in ``scripted_responses`` (ordinarily one
  of :mod:`ai.providers.claude.client`'s ``Claude*Error`` types) is
  raised instead of returned — this is how a test proves the
  orchestration loop's Claude-failure handling (PID §84's failure-proof
  doctrine) without ever needing a live, actually-failing Anthropic
  endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Union

from ai.providers.claude.client import ClaudeTurnResult, ToolCallRequest, ToolDefinition

ScriptedItem = Union[ClaudeTurnResult, BaseException]


def text_turn(
    text: str, *, provider_model: str = "fake-claude-v1", latency_ms: int = 1
) -> ClaudeTurnResult:
    """Build a scripted plain-text final answer (``stop_reason="end_turn"``,
    no tool calls)."""
    return ClaudeTurnResult(
        stop_reason="end_turn",
        text=text,
        tool_calls=(),
        provider_model=provider_model,
        usage={"input_tokens": 0, "output_tokens": 0},
        latency_ms=latency_ms,
        raw_content_blocks=({"type": "text", "text": text},),
    )


def tool_use_turn(
    *calls: tuple[str, str, Mapping[str, Any]], provider_model: str = "fake-claude-v1"
) -> ClaudeTurnResult:
    """Build a scripted tool-call request (``stop_reason="tool_use"``).

    ``calls`` is one or more ``(tool_call_id, tool_name, tool_input)``
    tuples — one call per requested tool use, exactly mirroring how
    Claude can request multiple tool calls in a single turn.
    """
    tool_calls = tuple(
        ToolCallRequest(tool_call_id=call_id, name=name, input=dict(tool_input))
        for call_id, name, tool_input in calls
    )
    raw_blocks = tuple(
        {"type": "tool_use", "id": c.tool_call_id, "name": c.name, "input": dict(c.input)}
        for c in tool_calls
    )
    return ClaudeTurnResult(
        stop_reason="tool_use",
        text=None,
        tool_calls=tool_calls,
        provider_model=provider_model,
        usage={"input_tokens": 0, "output_tokens": 0},
        latency_ms=1,
        raw_content_blocks=raw_blocks,
    )


@dataclass(frozen=True)
class RecordedCall:
    """One recorded ``send_message`` invocation — asserted against in
    tests (e.g. the prompt-injection fixture proves ``system`` never
    contains untrusted tool-result content)."""

    system: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[ToolDefinition, ...]


class FakeClaudeClient:
    """Deterministic :class:`~ai.providers.claude.client.ClaudeClientProtocol`
    double. See module docstring for how to script responses/failures.
    """

    def __init__(
        self,
        *,
        scripted_responses: Sequence[ScriptedItem] = (),
        default_text: str = (
            "This is a deterministic fake Claude response — no live Anthropic "
            "credential is configured for this environment."
        ),
        available: bool = True,
        model: str = "fake-claude-v1",
    ) -> None:
        self._queue: List[ScriptedItem] = list(scripted_responses)
        self._default_text = default_text
        self._available = available
        self.model = model
        #: Every call this fake received, in order — for test assertions
        #: (e.g. "the system prompt never embeds tool-result content").
        self.calls: List[RecordedCall] = []

    def send_message(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition] = (),
        max_tokens: Optional[int] = None,
    ) -> ClaudeTurnResult:
        self.calls.append(RecordedCall(system=system, messages=tuple(messages), tools=tuple(tools)))
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return text_turn(self._default_text, provider_model=self.model)

    def is_available(self) -> bool:
        return self._available
