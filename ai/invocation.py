"""Canonical `AIInvocation` domain model, state machine, and repository
(CD-5 PID §26-30/§73-76, WI-1).

An `AIInvocation` represents a single durable, auditable attempt to run
one governed BAGMAN AI task (PID §21) against an authorised provider/
capability. It is created the moment BAGMAN accepts a task request
(`REQUESTED`) and is durably updated as it moves through the
deterministic state machine below (PID §28) until it reaches a
terminal state — `SUCCEEDED`, `FAILED`, or `REJECTED`.

This module is provider-NEUTRAL by construction (PID §6/§70): it has no
knowledge of Anthropic/LiteLLM wire formats, no HTTP client, and never
imports anything from a future `ai/providers/`. It exists purely to
answer "what AI work did BAGMAN ask for, what happened, and can it
prove it" — the actual provider call is WI-2 (background tasks) / WI-3
(Claude operator) scope.

State machine (PID §28)
------------------------
``ALLOWED_TRANSITIONS`` is the single source of truth::

    REQUESTED -> {RUNNING, FAILED, REJECTED}
    RUNNING   -> {SUCCEEDED, FAILED}
    SUCCEEDED -> {}   (terminal)
    FAILED    -> {}   (terminal)
    REJECTED  -> {}   (terminal)

Two design decisions worth reading before changing this table:

* **`REQUESTED -> FAILED` is a real, direct edge.** A request can fail
  before it is ever dispatched to a provider — e.g. an infrastructure
  failure while attempting to start execution, or a queue/timeout
  condition hit before `RUNNING` is ever reached. Modelling this as a
  direct edge (rather than forcing every failure through `RUNNING`
  first) keeps `RUNNING` meaning "actually in flight with a provider",
  which is what §47/§48's per-tier health/failure-class distinctions
  need.
* **`REJECTED` is reachable only from `REQUESTED`, never from
  `RUNNING`.** `REJECTED` means "this invocation was never allowed to
  be dispatched to a provider at all" — e.g. an unknown task/version,
  a data-policy refusal (PID §51), or (this module's own concern) the
  one-active-invocation concurrency guard below. Once an invocation has
  actually reached `RUNNING` (a provider call was genuinely attempted),
  any negative outcome — a provider error, a timeout, or output that
  fails structured validation (PID §76) — is `FAILED`, not `REJECTED`.
  This keeps the two terminal-failure states meaningfully distinct
  rather than overlapping: `REJECTED` = "we said no before asking";
  `FAILED` = "we asked and it did not work".

Concurrency guard — "one active invocation per subject" (PID §73)
-------------------------------------------------------------------
PID §73 recommends "one active invocation per (evidence_id, task_id,
task_version) unless a deliberate retry/new-run request is recorded".
This module generalises "evidence_id" to a **primary input reference**
(see :func:`derive_primary_input_reference`) because not every task's
`input_references` is guaranteed to carry an `evidence_id` key
specifically (e.g. a hypothetical future task keyed off `entity_id`
alone) — but every task's input MUST carry at least one of a small,
fixed, documented set of recognised reference keys, in this precedence
order: ``evidence_id``, ``intake_id``, ``entity_id``. The first of
these present with a non-empty string value is the "subject" the
concurrency guard applies to. If `input_references` contains none of
them, `create_invocation` refuses to create the record at all
(`core.errors.ValidationError`) — this is a deliberate, stricter
reading of PID §29 ("do not create opaque AI analysis disconnected
from source evidence") than `IntakeRecord` needs, because unlike an
intake attempt (which legitimately starts with no evidence yet), an AI
invocation's whole point is to reason ABOUT something that must already
be canonically identifiable.

Given that subject tuple `(task_id, task_version, primary_input_reference)`,
`create_invocation` raises `core.errors.ActiveInvocationConflictError`
if an existing invocation for the exact same subject is still in a
NON-terminal status (`REQUESTED` or `RUNNING`). Once that invocation
reaches a terminal state, the same subject may be invoked again freely
— this creates a genuinely NEW, distinct `AIInvocation` row (PID §74:
"retries create auditable invocation attempts... do not overwrite the
history of a failed AI request"), never a mutation of the old one. This
is a different concurrency doctrine from
`services.evidence.intake.intake`'s idempotency-key replay (which
returns the SAME record forever for a repeated key) — see that
module's docstring for the contrast; do not copy its replay semantics
here.

`persistence/postgres/ai_invocation_models.py` backs this same
invariant with a REAL, database-enforced partial unique index (not
application-level pre-checking alone) — see that module's docstring for
the exact mechanism.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Optional

from core import actor, identity
from core.contract_validation import validate_against_contract
from core.errors import (
    ActiveInvocationConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "ai/bagman.ai_invocation.v1.schema.json"

#: CD-5 WI-1's only supported contract version for AIInvocation; the
#: contract itself pins `schema_version` to this exact value via a
#: JSON Schema `const`, so it is never a caller-supplied parameter.
SCHEMA_VERSION = "bagman.ai_invocation.v1"

#: The five invocation states (PID §28). Matches the contract's closed
#: `status` enum exactly.
STATUSES = frozenset({"REQUESTED", "RUNNING", "SUCCEEDED", "FAILED", "REJECTED"})

#: States from which no further transition is possible (PID §28).
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "REJECTED"})

#: The single source of truth for valid invocation state transitions
#: (PID §28). See the module docstring for the rationale behind the
#: `REQUESTED -> FAILED` edge and the `REJECTED`-only-from-`REQUESTED`
#: restriction.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"RUNNING", "FAILED", "REJECTED"}),
    "RUNNING": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "REJECTED": frozenset(),
}

#: The two application-level provider-routing roles (PID §8/§20's
#: locked three-tier topology collapses to exactly two roles at the
#: BAGMAN-application level — the Mac-mini-vs-Trinity-escalation split
#: lives entirely inside `capability_alias`, never as a third role).
#: `TaskRole` is the static-typing form of the exact same closed set,
#: used by `ai.tasks.TaskContract.role` — `ai.tasks` imports it from
#: here rather than redefining it, so there is exactly one source of
#: truth for this vocabulary (both its runtime `frozenset` form and its
#: `Literal` type-hint form).
TaskRole = Literal["OPERATOR", "BACKGROUND"]
ROLES = frozenset({"OPERATOR", "BACKGROUND"})

#: The two BAGMAN provider ADAPTERS CD-5 authorises (PID §6/§8) —
#: never a physical backend/model name. See the contract's own
#: `provider` field description for why this is a closed set.
PROVIDERS = frozenset({"LITELLM", "ANTHROPIC"})

#: THE single closed-set source of truth for BAGMAN's background
#: capability aliases (PID §9). Every later work item's alias
#: validation must import and use THIS constant — never redefine a
#: parallel list, and never add a `trinity-*` value here (PID §9's own
#: explicit prohibition; see `tests/integration/test_architecture_boundaries.py`
#: for the repo-wide `trinity-*` absence check WI-5 is expected to add).
BACKGROUND_CAPABILITY_ALIASES: frozenset[str] = frozenset({"bagman-fast", "bagman-core", "bagman-deep"})

#: Recognised `input_references` keys, in the precedence order
#: :func:`derive_primary_input_reference` checks them. See the module
#: docstring's "Concurrency guard" section.
PRIMARY_INPUT_REFERENCE_KEYS: tuple[str, ...] = ("evidence_id", "intake_id", "entity_id")


def derive_primary_input_reference(input_references: Mapping[str, Any]) -> str:
    """Return the one canonical reference `input_references` names as
    this invocation's subject (PID §29/§73) — the first of
    :data:`PRIMARY_INPUT_REFERENCE_KEYS`, in that precedence order,
    present with a non-empty string value.

    Raises:
        core.errors.ValidationError: if `input_references` contains
            none of the recognised keys with a usable value — an AI
            invocation must always be traceable to a canonical subject
            (PID §29); see the module docstring for why this is
            stricter than `IntakeRecord`'s own provenance requirements.
    """
    for key in PRIMARY_INPUT_REFERENCE_KEYS:
        value = input_references.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValidationError(
        "input_references must contain at least one recognised primary "
        f"reference key (checked in precedence order: {PRIMARY_INPUT_REFERENCE_KEYS}) "
        "with a non-empty string value — AI inputs must be traceable to "
        "canonical evidence/state (PID §29), and the one-active-invocation "
        "concurrency guard (PID §73) needs a subject to key on; got "
        f"input_references={dict(input_references)!r}"
    )


def validate_role_provider_capability_pairing(
    *, role: str, provider: str, capability_alias: Optional[str]
) -> None:
    """Enforce CD-5's locked role/provider/alias pairing (PID §2/§8):
    `OPERATOR` is served only by the `ANTHROPIC` adapter and never
    carries a `capability_alias`; `BACKGROUND` is served only by the
    `LITELLM` adapter and always carries one of
    :data:`BACKGROUND_CAPABILITY_ALIASES`.

    Raised as a plain `core.errors.ValidationError` rather than a new
    dedicated error type — a PL judgment call (documented here per the
    WI-1 dispatch): this is an input-SHAPE/domain-rule violation, the
    same class of thing `core.audit.InMemoryAuditRepository` already
    raises `ValidationError` for on an invalid `actor_type`, not a
    conflict with existing persisted state (which is what
    `ActiveInvocationConflictError` below is reserved for).
    """
    if role not in ROLES:
        raise ValidationError(f"role '{role}' is not one of the closed set {sorted(ROLES)} (PID §8)")
    if provider not in PROVIDERS:
        raise ValidationError(f"provider '{provider}' is not one of the closed set {sorted(PROVIDERS)} (PID §6/§8)")

    if role == "BACKGROUND":
        if provider != "LITELLM":
            raise ValidationError(
                f"a BACKGROUND invocation must use provider 'LITELLM' (PID §8); got '{provider}' — "
                "only the OPERATOR role may use the 'ANTHROPIC' adapter"
            )
        if capability_alias not in BACKGROUND_CAPABILITY_ALIASES:
            raise ValidationError(
                f"a BACKGROUND invocation's capability_alias must be one of "
                f"{sorted(BACKGROUND_CAPABILITY_ALIASES)} (PID §9); got {capability_alias!r}"
            )
    else:  # role == "OPERATOR"
        if provider != "ANTHROPIC":
            raise ValidationError(
                f"an OPERATOR invocation must use provider 'ANTHROPIC' (PID §8/§17); got '{provider}'"
            )
        if capability_alias is not None:
            raise ValidationError(
                "an OPERATOR invocation's capability_alias must be null — Claude is never "
                f"reached via a LiteLLM alias (PID §8); got {capability_alias!r}"
            )


@dataclass(frozen=True)
class AIInvocation:
    """A single durable AI invocation attempt (PID §26-30). Immutable
    once constructed; every state transition produces a NEW
    `AIInvocation` snapshot via :func:`transition` (frozen dataclasses
    are never mutated in place) that replaces the repository's current
    record for that `ai_invocation_id`.
    """

    ai_invocation_id: str
    task_id: str
    task_version: int
    role: str
    provider: str
    status: str
    started_at: datetime
    correlation_id: str
    actor_type: str
    actor_id: str
    input_references: Mapping[str, Any]
    capability_alias: Optional[str] = None
    provider_model: Optional[str] = None
    completed_at: Optional[datetime] = None
    prompt_contract_version: Optional[str] = None
    output: Optional[Mapping[str, Any]] = None
    confidence: Optional[float] = None
    validation_result: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None
    usage_metadata: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: Optional[int] = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/ai/bagman.ai_invocation.v1.schema.json``."""
        return {
            "ai_invocation_id": self.ai_invocation_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "role": self.role,
            "provider": self.provider,
            "capability_alias": self.capability_alias,
            "provider_model": self.provider_model,
            "started_at": to_contract_string(self.started_at),
            "completed_at": to_contract_string(self.completed_at) if self.completed_at is not None else None,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "actor": {"actor_type": self.actor_type, "actor_id": self.actor_id},
            "input_references": dict(self.input_references),
            "prompt_contract_version": self.prompt_contract_version,
            "output": dict(self.output) if self.output is not None else None,
            "confidence": self.confidence,
            "validation_result": dict(self.validation_result) if self.validation_result is not None else None,
            "error_code": self.error_code,
            "usage_metadata": dict(self.usage_metadata),
            "latency_ms": self.latency_ms,
            "schema_version": self.schema_version,
        }


