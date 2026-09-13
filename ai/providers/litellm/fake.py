"""``FakeLiteLLMClient`` — the deterministic substitute for
:class:`ai.providers.litellm.client.LiteLLMClient` used by every
ordinary test and by development/test composition (CD-5 PID §61, WI-2).

Mirrors exactly the "dev-only stub, never mistaken for the real thing"
doctrine ``app/api/composition.py``'s own
``_AlwaysCleanDevelopmentScanner`` already establishes for
``EvidenceSafetyScanner`` — this class performs no I/O of any kind, is
fully scripted by the test/caller, and satisfies
:class:`ai.providers.litellm.client.LiteLLMClientProtocol` exactly, so
``ai.gateway.background.run_background_task`` and
``app/api/routers/ai.py`` never need to know which implementation they
were handed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from ai.providers.litellm.client import (
    LiteLLMCompletionResult,
    LiteLLMOutcomeStatus,
    build_messages,
    validate_capability_alias,
)


@dataclass(frozen=True)
class RecordedCall:
    """One captured `complete()` invocation — exposed so a test can
    assert exactly what was sent, in particular that
    `system_instructions` (task instructions) and `evidence_content`
    (untrusted data) were kept as two distinct strings all the way to
    the point of being handed to the (fake) wire call — see
    `tests/integration/test_prompt_injection_structural.py`."""

    capability_alias: str
    system_instructions: str
    evidence_content: str
    timeout_seconds: float
    messages: list[dict[str, str]]


class FakeLiteLLMClient:
    """Deterministic, in-memory `LiteLLMClientProtocol` implementation.

    Usage: `queue_success(...)` / `queue_failure(...)` push one scripted
    outcome onto a per-alias FIFO queue; each `complete()` call for that
    alias pops and returns the next one. If a `default_response` factory
    was supplied at construction and an alias's queue is empty, that is
    used instead (handy for tests that do not care about the exact
    content, only that *something* valid comes back). If neither exists,
    `complete()` raises `AssertionError` loudly — a test/dev-composition
    bug (an unscripted call), never silently returns a fabricated
    success.

    `default_response` receives the exact `system_instructions` string
    `complete()` was called with (CD-5 WI-4 addition) — not a bare
    zero-arg factory — specifically so a caller like
    `app/api/composition.py`'s development-mode wiring can return a
    result shaped correctly for WHICHEVER task is actually being
    exercised (each CD-5 background task's prompt names its own
    `task_id` verbatim in its system instructions, e.g. "task
    DOCUMENT_SUMMARY" — see `ai/prompts/*/v1.md`), without needing to
    know in advance which one will be called next. Nothing before this
    WI ever constructed `FakeLiteLLMClient` with a `default_response` at
    all, so this is a safe, additive signature change.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        default_response: Optional[Callable[[str], LiteLLMCompletionResult]] = None,
    ) -> None:
        self._available = available
        self._default_response = default_response
        self._queues: dict[str, list[LiteLLMCompletionResult]] = defaultdict(list)
        #: Every `complete()` call ever made, in order — test-inspection only.
        self.calls: list[RecordedCall] = []

    # -- scripting ------------------------------------------------------

    def queue_success(
        self,
        *,
        capability_alias: str,
        content: str,
        provider_model: str = "fake-litellm-backend-v1",
        usage_metadata: Optional[Mapping[str, Any]] = None,
        latency_ms: int = 5,
        request_id: Optional[str] = None,
    ) -> None:
        self._queues[capability_alias].append(
            LiteLLMCompletionResult(
                status=LiteLLMOutcomeStatus.OK,
                content=content,
                provider_model=provider_model,
                usage_metadata=dict(usage_metadata) if usage_metadata else {},
                latency_ms=latency_ms,
                request_id=request_id,
            )
        )

    def queue_failure(
        self,
        *,
        capability_alias: str,
        status: LiteLLMOutcomeStatus,
        error_detail: str = "",
        latency_ms: Optional[int] = None,
    ) -> None:
        self._queues[capability_alias].append(
            LiteLLMCompletionResult(status=status, error_detail=error_detail, latency_ms=latency_ms)
        )

    def set_available(self, available: bool) -> None:
        self._available = available

    # -- LiteLLMClientProtocol ------------------------------------------

    def complete(
        self,
        *,
        capability_alias: str,
        system_instructions: str,
        evidence_content: str,
        timeout_seconds: float,
    ) -> LiteLLMCompletionResult:
        # Exactly the same mechanical enforcement the real client uses
        # (PID §5/§9/§10) — shared, not reimplemented, so the fake can
        # never silently accept something the real client would refuse.
        validate_capability_alias(capability_alias)

        messages = build_messages(system_instructions, evidence_content)
        self.calls.append(
            RecordedCall(
                capability_alias=capability_alias,
                system_instructions=system_instructions,
                evidence_content=evidence_content,
                timeout_seconds=timeout_seconds,
                messages=messages,
            )
        )

        queue = self._queues.get(capability_alias)
        if queue:
            return queue.pop(0)
        if self._default_response is not None:
            return self._default_response(system_instructions)
        raise AssertionError(
            f"FakeLiteLLMClient.complete(): no scripted response queued for "
            f"capability_alias={capability_alias!r} and no default_response configured — "
            "call queue_success()/queue_failure() before exercising this path"
        )

    def is_available(self) -> bool:
        return self._available
