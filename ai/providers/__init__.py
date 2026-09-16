"""BAGMAN.AI provider adapters (CD-5 PID §6/§8, WI-2/WI-3).

Each subpackage here is the ONE place that actually speaks a given
provider's wire protocol — ``ai/providers/litellm/`` (WI-2) for
BAGMAN's LiteLLM-fronted background tiers (``ai/providers/claude/``,
WI-3, is the separate Anthropic operator provider). Final topology
(CD-5 Gate-1 closure, 2026-09-16 — see the CD-5 evidence file's own
architecture-history note for the superseded intermediate states):
``ai/providers/litellm/`` speaks to a dedicated, BAGMAN-exclusive Mac
AI appliance (its own LiteLLM + PostgreSQL, not the shared Trinity
installation this package originally targeted) for `bagman-fast`/
`bagman-core`, escalating via the same appliance to Trinity compute
for `bagman-deep`. Nothing outside a provider's own
subpackage may construct an HTTP request to that provider directly
(PID §6/§70) — ``ai/gateway/`` (background tasks) and ``agent/``
(the Claude operator path) orchestrate against these adapters'
normalised, provider-neutral result types only.

No other provider adapter package may exist here: BAGMAN never
instantiates an Ollama/MLX/llama.cpp/OpenAI-compatible/raw-HTTP-LLM
client anywhere else in the codebase (PID §6/§19).
"""
from __future__ import annotations
