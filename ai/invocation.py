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

State machine (PID §28; CD-6 PID §98/§100.14/§100.16 reliability delta
adds ``TIMED_OUT``/``CANCELLED``, see below)
------------------------------------------------------------------------
``ALLOWED_TRANSITIONS`` is the single source of truth::

    REQUESTED -> {RUNNING, FAILED, REJECTED, CANCELLED}
    RUNNING   -> {SUCCEEDED, FAILED, TIMED_OUT, CANCELLED}
    SUCCEEDED -> {}   (terminal)
    FAILED    -> {}   (terminal)
    REJECTED  -> {}   (terminal)
    TIMED_OUT -> {}   (terminal)
    CANCELLED -> {}   (terminal)

Three design decisions worth reading before changing this table:

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
* **`TIMED_OUT` and `CANCELLED` (CD-6 reliability delta, PID §100.14's
  honestly-flagged finding — a stuck-`RUNNING` `AIInvocation` from a
  real live Ask BAGMAN incident) are new, narrowly-scoped terminal
  states, not a redefinition of the three above.**

  - `TIMED_OUT` means "BAGMAN's own bounded timeout fired — we gave up
    waiting" — distinct from `FAILED`'s "the provider actively
    errored". Two producers: (1)
    `agent.claude_code.orchestrator.handle_operator_message` maps a
    `ClaudeCodeOutcomeStatus.TIMEOUT` runner outcome here instead of to
    `FAILED` (see that module and `agent.claude_code.runner` for the
    real subprocess-timeout mechanism this reports); (2) this module's
    own stale-`RUNNING`-recovery backstop (see "Stale-`RUNNING`
    recovery" below) uses `TIMED_OUT` with
    `error_code=STALE_RECOVERY_ERROR_CODE` for a row that was
    definitely never going to complete because whatever process was
    running it is gone. Reachable only from `RUNNING` (never
    `REQUESTED`): a request cannot time out before it was ever
    dispatched — that is `REJECTED`/`FAILED`'s territory, not a new
    third pre-dispatch outcome.
  - `CANCELLED` means "an invocation was explicitly cancelled — by an
    operator action or a deliberate application-level decision —
    rather than having failed or timed out on its own". PID §100's
    "required behaviour explicitly includes task cancellation as its
    own case" is honoured here as a real, transition-tested terminal
    state; there is deliberately no live UI trigger for it in this
    delivery (Ask BAGMAN has no "cancel" button today, and inventing
    one merely to exercise this edge would be exactly the kind of
    fabricated affordance PID §98.2's "no fake buttons" doctrine
    forbids) — see `tests/integration/test_ai_invocation_domain.py`
    and `tests/persistence/test_ai_invocation_repository.py` for the
    direct, repository-level transition tests that exercise it
    instead. Reachable from BOTH `REQUESTED` (cancelled before a
    provider call was ever attempted — e.g. a future "cancel this
    pending request" action) and `RUNNING` (cancelled mid-flight — the
    same shape of decision as `TIMED_OUT`'s "we stopped waiting", just
    operator-initiated rather than a bound expiring). Never reachable
    from `SUCCEEDED`/`FAILED`/`REJECTED`/`TIMED_OUT` — every terminal
    state stays terminal; cancellation is not a way to retroactively
    un-fail or un-succeed something.

Stale-`RUNNING` recovery (CD-6 reliability delta, PID §100.14/§100.16)
------------------------------------------------------------------------
The live incident this delta fixes (see this delivery's own evidence/
report, and the root-cause finding recorded there) proved that a
process-level event (a deliberate `bagman-api` restart, an OOM kill, a
crash, a host reboot — ANY cause, not merely the specific client-
disconnect scenario first observed) can terminate the process that was
mid-flight inside `RUNNING`, abandoning the row forever with no code
left running anywhere to ever revisit it. `ALLOWED_TRANSITIONS` alone
cannot fix this — a state machine only enforces which transitions are
valid, not that SOME transition eventually happens.

The architect's explicit requirement is a **bounded, deterministic**
backstop — "do not introduce an unrestricted background worker
architecture merely to solve this" — so this is deliberately NOT a
polling daemon/cron/background thread. It is a check performed lazily,
at the exact two points where staleness matters:
:meth:`AIInvocationRepository.create_invocation` (when it finds an
existing "active" row for the subject) and
:meth:`AIInvocationRepository.find_active_invocation`. Both concrete
repositories (:class:`InMemoryAIInvocationRepository` and
`persistence.postgres.ai_invocation_repository.PostgresAIInvocationRepository`)
implement this identically, sharing the exact same threshold/predicate
(:func:`is_stale_running`) and the exact same recovery transition
(:func:`recover_stale_invocation`) — see each repository's own
docstring for its concurrency-specific details (Postgres: row-locked
under `SELECT ... FOR UPDATE`, the same discipline its own
`transition_status` already uses; in-memory: single-process, no
locking needed).

:data:`STALE_RUNNING_THRESHOLD_SECONDS` is a **fixed constant**, not
derived live from `ai.tasks.TASK_REGISTRY`'s own `timeout_seconds`
values, for a concrete, structural reason: `ai.tasks` already imports
FROM this module (`TaskRole`, `BACKGROUND_CAPABILITY_ALIASES`) — a
live import back from here would be circular. The chosen value (10
minutes) is documented, not arbitrary: at the time this delta was
written, `ASK_BAGMAN_V1` (`ai/tasks.py`) carries this system's longest
registered `timeout_seconds`, 90 — the only task whose provider call
can plausibly run for minutes rather than seconds (a bounded headless
Claude Code subprocess, not a bounded LiteLLM HTTP call). 600 seconds
is roughly 6-7x that bound: comfortable headroom over the subprocess
timeout itself (90s) + the runner's own best-effort post-timeout
cleanup (`agent.claude_code.runner`'s `communicate(timeout=5.0)`) +
realistic database/network latency + ordinary clock skew, while still
being tight enough that an operator who hits a genuinely stuck subject
is never blocked for more than ~10 minutes even in the worst case.
Whoever registers a future task with a `timeout_seconds` approaching
this bound must revisit this constant explicitly — it is not
recalculated automatically, exactly because it cannot be, by
construction.

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
order: ``evidence_id``, ``intake_id``, ``entity_id``, ``conversation_id``.
The first of these present with a non-empty string value is the
"subject" the concurrency guard applies to. If `input_references`
contains none of them, `create_invocation` refuses to create the
record at all (`core.errors.ValidationError`) — this is a deliberate,
stricter reading of PID §29 ("do not create opaque AI analysis
disconnected from source evidence") than `IntakeRecord` needs, because
unlike an intake attempt (which legitimately starts with no evidence
yet), an AI invocation's whole point is to reason ABOUT something that
must already be canonically identifiable.

``conversation_id`` (CD-6 PID §98/§100 reliability delta) is
deliberately LAST in precedence, not first or alongside the other
three: it exists to satisfy the architect's traceability ruling that
"an operator-originated conversational message is itself a valid
traceable input" for Ask BAGMAN's persistent header affordance — which
today has no multi-turn server-side conversation state (each call is
one bounded, independent Claude Code invocation, see
`agent.claude_code.orchestrator`'s own module docstring) but DOES have
a real, GUI-generated identifier for "this open drawer session" (see
`app/api/static/features/ai/ask-bagman.js`). When an operator asks
Ask BAGMAN a question WHILE a specific document/intake/entity is
attached (the more common, more specific case — "Ask BAGMAN about
this"), the invocation should stay keyed on THAT canonical subject, not
be diluted onto the surrounding conversation — a second, unrelated
question typed moments later in the same open drawer but about the
SAME document should still correctly conflict with an in-flight first
one about that document (unchanged, pre-existing doctrine). Only when
none of `evidence_id`/`intake_id`/`entity_id` is present — the
genuinely contextless "hi bagman" case PID §100 identifies as a real,
previously-broken path (see `derive_primary_input_reference`'s own
docstring for the exact production incident this fixes) — does
`conversation_id` become the subject, so that two turns typed in quick
succession in the SAME open conversation still correctly conflict with
each other (the existing, unchanged "wait for the prior one to reach a
terminal state" doctrine, now applying to a conversation subject
instead of only a document subject), while two DIFFERENT conversations
(two browser tabs, or the drawer closed and reopened) never conflict
with each other at all.

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
from core.audit import AuditRepository, InMemoryAuditRepository
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

#: The seven invocation states (PID §28; `TIMED_OUT`/`CANCELLED` added
#: by the CD-6 reliability delta, PID §100.14/§100.16 — see module
#: docstring's "State machine" section). Matches the contract's closed
#: `status` enum exactly.
STATUSES = frozenset(
    {"REQUESTED", "RUNNING", "SUCCEEDED", "FAILED", "REJECTED", "TIMED_OUT", "CANCELLED"}
)

#: States from which no further transition is possible (PID §28;
#: `TIMED_OUT`/`CANCELLED` added by the CD-6 reliability delta).
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "REJECTED", "TIMED_OUT", "CANCELLED"})

#: The single source of truth for valid invocation state transitions
#: (PID §28). See the module docstring's "State machine" section for
#: the rationale behind the `REQUESTED -> FAILED` edge, the
#: `REJECTED`-only-from-`REQUESTED` restriction, and the CD-6 reliability
#: delta's `TIMED_OUT`/`CANCELLED` additions (in particular why
#: `TIMED_OUT` is reachable only from `RUNNING` while `CANCELLED` is
#: reachable from both `REQUESTED` and `RUNNING`).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"RUNNING", "FAILED", "REJECTED", "CANCELLED"}),
    "RUNNING": frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "REJECTED": frozenset(),
    "TIMED_OUT": frozenset(),
    "CANCELLED": frozenset(),
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
#: :func:`derive_primary_input_reference` checks them. `conversation_id`
#: is deliberately LAST (CD-6 reliability delta) — see the module
#: docstring's "Concurrency guard" section for the full precedence
#: reasoning.
PRIMARY_INPUT_REFERENCE_KEYS: tuple[str, ...] = ("evidence_id", "intake_id", "entity_id", "conversation_id")

#: Bounded, deterministic stale-`RUNNING` recovery threshold (CD-6
#: reliability delta) — see the module docstring's "Stale-`RUNNING`
#: recovery" section for the full reasoning behind both the fixed
#: (non-derived) nature of this constant and its exact value.
STALE_RUNNING_THRESHOLD_SECONDS: float = 600.0

#: `error_code` stamped on an `AIInvocation` the stale-`RUNNING`
#: recovery backstop transitions to `TIMED_OUT` — distinguishes "we
#: gave up waiting because the recovery backstop found an abandoned row"
#: from `agent.claude_code.orchestrator`'s own, more specific
#: `CLAUDE_CODE_TIMEOUT` (a live runner call that itself hit its
#: subprocess timeout while something was still there to observe it).
STALE_RECOVERY_ERROR_CODE = "STALE_RECOVERY_TIMEOUT"

#: `error_code` stamped on a stale `REQUESTED` row the recovery
#: backstop finds — see :func:`recover_stale_invocation`'s own
#: docstring for why this is a DIFFERENT terminal state/code than
#: :data:`STALE_RECOVERY_ERROR_CODE` (a real, live-reproduced defect a
#: fresh Auditor found and this constant fixes, CD-6 reliability delta
#: follow-up): a row abandoned while still `REQUESTED` was never
#: dispatched to a provider at all — the module's own pre-existing
#: `REQUESTED -> FAILED` doctrine ("a request can fail before it is
#: ever dispatched") already covers exactly this shape of outcome;
#: `TIMED_OUT` is documented, elsewhere in this same module, as
#: reachable ONLY from `RUNNING` ("a request cannot time out before it
#: was ever dispatched — that is REJECTED/FAILED's territory"), so
#: unconditionally targeting `TIMED_OUT` for every stale row regardless
#: of its actual current status violated the module's own state
#: machine and made a stale `REQUESTED` row permanently unrecoverable
#: (every recovery attempt raised `InvalidStateTransitionError`,
#: crashing the caller with an HTTP 500 instead of freeing the
#: subject — worse than the original stuck-row symptom this whole
#: backstop exists to fix).
STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE = "STALE_RECOVERY_NEVER_DISPATCHED"

#: `AuditEvent.event_type` recorded (same discipline every other status
#: transition already gets, per the architect's explicit instruction)
#: the moment the stale-`RUNNING` recovery backstop transitions an
#: abandoned row — emitted by the repository itself (not by an external
#: caller, since no external caller is positioned to observe this: it
#: happens transparently as a side effect of some UNRELATED caller's own
#: `create_invocation`/`find_active_invocation` call for a *different*,
#: brand-new invocation attempt against the same subject).
STALE_RECOVERY_AUDIT_EVENT_TYPE = "AI_INVOCATION_STALE_RECOVERED"


def is_stale_running(invocation: "AIInvocation", *, now: Optional[datetime] = None) -> bool:
    """True if ``invocation`` is non-terminal (`REQUESTED`/`RUNNING`)
    and has been sitting there longer than
    :data:`STALE_RUNNING_THRESHOLD_SECONDS` — the single shared
    predicate both :class:`InMemoryAIInvocationRepository` and
    `persistence.postgres.ai_invocation_repository.PostgresAIInvocationRepository`
    use, so the threshold/semantics can never drift between them (PID
    §100's explicit "implemented identically... in BOTH repositories"
    requirement).

    ``now`` is injectable purely for deterministic testing (construct a
    row with an artificially old `started_at` and call this directly,
    or freeze `now` instead of sleeping for real) — defaults to
    :func:`core.timestamps.utc_now`.
    """
    if invocation.status not in ("REQUESTED", "RUNNING"):
        return False
    now = now if now is not None else utc_now()
    return (now - invocation.started_at).total_seconds() > STALE_RUNNING_THRESHOLD_SECONDS


def recover_stale_invocation(invocation: "AIInvocation") -> "AIInvocation":
    """Return the terminal `AIInvocation` a stale, abandoned ``invocation``
    (see :func:`is_stale_running`) is recovered into — never a silent
    delete/ignore (PID §100: "a stale invocation must be transitioned
    explicitly and audibly to an appropriate terminal/recovered
    state"). The single shared recovery transition both repositories
    apply, via the same :func:`transition` state-machine enforcement
    every other status change goes through — this is not a bespoke
    bypass of `ALLOWED_TRANSITIONS`.

    **Target status depends on ``invocation.status`` at the moment it
    went stale** (a fresh Auditor found and this fixes a real,
    live-reproduced defect in an earlier version of this function that
    ignored this distinction): a `RUNNING` row was genuinely dispatched
    to a provider and abandoned mid-flight — `TIMED_OUT` (with
    :data:`STALE_RECOVERY_ERROR_CODE`), matching the module's own
    documented "we dispatched and gave up waiting" meaning for that
    state. A `REQUESTED` row was abandoned BEFORE ever being dispatched
    (e.g. the process died in the real, reachable window between
    `create_invocation` returning `REQUESTED` and a caller's own
    subsequent `transition_status(..., "RUNNING")` a few lines later,
    exactly the caller pattern `agent.claude_code.orchestrator
    .handle_operator_message` uses) — `FAILED` (with
    :data:`STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE`), matching this
    module's own pre-existing `REQUESTED -> FAILED` doctrine, since
    `ALLOWED_TRANSITIONS["REQUESTED"]` deliberately does not include
    `TIMED_OUT` at all (see that table's own docstring: "a request
    cannot time out before it was ever dispatched"). Unconditionally
    targeting `TIMED_OUT` regardless of the row's actual status (the
    original version of this function) made a stale `REQUESTED` row
    permanently unrecoverable — every attempt raised
    `InvalidStateTransitionError`, crashing the caller with an HTTP 500
    instead of freeing the subject.

    Does not itself persist anything or emit an audit event — callers
    (the two concrete repositories) are responsible for both, using
    :data:`STALE_RECOVERY_AUDIT_EVENT_TYPE`.
    """
    if invocation.status == "RUNNING":
        target_status = "TIMED_OUT"
        error_code = STALE_RECOVERY_ERROR_CODE
        reason = (
            f"started_at was older than STALE_RUNNING_THRESHOLD_SECONDS "
            f"({STALE_RUNNING_THRESHOLD_SECONDS}s) with no terminal transition ever "
            "recorded while RUNNING — recovered by the bounded stale-RUNNING backstop, "
            "not a live runner outcome"
        )
    else:
        target_status = "FAILED"
        error_code = STALE_RECOVERY_NEVER_DISPATCHED_ERROR_CODE
        reason = (
            f"started_at was older than STALE_RUNNING_THRESHOLD_SECONDS "
            f"({STALE_RUNNING_THRESHOLD_SECONDS}s) while still REQUESTED — the process that "
            "would have dispatched this invocation to a provider is gone; recovered by the "
            "bounded stale-RUNNING backstop as FAILED (REQUESTED cannot reach TIMED_OUT, "
            "PID §28's own doctrine — a request that never dispatched did not time out, it "
            "simply never happened)"
        )

    return transition(
        invocation,
        target_status,
        error_code=error_code,
        usage_metadata={
            **dict(invocation.usage_metadata),
            "stale_recovery": {
                "reason": reason,
                "recovered_at": to_contract_string(utc_now()),
            },
        },
    )


def derive_primary_input_reference(input_references: Mapping[str, Any]) -> str:
    """Return the one canonical reference `input_references` names as
    this invocation's subject (PID §29/§73) — the first of
    :data:`PRIMARY_INPUT_REFERENCE_KEYS`, in that precedence order,
    present with a non-empty string value.

    `conversation_id` (CD-6 reliability delta, last in precedence) is
    what fixes the real, live, production-confirmed defect PID §98/§100
    record: before this delta, a genuinely contextless Ask BAGMAN
    message (no `evidence_id`/`intake_id`/`entity_id` — e.g. a plain
    "hi bagman" typed into the GUI's persistent header affordance with
    no document/intake/entity attached) had no recognised primary
    reference at all and this function raised `ValidationError`
    unconditionally, which `agent.claude_code.orchestrator
    .handle_operator_message` let propagate as an HTTP 422 — the
    architect's own ruling is that this was always wrong: "an
    operator-originated conversational message is itself a valid
    traceable input... Ask BAGMAN MUST NOT require an external
    EvidenceItem for ordinary conversation."

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
    """Repository abstraction for AIInvocation (PID §27).

    Concrete implementations (:class:`InMemoryAIInvocationRepository`,
    `persistence.postgres.ai_invocation_repository.PostgresAIInvocationRepository`)
    each accept an `audit_repository` at construction — used ONLY to
    emit :data:`STALE_RECOVERY_AUDIT_EVENT_TYPE` when
    `create_invocation`/`find_active_invocation` silently discover and
    recover an abandoned stale-`RUNNING` row (see module docstring's
    "Stale-`RUNNING` recovery" section). This is deliberately different
    from every OTHER `AIInvocation` audit event (`AI_INVOCATION_REQUESTED`/
    `_SUCCEEDED`/`_FAILED`/...), which the CALLER emits explicitly
    alongside its own repository calls (see
    `agent.claude_code.orchestrator.handle_operator_message` and
    `ai.gateway.background.run_background_task`) — stale recovery is the
    one transition that happens transparently, as a side effect of some
    UNRELATED caller's own `create_invocation`/`find_active_invocation`
    call, so no external caller is ever positioned to audit it itself.
    """

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
        a bounded stale-`RUNNING` recovery pass (CD-6 reliability delta
        — see module docstring: if the subject's existing "active" row
        is actually abandoned per :func:`is_stale_running`, it is
        transitioned to `TIMED_OUT` first, audibly, before this method
        proceeds), and the one-active-invocation concurrency guard (PID
        §73) — raising `core.errors.ActiveInvocationConflictError` if a
        genuinely NON-terminal (and non-stale) invocation still exists
        for the exact same `(task_id, task_version,
        primary_input_reference)` subject (see module docstring).

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
        """Read-only(-ish) lookup: the invocation currently in a
        NON-terminal status (`REQUESTED` or `RUNNING`) for this exact
        subject, if any — the same check `create_invocation` performs
        internally, exposed for callers that want to know without
        attempting a create (e.g. a GUI deciding whether to grey out a
        'run analysis' button). Returns `None` if none exists — a
        query, not a fetch-by-ID, so it never raises `NotFoundError`.

        CD-6 reliability delta: if the row found IS non-terminal but is
        stale per :func:`is_stale_running`, this method recovers it
        (transitions it to `TIMED_OUT`, audibly — see module docstring's
        "Stale-`RUNNING` recovery" section and the `AIInvocationRepository`
        class docstring) and returns `None` — a genuinely abandoned row
        must never be reported as "active" to a caller deciding whether
        to grey out a button, or Ask BAGMAN's own `create_invocation`
        pre-check above would keep believing a dead subject is still in
        flight forever.
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
        primary_input_reference: Optional[str] = None,
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

        ``primary_input_reference`` (CD-5 WI-4 addition): an optional
        equality filter on exactly the same value
        :func:`derive_primary_input_reference` computes for each record
        at creation time — i.e. whichever of `evidence_id`/`intake_id`/
        `entity_id` a given invocation's `input_references` carries
        (PID §29/§73's "subject" — see this module's own docstring).
        This is what lets a caller ask "every AIInvocation — of any
        `task_id`, terminal or not — about THIS canonical subject",
        which `find_active_invocation` deliberately cannot answer (that
        method is scoped to one exact `(task_id, task_version)` pair and
        only ever returns a single NON-terminal record). The Documents
        AI panel (WI-4) is this filter's motivating caller: "what AI
        analysis exists for this document" needs every invocation for
        its `evidence_id`, across `DOCUMENT_SUMMARY`/
        `DOCUMENT_TYPE_PROPOSAL`/`ENTITY_PROPOSAL`, in every status.
        """
        raise NotImplementedError


class InMemoryAIInvocationRepository(AIInvocationRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self, *, audit_repository: Optional[AuditRepository] = None) -> None:
        self._by_id: dict[str, AIInvocation] = {}
        #: subject tuple -> ai_invocation_id of the currently-active
        #: (non-terminal) invocation for that subject, if any.
        self._active_by_subject: dict[tuple[str, int, str], str] = {}
        # Single-process, GIL-serialised — no real concurrency to defend
        # against here, unlike the Postgres implementation's row locking
        # (module docstring's "Stale-RUNNING recovery" section). Defaults
        # to a fresh, private `InMemoryAuditRepository` (never `None`)
        # so stale-recovery audit emission never has to special-case a
        # missing sink; a caller that wants stale-recovery events to
        # land in the SAME audit trail the rest of the app reads
        # (e.g. `core.api.BagmanCanonicalAPI.audit_repository`) passes
        # it explicitly — `app/api/composition.py` does exactly this.
        self._audit_repository: AuditRepository = audit_repository or InMemoryAuditRepository()

    def _recover_if_stale(self, invocation: AIInvocation) -> Optional[AIInvocation]:
        """If ``invocation`` (a row this instance currently believes is
        active for its subject) is stale per :func:`is_stale_running`,
        transition it to the appropriate terminal state (see
        :func:`recover_stale_invocation`'s own docstring — `TIMED_OUT`
        for a `RUNNING` row, `FAILED` for a `REQUESTED` one), persist,
        clear it from `_active_by_subject`, emit
        :data:`STALE_RECOVERY_AUDIT_EVENT_TYPE`, and return the
        recovered record. Returns `None` if ``invocation`` is not
        actually stale (the ordinary, common case) — callers
        distinguish "nothing to recover" from "recovered" by this
        return value, never by a side effect alone.
        """
        if not is_stale_running(invocation):
            return None

        recovered = recover_stale_invocation(invocation)
        self._by_id[recovered.ai_invocation_id] = recovered
        subject = (
            recovered.task_id,
            recovered.task_version,
            derive_primary_input_reference(recovered.input_references),
        )
        if self._active_by_subject.get(subject) == recovered.ai_invocation_id:
            del self._active_by_subject[subject]

        # `error_code`/`status` read from `recovered` itself, never a
        # hardcoded constant — see `recover_stale_invocation`'s own
        # docstring for why the target terminal state (and therefore
        # the correct error_code) depends on the row's pre-recovery
        # status.
        self._audit_repository.record_audit_event(
            event_type=STALE_RECOVERY_AUDIT_EVENT_TYPE,
            actor_type=actor.SYSTEM,
            actor_id="ai-invocation-stale-recovery",
            subject_type="AIInvocation",
            subject_id=recovered.ai_invocation_id,
            correlation_id=recovered.correlation_id,
            causation_id=None,
            payload={
                "task_id": recovered.task_id,
                "task_version": recovered.task_version,
                "recovered_status": recovered.status,
                "error_code": recovered.error_code,
                "stale_threshold_seconds": STALE_RUNNING_THRESHOLD_SECONDS,
            },
        )
        return recovered

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
            # Bounded stale-RUNNING recovery backstop (CD-6 reliability
            # delta) — BEFORE the conflict check below, exactly per the
            # module docstring: an abandoned row must never permanently
            # block this subject.
            self._recover_if_stale(self._by_id[existing_id])
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
        if existing_id is None:
            return None
        existing = self._by_id[existing_id]
        # Bounded stale-RUNNING recovery backstop (CD-6 reliability
        # delta) — see module docstring and `AIInvocationRepository
        # .find_active_invocation`'s own docstring: an abandoned row is
        # never reported as active.
        recovered = self._recover_if_stale(existing)
        if recovered is not None:
            return None
        return existing

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
        primary_input_reference: Optional[str] = None,
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
        if primary_input_reference is not None:
            # Computed per-record (the in-memory repository has no
            # separate indexed column for this — see module docstring)
            # using the exact same derivation every record's subject was
            # established with at create_invocation time. Every record
            # reaching this repository was already proven to yield one
            # (create_invocation refuses otherwise, PID §29), so this
            # never raises here.
            records = [
                r for r in records if derive_primary_input_reference(r.input_references) == primary_input_reference
            ]

        records.sort(key=lambda r: (r.started_at, r.ai_invocation_id), reverse=True)

        if limit is None:
            return records[offset:]
        return records[offset : offset + limit]