def transition(invocation: AIInvocation, new_status: str, **field_updates: Any) -> AIInvocation:
    """Move ``invocation`` to ``new_status``, enforcing
    :data:`ALLOWED_TRANSITIONS` (PID §28).

    ``field_updates`` may set any other `AIInvocation` field that is
    legitimately updated alongside a transition — `provider_model`,
    `output`, `confidence`, `validation_result`, `error_code`,
    `usage_metadata`, `latency_ms`, `prompt_contract_version`. Identity
    fields established at creation (`task_id`, `task_version`, `role`,
    `provider`, `capability_alias`, `correlation_id`, `actor_type`,
    `actor_id`, `input_references`, `started_at`) are never legitimate
    `field_updates` targets — passing one raises `ValidationError` just
    like any other unrecognised field name would (this module does not
    special-case reject them individually; `dataclasses.replace` simply
    treats them as ordinary field overrides, so a caller CAN pass them,
    but doing so is not part of this module's supported contract and no
    repository implementation here ever does so).

    ``completed_at`` is handled specially and must NOT be passed in
    ``field_updates``: it is stamped with :func:`core.timestamps.utc_now`
    automatically the moment ``new_status`` is one of
    :data:`TERMINAL_STATUSES`, and is otherwise left as ``invocation``'s
    existing value (which is always ``None`` before a terminal state).

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``invocation.status``.
        core.errors.ValidationError: if the resulting invocation fails
            contract validation, or ``field_updates`` names something
            that is not a real ``AIInvocation`` field.
    """
    allowed = ALLOWED_TRANSITIONS.get(invocation.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"AIInvocation '{invocation.ai_invocation_id}' cannot transition from "
            f"'{invocation.status}' to '{new_status}'; allowed transitions from "
            f"'{invocation.status}' are {sorted(allowed) or '(none — terminal state)'}"
        )

    if "completed_at" in field_updates:
        raise ValidationError(
            "completed_at is stamped automatically by transition() on reaching a "
            "terminal state and must not be supplied directly in field_updates"
        )

    completed_at = utc_now() if new_status in TERMINAL_STATUSES else invocation.completed_at

    try:
        updated = dataclasses.replace(
            invocation,
            status=new_status,
            completed_at=completed_at,
            **field_updates,
        )
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition AIInvocation: {exc}") from exc

    return updated


