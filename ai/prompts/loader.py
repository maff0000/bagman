"""Load versioned prompt assets by ``(task_id, task_version)`` (CD-5
PID §31, WI-2; task-version-aware keying added CD-6 Slice 5 WI-3 §10).

The single source of truth for "which prompt version does task X at
contract version Y currently use" is :data:`_TASK_PROMPT_VERSIONS`
below — deliberately a plain dict, not derived from
``ai.tasks.TASK_REGISTRY``'s own ``task_version`` (a task's CONTRACT
version and its CURRENT prompt version are related but independently
evolvable: a prompt wording fix that stays behaviourally compatible
does not necessarily need to bump `task_version`, and this WI's own
dispatch is explicit that "changing material task instructions
requires version change OR documented compatibility rationale" — i.e.
not every prompt change forces a contract version bump). Only
``BACKGROUND`` tasks are covered here — ``OPERATOR_DOCUMENT_REVIEW``'s
prompt is WI-3's own (Claude operator gateway) scope, not this one.

Keyed by ``(task_id, task_version)``, not ``task_id`` alone (WI-3 §10)
------------------------------------------------------------------------
CD-5's original keying by ``task_id`` alone was a real, documented
defect once `DOCUMENT_TYPE_PROPOSAL` gained a second, contract-
incompatible version (v2, CD-6 Slice 5 WI-3): v1's own open/generic
`proposed_type` vocabulary and v2's closed canonical enum are mutually
incompatible prompt contracts, so a caller resolving "the" prompt for
`DOCUMENT_TYPE_PROPOSAL` with no version qualifier could silently pair
the WRONG prompt with either task's `output_schema`. Every caller
(`ai.gateway.background.run_background_task`, and any test that calls
this function directly) now supplies both `task_id` AND `task_version`.
"""
from __future__ import annotations

from pathlib import Path

from core.errors import NotFoundError

_PROMPTS_ROOT = Path(__file__).resolve().parent

#: (task_id, task_version) -> current prompt_contract_version for every
#: registered BACKGROUND task (PID §21; WI-3 §10 additively registers
#: (DOCUMENT_TYPE_PROPOSAL, 2)). Add a new entry here (and a new
#: `ai/prompts/<task_id lowercased>/v<N>.md` file) when a task's prompt
#: version advances — never edit an existing `vN.md` file's wording in
#: place once it has ever been used for a real invocation.
_TASK_PROMPT_VERSIONS: dict[tuple[str, int], str] = {
    ("DOCUMENT_SUMMARY", 1): "v1",
    ("DOCUMENT_TYPE_PROPOSAL", 1): "v1",
    ("DOCUMENT_TYPE_PROPOSAL", 2): "v2",
    ("DOCUMENT_TYPE_PROPOSAL", 3): "v3",
    ("ENTITY_PROPOSAL", 1): "v1",
}


def resolve_prompt_contract_version(task_id: str, task_version: int) -> str:
    """Return the current prompt version string (e.g. ``"v1"``/``"v2"``)
    for `(task_id, task_version)` — the value stored as
    `AIInvocation.prompt_contract_version`.

    Raises:
        core.errors.NotFoundError: if `(task_id, task_version)` has no
            registered BACKGROUND prompt (e.g. it is unknown, or it is
            `OPERATOR_DOCUMENT_REVIEW`, the Claude operator gateway's
            own scope).
    """
    try:
        return _TASK_PROMPT_VERSIONS[(task_id, task_version)]
    except KeyError:
        raise NotFoundError(
            f"no registered background prompt version for task_id={task_id!r} "
            f"task_version={task_version!r} — registered: {sorted(_TASK_PROMPT_VERSIONS)}"
        ) from None


def load_system_prompt(task_id: str, prompt_contract_version: str) -> str:
    """Load the SYSTEM-role instruction text for `task_id` at
    `prompt_contract_version` (e.g. ``"v1"``) from
    ``ai/prompts/<task_id lowercased>/<prompt_contract_version>.md``.

    Raises:
        core.errors.NotFoundError: if no such asset file exists.
    """
    path = _PROMPTS_ROOT / task_id.lower() / f"{prompt_contract_version}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NotFoundError(
            f"no prompt asset for task_id={task_id!r} prompt_contract_version="
            f"{prompt_contract_version!r} (expected at {path}): {exc}"
        ) from None
