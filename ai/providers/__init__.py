"""BAGMAN.AI provider adapters (CD-5 PID §6/§8, WI-2/WI-3).

Each subpackage here is the ONE place that actually speaks a given
provider's wire protocol — ``ai/providers/litellm/`` (WI-2) for the
existing Trinity LiteLLM installation (both the Mac-mini and
Trinity-escalation tiers), ``ai/providers/claude/`` (WI-3) for the
Anthropic operator provider. Nothing outside a provider's own
subpackage may construct an HTTP request to that provider directly
(PID §6/§70) — ``ai/gateway/`` (background tasks) and ``agent/``
(the Claude operator path) orchestrate against these adapters'
normalised, provider-neutral result types only.

No other provider adapter package may exist here: BAGMAN never
instantiates an Ollama/MLX/llama.cpp/OpenAI-compatible/raw-HTTP-LLM
client anywhere else in the codebase (PID §6/§19).
"""
from __future__ import annotations