class AIInvocationRepository(abc.ABC):
    """Repository abstraction for AIInvocation (PID §27)."""

    @abc.abstractmethod
    def create_invocation(
        self,
        *,
        task_id: str,
        task_version: int,
        role: str,
        provider: str,
        capability_alias: Optional[str],
        input_references: Mapping[str, Any],
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        prompt_contract_version: Optional[str] = None,
    ) -> AIInvocation:
        """Create a new `AIInvocation` in `REQUESTED` (PID §28).

        Enforces, in order: the role/provider/capability_alias pairing
        (see :func:`validate_role_provider_capability_pairing`), that
        `input_references` yields a `derive_primary_input_reference`,
        and the one-active-invocation concurrency guard (PID §73) —
        raising `core.errors.ActiveInvocationConflictError` if a
        NON-terminal invocation already exists for the exact same
        `(task_id, task_version, primary_input_reference)` subject (see
        module docstring).

        `correlation_id` is generated fresh if not supplied — an AI
        invocation is normally either the start of a new workflow
        correlation or, more commonly, a continuation of one already
        established upstream (e.g. by evidence intake, PID §58), in
        which case the caller supplies it explicitly.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_invocation(self, ai_invocation_id: str) -> AIInvocation:
        raise NotImplementedError

    @abc.abstractmethod
    def transition_status(self, ai_invocation_id: str, new_status: str, **field_updates: Any) -> AIInvocation:
        """Look up `ai_invocation_id`, apply :func:`transition`,
        persist and return the resulting `AIInvocation`."""
        raise NotImplementedError

    @abc.abstractmethod
    def find_active_invocation(
        self, *, task_id: str, task_version: int, primary_input_reference: str
    ) -> Optional[AIInvocation]:
        """Read-only lookup: the invocation currently in a NON-terminal
        status (`REQUESTED` or `RUNNING`) for this exact subject, if any
        — the same check `create_invocation` performs internally,
        exposed for callers that want to know without attempting a
        create (e.g. a GUI deciding whether to grey out a 'run
        analysis' button). Returns `None` if none exists — a query, not
        a fetch-by-ID, so it never raises `NotFoundError`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def list_invocations(
        self,
        *,
        task_id: Optional[str] = None,
        task_version: Optional[int] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        correlation_id: Optional[str] = None,
        started_at_from: Optional[datetime] = None,
        started_at_to: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[AIInvocation]:
        """List AIInvocations, most-recently-started first.

        Default ordering is ``started_at DESC`` with `ai_invocation_id`
        as a deterministic tie-breaker — the same ordering doctrine
        `services.evidence.intake.intake.IntakeRepository.list_intake_records`
        establishes for `IntakeRecord`. `task_id`/`task_version`/`role`/
        `status`/`correlation_id`/`started_at_from`/`started_at_to` are
        optional equality/range filters — omitted filters match every
        record. `limit`/`offset` page the result; `limit=None` (the
        default, preserved for every existing caller that does not ask
        for pagination) returns every matching record.
        """
        raise NotImplementedError


class InMemoryAIInvocationRepository(AIInvocationRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, AIInvocation] = {}
        #: subject tuple -> ai_invocation_id of the currently-active
        #: (non-terminal) invocation for that subject, if any.
        self._active_by_subject: dict[tuple[str, int, str], str] = {}

    def create_invocation(
        self,
        *,
        task_id: str,
        task_version: int,
        role: str,
        provider: str,
        capability_alias: Optional[str],
        input_references: Mapping[str, Any],
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str] = None,
        prompt_contract_version: Optional[str] = None,
    ) -> AIInvocation:
        validate_role_provider_capability_pairing(role=role, provider=provider, capability_alias=capability_alias)

        if not actor.is_valid(actor_type):
            raise ValidationError(
                f"actor_type '{actor_type}' is not one of the closed set {sorted(actor.ALL)} (PID §14)"
            )

        primary_ref = derive_primary_input_reference(input_references)
        subject = (task_id, task_version, primary_ref)

        existing_id = self._active_by_subject.get(subject)
        if existing_id is not None:
            raise ActiveInvocationConflictError(
                f"an active (non-terminal) AIInvocation '{existing_id}' already exists for "
                f"task_id='{task_id}', task_version={task_version}, "
                f"primary_input_reference='{primary_ref}' — refusing to create a second "
                "concurrent invocation for the same subject (PID §73); wait for it to reach "
                "a terminal state, or use its existing invocation, before retrying"
            )

        try:
            candidate = AIInvocation(
                ai_invocation_id=identity.generate_id(),
                task_id=task_id,
                task_version=task_version,
                role=role,
                provider=provider,
                capability_alias=capability_alias,
                status="REQUESTED",
                started_at=utc_now(),
                correlation_id=correlation_id if correlation_id is not None else identity.generate_id(),
                actor_type=actor_type,
                actor_id=actor_id,
                input_references=dict(input_references),
                prompt_contract_version=prompt_contract_version,
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create AIInvocation: {exc}") from exc

        self._by_id[candidate.ai_invocation_id] = candidate
        self._active_by_subject[subject] = candidate.ai_invocation_id
        return candidate

    def get_invocation(self, ai_invocation_id: str) -> AIInvocation:
        try:
            return self._by_id[ai_invocation_id]
        except KeyError:
            raise NotFoundError(f"no AIInvocation with ai_invocation_id '{ai_invocation_id}'") from None

    def transition_status(self, ai_invocation_id: str, new_status: str, **field_updates: Any) -> AIInvocation:
        current = self.get_invocation(ai_invocation_id)
        updated = transition(current, new_status, **field_updates)
        self._by_id[ai_invocation_id] = updated

        if new_status in TERMINAL_STATUSES:
            subject = (
                updated.task_id,
                updated.task_version,
                derive_primary_input_reference(updated.input_references),
            )
            # Only clear the active-subject slot if THIS invocation is
            # the one currently occupying it (defensive — it always
            # should be, since only one invocation can hold a subject
            # at a time by construction).
            if self._active_by_subject.get(subject) == ai_invocation_id:
                del self._active_by_subject[subject]

        return updated

    def find_active_invocation(
        self, *, task_id: str, task_version: int, primary_input_reference: str
    ) -> Optional[AIInvocation]:
        existing_id = self._active_by_subject.get((task_id, task_version, primary_input_reference))
        return self._by_id[existing_id] if existing_id is not None else None

    def list_invocations(
        self,
        *,
        task_id: Optional[str] = None,
        task_version: Optional[int] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        correlation_id: Optional[str] = None,
        started_at_from: Optional[datetime] = None,
        started_at_to: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[AIInvocation]:
        records = list(self._by_id.values())
        if task_id is not None:
            records = [r for r in records if r.task_id == task_id]
        if task_version is not None:
            records = [r for r in records if r.task_version == task_version]
        if role is not None:
            records = [r for r in records if r.role == role]
        if status is not None:
            records = [r for r in records if r.status == status]
        if correlation_id is not None:
            records = [r for r in records if r.correlation_id == correlation_id]
        if started_at_from is not None:
            records = [r for r in records if r.started_at >= started_at_from]
        if started_at_to is not None:
            records = [r for r in records if r.started_at <= started_at_to]

        records.sort(key=lambda r: (r.started_at, r.ai_invocation_id), reverse=True)

        if limit is None:
            return records[offset:]
        return records[offset : offset + limit]
