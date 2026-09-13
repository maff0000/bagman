"""BAGMAN.AI — the general, provider-agnostic AI gateway/task-routing/
persistence component (CD-5 PID §6-7, WI-1).

This is the `ai/` side of the CD-5 component-boundary decision recorded
in the WI-1 delivery report: `ai/` owns the AI invocation domain model,
the task contract/registry framework, and the durable repository — all
of it provider-neutral (it does not know or care whether a given task
ultimately calls Claude, the dedicated Mac mini, or Trinity compute;
that is the adapter layer's job, WI-2/WI-3). The Claude-operator-facing
orchestration loop, tool registry, and Claude-specific policy live
under the separate, already-reserved `agent/` component instead
(`agent/bagman/`, `agent/tools/`, `agent/policies/` — populated
starting in WI-3, not by this work item).

WI-1 delivers only:

* ``ai.invocation`` — the ``AIInvocation`` domain model, its state
  machine, and the ``AIInvocationRepository`` abstraction (in-memory
  reference implementation here; the durable PostgreSQL implementation
  lives in ``persistence/postgres/ai_invocation_repository.py``,
  exactly mirroring how ``services/evidence/intake/intake.py`` and
  ``persistence/postgres/intake_repository.py`` divide the same
  responsibility for `IntakeRecord`).
* ``ai.tasks`` — the ``TaskContract``/``TaskRegistry`` framework, the
  four CD-5 PID §21 tasks' *metadata* (not their prompt content), and
  ``validate_task_output``.

WI-1 does NOT create ``ai/gateway/``, ``ai/providers/``, ``ai/prompts/``,
``ai/policy/``, ``ai/evaluation/``, or ``ai/provenance/`` — those are
WI-2/WI-3/WI-5's job to populate (PID §6's suggested structure).
"""
