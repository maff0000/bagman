"""``ai/providers/claude/`` — the Anthropic Messages API provider adapter
(CD-5 PID §6/§8/§17-18/§33-35/§49, WI-3).

A pure provider adapter: it knows how to talk to the Anthropic Messages
API (native tool-use included) and normalise the result into a
provider-neutral shape (:class:`ai.providers.claude.client.ClaudeTurnResult`)
that ``agent/bagman/orchestrator.py`` acts on without needing to know
Anthropic's wire format. It contains **no** BAGMAN-specific
orchestration logic (no system-prompt construction, no tool registry,
no `AIInvocation` persistence) — that is ``agent/bagman/``'s job. See
that package's own module docstring for the exact component-boundary
decision this WI made.

``client.py`` is the real, network-speaking adapter — instantiated only
by ``app/api/composition.py`` in production. ``fake.py`` is the
deterministic double every ordinary test and development-mode
composition uses instead (PID §61) — never a live model.
"""
