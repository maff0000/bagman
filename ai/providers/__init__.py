"""BAGMAN.AI provider adapters (CD-5 PID §6, WI-2/WI-3).

Each subpackage here is the ONE place that actually speaks a given
provider's wire protocol — ``ai/providers/litellm/`` (this WI, WI-2)
for the existing Trinity LiteLLM installation, ``ai/providers/claude/``
(WI-3) for the Anthropic operator provider. Nothing outside a
provider's own subpackage may construct an HTTP request to that
provider directly (PID §6/§70) — ``ai/gateway/`` orchestrates against
these adapters' normalised, provider-neutral result types only.
"""
from __future__ import annotations
