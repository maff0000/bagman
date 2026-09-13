"""The ONE LiteLLM-speaking adapter (CD-5 PID §6/§8/§50, WI-2).

Everything BAGMAN knows about the existing Trinity LiteLLM installation
lives here and nowhere else: :class:`ai.providers.litellm.client.LiteLLMClient`
speaks the real ``POST /v1/chat/completions`` OpenAI-compatible wire
protocol, alias-only (never a physical model name — PID §5/§10), and
:class:`ai.providers.litellm.fake.FakeLiteLLMClient` is the deterministic
substitute every ordinary test and dev/test composition uses instead
(PID §61 — no live LLM in ordinary tests).
"""
from __future__ import annotations
