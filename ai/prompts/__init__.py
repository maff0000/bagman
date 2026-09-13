"""Versioned prompt template assets (CD-5 PID §31, WI-2).

Task prompts are code/configuration assets, not strings assembled
inline in ``ai/gateway/``. Each background task's SYSTEM instruction
text lives at ``ai/prompts/<task_id lowercased>/v<N>.md`` and is loaded
by :mod:`ai.prompts.loader`, referenced by
``AIInvocation.prompt_contract_version`` (e.g. ``"v1"``). Changing
material task instructions requires adding a new ``vN.md`` file (a new
version) rather than editing an existing one in place — the same
compatibility discipline every other versioned BAGMAN contract already
follows.

These files are always used as the ``system``-role message content
(see ``ai.providers.litellm.client.build_messages``) — never
concatenated with untrusted evidence content into one string (PID
§26/§32). The evidence content a task reasons about is always the
separate ``user``-role message; nothing in a prompt asset here is
allowed to reference or embed it.
"""
from __future__ import annotations
