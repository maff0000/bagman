"""``agent/tools/`` — the discrete, read-only/analyse-only tools the
BAGMAN AI agent (Claude, the operator intelligence) may invoke (CD-5
PID §33-35/§62-63, WI-3).

``registry.py`` owns the mechanism (fixed mapping, fail-closed
dispatch); ``handlers.py`` owns the content (the 8 PID §34 tools);
``background.py`` owns the ``run_background_analysis`` tool's seam into
WI-2's real background-task gateway (not present in this worktree).
"""
from agent.tools.handlers import ToolDependencies, build_default_tool_registry
from agent.tools.registry import (
    FORBIDDEN_TOOL_NAMES,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)

__all__ = [
    "ToolDependencies",
    "build_default_tool_registry",
    "FORBIDDEN_TOOL_NAMES",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolSpec",
]
