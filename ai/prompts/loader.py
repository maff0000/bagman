"""Load versioned prompt assets by ``task_id`` (CD-5 PID §31, WI-2).

The single source of truth for "which prompt version does task X
currently use" is :data:`_TASK_PROMPT_VERSIONS` below — deliberately a
plain dict, not derived from ``ai.tasks.TASK_REGISTRY``'s
``task_version`` (a task's CONTRACT version and its CURRENT prompt
version are related but independently evolvable: a prompt wording fix
that stays behaviourally compatible does not necessarily need to bump
`task_version`, and this WI's own dispatch is explicit that "changing
material task instructions requires version change OR documented
compatibility rationale" — i.e. not every prompt change forces a
contract version bump). Only ``BACKGROUND`` tasks are covered here —
``OPERATOR_DOCUMENT_REVIEW``'s prompt is WI-3's own scope.
"""
from __future__ import annotations

from pathlib import Path

from core.errors import NotFoundError

_PROMPTS_ROOT = Path(__file__).resolve().parent

#: task_id -> current prompt_contract_version for CD-5's three
#: BACKGROUND tasks (PID §21). Add a new entry here (and a new
#: `ai/prompts/<task_id lowercased>/v<N>.md` file) when a task's prompt
#: version advances — never edit an existing `vN.md` file's wording in
#: place once it has ever been used for a real invocation.
_TASK_PROMPT_VERSIONS: dict[str, str] = {
    "DOCUMENT_SUMMARY": "v1",
    "DOCUMENT_TYPE_PROPOSAL": "v1",
    "ENTITY_PROPOSAL": "v1",
}


def resolve_prompt_contract_version(task_id: str) -> str:
    """Return the current prompt version string (e.g. ``"v1"``) for
    `task_id` — the value stored as
    `AIInvocation.prompt_contract_version`.

    Raises:
        core.errors.NotFoundError: if `task_id` has no registered
            BACKGROUND prompt (e.g. it is unknown, or it is
            `OPERATOR_DOCUMENT_REVIEW`, WI-3's own scope).
    """
    try:
        return _TASK_PROMPT_VERSIONS[task_id]
    except KeyError:
        raise NotFoundError(
            f"no registered background prompt version for task_id={task_id!r} — "
            f"registered: {sorted(_TASK_PROMPT_VERSIONS)}"
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
