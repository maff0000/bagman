"""BAGMAN's bounded headless Claude Code operator runner (CD-5 Gate-2
closure, 2026-09-16 — see ``PID.md`` §97 and the CD-5 evidence file).

Supersedes the CD-5 WI-3 assumption that BAGMAN talks to Claude via a
direct Anthropic Messages API adapter under BAGMAN's own API key
(``ai/providers/claude/``). The authoritative operator topology is
instead::

    Matt
      -> BAGMAN Ask BAGMAN HTML UI
      -> bagman-api
      -> agent.claude_code (this package)
      -> `claude -p` (headless Claude Code, its own auth/session)
      -> response

BAGMAN never holds, reads, or transmits an Anthropic API key for this
path. Claude Code owns its own authentication/session mechanism
entirely; this package's job is to invoke it SAFELY and BOUNDEDLY:

* :mod:`agent.claude_code.runner` — the process-execution boundary.
  Fixed executable/argument contract, never a shell, never any
  caller-supplied value in the executable name, flags, cwd, or
  environment. ``--tools ""`` (all tools disabled) + ``--restricted``
  (belt-and-braces: also refuses command-running tools, ignores
  project/user settings files, confines file tools to the working
  directory) give the invoked Claude Code process ZERO ability to run
  shell commands, edit files, or reach anything outside the prompt
  text it was given — the operator's authority is deliberately a
  strict SUBSET of, never inherited from, this host's own normal
  Claude Code development-agent authority.
* :mod:`agent.claude_code.fake` — the deterministic substitute every
  ordinary test and dev/test composition uses (PID §61 — no real
  subprocess in ordinary tests), mirroring
  ``ai.providers.litellm.fake``/``ai.providers.claude.fake``'s own
  established pattern exactly.
* :mod:`agent.claude_code.orchestrator` — turns one Ask BAGMAN message
  into a bounded, single-turn (no live tool-calling loop — PID's own
  "keep the implementation bounded" instruction) Claude Code
  invocation: BAGMAN's own application layer assembles governed
  context (evidence/intake/entity, fetched directly, never handed to
  Claude as tool-call authority) and passes it as clearly-separated
  prompt sections; untrusted evidence content is always DATA, never
  instructions.

No BAGMAN component outside this package ever constructs a `claude`
subprocess invocation.
"""
from __future__ import annotations
