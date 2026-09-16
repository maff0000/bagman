"""``FakeClaudeCodeOperatorRunner`` — the deterministic substitute for
:class:`agent.claude_code.runner.ClaudeCodeOperatorRunner` used by
every ordinary test and by development/test composition (PID §61 — no
real subprocess in ordinary tests). Mirrors
``ai.providers.litellm.fake.FakeLiteLLMClient``/
``ai.providers.claude.fake.FakeClaudeClient``'s own established
scripting convention exactly.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from agent.claude_code.runner import ClaudeCodeInvocationResult, ClaudeCodeOutcomeStatus


@dataclass(frozen=True)
class RecordedInvocation:
    """One captured `run()` call — exposed so a test can assert exactly
    what was sent, in particular that `system_prompt` (BAGMAN's fixed
    operator instructions + trusted context) and `user_prompt`
    (governed context + untrusted evidence + the operator's question)
    reached this point as the caller intended."""

    system_prompt: str
    user_prompt: str
    timeout_seconds: float


class FakeClaudeCodeOperatorRunner:
    """Deterministic, in-memory `ClaudeCodeOperatorRunnerProtocol`
    implementation.

    Usage: `queue_success(...)`/`queue_failure(...)` push one scripted
    outcome onto a FIFO queue; each `run()` call pops and returns the
    next one. If a `default_response` factory was supplied at
    construction and the queue is empty, that is used instead. If
    neither exists, `run()` raises `AssertionError` loudly — a test/
    dev-composition bug (an unscripted call), never a silently
    fabricated success.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        default_response: Optional[Callable[[str, str], ClaudeCodeInvocationResult]] = None,
    ) -> None:
        self._available = available
        self._default_response = default_response
        self._queue: "deque[ClaudeCodeInvocationResult]" = deque()
        #: Every `run()` call ever made, in order — test-inspection only.
        self.calls: list[RecordedInvocation] = []

    # -- scripting ------------------------------------------------------

    def queue_success(
        self,
        *,
        text: str,
        session_id: str = "fake-session-id",
        model_usage: Optional[Mapping[str, Any]] = None,
        total_cost_usd: float = 0.0001,
        duration_ms: int = 5,
        num_turns: int = 1,
    ) -> None:
        self._queue.append(
            ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.OK,
                text=text,
                session_id=session_id,
                model_usage=dict(model_usage) if model_usage else {"fake-claude-code-model-v1": {}},
                total_cost_usd=total_cost_usd,
                duration_ms=duration_ms,
                num_turns=num_turns,
                permission_denials=(),
            )
        )

    def queue_failure(
        self,
        *,
        status: ClaudeCodeOutcomeStatus,
        error_detail: str = "",
        duration_ms: Optional[int] = None,
    ) -> None:
        self._queue.append(
            ClaudeCodeInvocationResult(status=status, error_detail=error_detail, duration_ms=duration_ms)
        )

    def set_available(self, available: bool) -> None:
        self._available = available

    # -- ClaudeCodeOperatorRunnerProtocol --------------------------------

    def run(
        self, *, system_prompt: str, user_prompt: str, timeout_seconds: float
    ) -> ClaudeCodeInvocationResult:
        self.calls.append(
            RecordedInvocation(
                system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds
            )
        )

        if self._queue:
            return self._queue.popleft()
        if self._default_response is not None:
            return self._default_response(system_prompt, user_prompt)
        raise AssertionError(
            "FakeClaudeCodeOperatorRunner.run(): no scripted response queued and no "
            "default_response configured — call queue_success()/queue_failure() before "
            "exercising this path"
        )

    def is_available(self) -> bool:
        return self._available
