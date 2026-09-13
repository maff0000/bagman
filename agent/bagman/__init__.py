"""``agent/bagman/`` — the BAGMAN AI agent's own reasoning/orchestration
layer (CD-5 PID §17/§42-44, WI-3): Ask BAGMAN's system-prompt
construction, tool-calling loop, and `AIInvocation` lifecycle
management. Acts only as a governed client of BAGMAN's own services
(``core.api.BagmanCanonicalAPI``, ``ai.invocation``/``ai.tasks``, and
``agent.tools``'s fixed tool registry) — see ``orchestrator.py``'s own
module docstring for the full design.
"""
from agent.bagman.orchestrator import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    TASK_ID,
    TASK_VERSION,
    AskBagmanResult,
    handle_operator_message,
)

__all__ = [
    "AskBagmanResult",
    "handle_operator_message",
    "TASK_ID",
    "TASK_VERSION",
    "DEFAULT_MAX_TOOL_ITERATIONS",
]
