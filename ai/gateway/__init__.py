"""BAGMAN AI task orchestration (CD-5 PID §6, WI-2).

``ai.gateway.background.run_background_task`` is the one function that
turns a validated task request into a fully-lived ``AIInvocation``
lifecycle (``REQUESTED -> RUNNING -> {SUCCEEDED, FAILED}``) against the
``LITELLM`` provider adapter (``ai.providers.litellm``), emitting the
PID §57 audit events along the way. The Claude-operator equivalent
(``OPERATOR`` role, ``ANTHROPIC`` provider) is WI-3's own
``ai/gateway/`` addition, not this module's concern — see
``run_background_task``'s own explicit `role != "BACKGROUND"` refusal.
"""
from __future__ import annotations
