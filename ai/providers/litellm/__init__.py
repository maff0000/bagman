"""The ONE LiteLLM-speaking adapter (CD-5 PID §6/§8/§50, WI-2).

Everything BAGMAN knows about the LiteLLM gateway it talks to lives
here and nowhere else: :class:`ai.providers.litellm.client.LiteLLMClient`
speaks the real ``POST /v1/chat/completions`` OpenAI-compatible wire
protocol, alias-only (never a physical model name — PID §5/§10), and
:class:`ai.providers.litellm.fake.FakeLiteLLMClient` is the deterministic
substitute every ordinary test and dev/test composition uses instead
(PID §61 — no live LLM in ordinary tests).

Final topology (CD-5 Gate-1 closure, 2026-09-16): this module speaks
to HELM's dedicated, BAGMAN-exclusive Mac AI appliance — its own
LiteLLM + PostgreSQL, `http://192.168.11.4:4100`, configured for
`response_format` JSON-schema-constrained structured output — not the
shared Trinity LiteLLM installation this package originally targeted
at CD-5's initial dispatch. See the CD-5 evidence file
(`memory/generated/CD5-EVIDENCE-...md`) for the full, preserved history
of how the topology arrived here; nothing about this module's own
wire protocol/alias-only contract changed across that history.
"""
from __future__ import annotations
