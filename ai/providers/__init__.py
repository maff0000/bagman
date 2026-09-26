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
`bagman-core` (two generation PROFILES against BAGMAN's one
permanently-resident Mac-mini model — CD-6 §103 Inference Architecture
Ruling). The same appliance also fronts `trinity-core` — CD-6 §103's
sole authorised Trinity alias, backlog/overflow processing only (`ai.jobs`),
never a routine per-request escalation; this RETIRES the earlier
`bagman-deep` tier, which routed to a genuinely different, heavier
Trinity-hosted model as a routine escalation (no live task contract
ever used it — see `ai.invocation.BACKGROUND_CAPABILITY_ALIASES`'s own
docstring for the full history). Nothing outside a provider's own
subpackage may construct an HTTP request to that provider directly
(PID §6/§70) — ``ai/gateway/`` (background tasks) and ``agent/``
(the Claude operator path) orchestrate against these adapters'
normalised, provider-neutral result types only.

No other provider adapter package may exist here: BAGMAN never
instantiates an Ollama/MLX/llama.cpp/OpenAI-compatible/raw-HTTP-LLM
client anywhere else in the codebase (PID §6/§19).
"""
from __future__ import annotations
