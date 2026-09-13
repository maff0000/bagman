"""``ai/providers/`` — the provider-adapter layer of BAGMAN.AI (CD-5 PID
§6/§8).

Every module under here is a thin, provider-NEUTRAL-RESULT-producing
adapter for exactly one external AI provider — never BAGMAN-specific
orchestration logic (that lives in ``agent/`` for the Claude operator
path, and will live in ``ai/gateway/`` for the background-task routing
path). Two adapters are authorised for CD-5:

* ``ai/providers/claude/`` — the Anthropic Messages API adapter (WI-3).
* ``ai/providers/litellm/`` — the one LiteLLM-speaking adapter for both
  the Mac-mini and Trinity-escalation tiers (WI-2, built in parallel;
  not present in this work item's delivery).

No other provider adapter package may exist here (PID §6): BAGMAN never
instantiates an Ollama/MLX/llama.cpp/OpenAI-compatible/raw-HTTP-LLM
client anywhere else in the codebase.
"""
