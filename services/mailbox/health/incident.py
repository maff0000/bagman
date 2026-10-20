"""``MailboxHealthIncident`` — episode-aware mailbox-connection-health
incident model (PID §118.C/D, WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING
§6.A.2).

Structurally mirrors ``core.entity.GovernedEntity``/``core.entity
.EntityRepository``'s shape exactly (per the governing Work Order's own
explicit instruction): a frozen dataclass domain object, an ABC
repository, and a narrow in-memory reference implementation. The real,
concurrency-safe PostgreSQL implementation
(``persistence.postgres.mailbox_health_repository
.PostgresMailboxHealthIncidentRepository``) is where the partial unique
index (PID §118.C) actually proves the episode mechanism under a real
race — this module's own in-memory implementation is a same-process-only
proxy for it, exactly like every other in-memory repository in this
codebase documents for its own durable sibling.

Why this is its own new Needs You item_type/allowed_action_type,
declared HERE rather than in ``services.needs_you.needs_you``
------------------------------------------------------------------------
Every other Needs You ``item_type``/``allowed_action_type`` constant in
this codebase is declared centrally in ``services.needs_you.needs_you``
(an intentional registry convention, documented in that module's own
docstring). This delivery's own governing Work Order (§6.A.3) authorises
exactly one, narrow, additive change to that file — an optional
``session`` parameter on two existing methods — and explicitly lists
every file this delivery may touch; ``services/needs_you/needs_you.py``
is not authorised for any OTHER change. Since ``item_type``/
``allowed_action_type`` are both deliberately OPEN, non-enum vocabulary
(validated only by a regex, never a JSON Schema ``enum`` — see
``services.needs_you.needs_you``'s own module docstring), a plain string
constant declared in the module that actually OWNS this producer is
fully equivalent, at the contract level, to one declared centrally — the
single difference is where a future reader looks first. Documented here,
explicitly, as a deliberate, scope-driven deviation from the codebase's
usual registry convention — not an oversight.
"""
from __future__ import annotations

import abc
import dataclasses
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from core import identity
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.mailbox.mailbox import CONNECTION_STATE_AUTH_REQUIRED, MailboxSource
from services.mailbox.sweep_run import MailboxSweepRun

#: One incident produces exactly one NeedsYouItem
#: (``source_object_reference = incident_id`` — PID §118.C's own "a
#: fresh, independently-issued identity is required per episode"
#: requirement). See module docstring for why these two constants live
#: here rather than in ``services.needs_you.needs_you``.
ITEM_TYPE_MAILBOX_HEALTH_INCIDENT = "MAILBOX_HEALTH_INCIDENT"
ALLOWED_ACTION_ACKNOWLEDGE_MAILBOX_HEALTH_INCIDENT = "ACKNOWLEDGE_MAILBOX_HEALTH_INCIDENT"

# ---------------------------------------------------------------------
# Failure taxonomy (PID §118.D)
# ---------------------------------------------------------------------
FAILURE_CLASS_AUTH_REQUIRED = "AUTH_REQUIRED"
FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR = "PERSISTENT_PROVIDER_ERROR"
FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE = "OAUTH_RECONNECT_INCOMPLETE"
#: Reserved and inactive (PID §118.D/G) until a separately governed
#: recurring mailbox-sweep schedule exists — see
#: :func:`detect_stale_ingestion` below, which always raises
#: ``NotImplementedError``. Declared here, in the taxonomy, so a future
#: delivery adding real detection logic needs no vocabulary change.
FAILURE_CLASS_STALE_INGESTION = "STALE_INGESTION"

FAILURE_CLASSES = frozenset(
    {
        FAILURE_CLASS_AUTH_REQUIRED,
        FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR,
        FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE,
        FAILURE_CLASS_STALE_INGESTION,
    }
)

#: Bounded consecutive-failure threshold for PERSISTENT_PROVIDER_ERROR
#: (PID §118.D: "incident after three consecutive qualifying failed
#: sweep attempts, reusing the classification-job max_attempts
#: pattern").
PERSISTENT_PROVIDER_ERROR_CONSECUTIVE_THRESHOLD = 3

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUSES = frozenset({STATUS_OPEN, STATUS_RESOLVED})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_OPEN: frozenset({STATUS_RESOLVED}),
    STATUS_RESOLVED: frozenset(),
}


@dataclass(frozen=True)
class MailboxHealthIncident:
    """One mailbox-health incident episode (PID §118.C). Immutable once
    constructed — :func:`resolve` produces a NEW snapshot; a `RESOLVED`
    row is never updated again (module docstring's own "Resolved
    incidents are immutable" invariant)."""

    incident_id: str
    mailbox_id: str
    failure_class: str
    status: str
    first_observed_at: datetime
    last_observed_at: datetime
    resolved_at: Optional[datetime] = None
    recovery_evidence: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Plain-dict rendering for logging/reporting — no JSON Schema
        contract governs this shape (this delivery has no external
        HTTP-facing surface for incidents in Stage A)."""
        return {
            "incident_id": self.incident_id,
            "mailbox_id": self.mailbox_id,
            "failure_class": self.failure_class,
            "status": self.status,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "resolved_at": self.resolved_at,
            "recovery_evidence": dict(self.recovery_evidence) if self.recovery_evidence is not None else None,
            "metadata": dict(self.metadata),
        }


def resolve(incident: MailboxHealthIncident, *, recovery_evidence: Mapping[str, Any], now: Optional[datetime] = None) -> MailboxHealthIncident:
    """Move ``incident`` `OPEN -> RESOLVED`, stamping
    ``resolved_at``/``recovery_evidence`` — the ONLY transition this
    state machine allows (module docstring's own immutability
    invariant: a resolved incident is never updated again, and this
    function is never called on an already-`RESOLVED` row by a correct
    caller)."""
    allowed = ALLOWED_TRANSITIONS.get(incident.status, frozenset())
    if STATUS_RESOLVED not in allowed:
        raise InvalidStateTransitionError(
            f"MailboxHealthIncident '{incident.incident_id}' cannot transition from '{incident.status}' to "
            f"'{STATUS_RESOLVED}'; allowed transitions from '{incident.status}' are "
            f"{sorted(allowed) or '(none — terminal state)'}"
        )
    return dataclasses.replace(
        incident,
        status=STATUS_RESOLVED,
        resolved_at=now if now is not None else utc_now(),
        recovery_evidence=dict(recovery_evidence),
    )


@dataclass(frozen=True)
class IncidentCreationResult:
    """Return shape for
    :meth:`MailboxHealthIncidentRepository.create_incident` — ``created``
    tells the caller whether this is a FRESH episode (so it must also
    create the one-time ``NeedsYouItem``/``INITIAL`` notification intent)
    or an idempotent re-read of an already-OPEN incident for the same
    ``(mailbox_id, failure_class)`` (so it must not)."""

    incident: MailboxHealthIncident
    created: bool


class MailboxHealthIncidentRepository(abc.ABC):
    """Repository abstraction for `MailboxHealthIncident` (PID §118.C)."""

    @abc.abstractmethod
    def create_incident(
        self,
        *,
        mailbox_id: str,
        failure_class: str,
        metadata: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
        session: Optional[Any] = None,
    ) -> IncidentCreationResult:
        """Idempotent, concurrency-safe create-or-get of the `OPEN`
        incident for `(mailbox_id, failure_class)` (PID §118.C): if no
        `OPEN` row exists for this pair, create one and return
        `created=True`; if one already exists (including a concurrent
        creator that won a genuine race), return it UNCHANGED with
        `created=False` — never a second `OPEN` row, never an error.

        `session` is the §118.F shared-transaction extension: when
        supplied, this call participates in the caller's own already-open
        transaction instead of opening its own; omitted (the default),
        behaviour is unchanged from a standalone call.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def touch_observed(
        self, incident_id: str, *, observed_at: Optional[datetime] = None, session: Optional[Any] = None
    ) -> MailboxHealthIncident:
        """Advance `last_observed_at` on an already-`OPEN` incident — a
        plain, narrow update (never touches `status`/`resolved_at`/
        `recovery_evidence`), called by the evaluator every cycle a
        still-failing mailbox reaffirms an episode already has an open
        incident."""
        raise NotImplementedError

    @abc.abstractmethod
    def resolve_incident(
        self,
        incident_id: str,
        *,
        recovery_evidence: Mapping[str, Any],
        now: Optional[datetime] = None,
        session: Optional[Any] = None,
    ) -> MailboxHealthIncident:
        """`OPEN -> RESOLVED` (see :func:`resolve`). `session` is the
        §118.F shared-transaction extension (see
        :meth:`create_incident`'s own docstring)."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_incident(self, incident_id: str) -> MailboxHealthIncident:
        raise NotImplementedError

    @abc.abstractmethod
    def get_open_incident(self, *, mailbox_id: str, failure_class: str) -> Optional[MailboxHealthIncident]:
        """Read-only lookup — never `NotFoundError`; absence of an open
        incident for this pair is an ordinary, expected state."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_open_incidents(self) -> list[MailboxHealthIncident]:
        """Every currently-`OPEN` incident, across every mailbox — the
        evaluator's own per-cycle recovery-check candidate set."""
        raise NotImplementedError


class InMemoryMailboxHealthIncidentRepository(MailboxHealthIncidentRepository):
    """Narrow in-memory reference implementation (PID §21 Option A). A
    plain `threading.Lock` is enough here — there is no real
    cross-process concurrency to defend against in-memory; the real
    proof of the partial-unique-index race lives in
    `tests/persistence/test_mailbox_health_repository.py` against a real
    disposable PostgreSQL instance."""

    def __init__(self) -> None:
        self._by_id: dict[str, MailboxHealthIncident] = {}
        self._open_by_key: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def create_incident(
        self,
        *,
        mailbox_id: str,
        failure_class: str,
        metadata: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
        session: Optional[Any] = None,
    ) -> IncidentCreationResult:
        if failure_class not in FAILURE_CLASSES:
            raise ValidationError(f"'{failure_class}' is not a governed failure_class — must be one of {sorted(FAILURE_CLASSES)}")
        with self._lock:
            key = (mailbox_id, failure_class)
            existing_id = self._open_by_key.get(key)
            if existing_id is not None:
                return IncidentCreationResult(incident=self._by_id[existing_id], created=False)

            resolved_now = now if now is not None else utc_now()
            candidate = MailboxHealthIncident(
                incident_id=identity.generate_id(),
                mailbox_id=mailbox_id,
                failure_class=failure_class,
                status=STATUS_OPEN,
                first_observed_at=resolved_now,
                last_observed_at=resolved_now,
                metadata=dict(metadata) if metadata is not None else {},
            )
            self._by_id[candidate.incident_id] = candidate
            self._open_by_key[key] = candidate.incident_id
            return IncidentCreationResult(incident=candidate, created=True)

    def touch_observed(
        self, incident_id: str, *, observed_at: Optional[datetime] = None, session: Optional[Any] = None
    ) -> MailboxHealthIncident:
        with self._lock:
            current = self.get_incident(incident_id)
            updated = dataclasses.replace(current, last_observed_at=observed_at if observed_at is not None else utc_now())
            self._by_id[incident_id] = updated
            return updated

    def resolve_incident(
        self,
        incident_id: str,
        *,
        recovery_evidence: Mapping[str, Any],
        now: Optional[datetime] = None,
        session: Optional[Any] = None,
    ) -> MailboxHealthIncident:
        with self._lock:
            current = self.get_incident(incident_id)
            updated = resolve(current, recovery_evidence=recovery_evidence, now=now)
            self._by_id[incident_id] = updated
            key = (current.mailbox_id, current.failure_class)
            if self._open_by_key.get(key) == incident_id:
                del self._open_by_key[key]
            return updated

    def get_incident(self, incident_id: str) -> MailboxHealthIncident:
        try:
            return self._by_id[incident_id]
        except KeyError:
            raise NotFoundError(f"no MailboxHealthIncident with incident_id '{incident_id}'") from None

    def get_open_incident(self, *, mailbox_id: str, failure_class: str) -> Optional[MailboxHealthIncident]:
        existing_id = self._open_by_key.get((mailbox_id, failure_class))
        return self._by_id[existing_id] if existing_id is not None else None

    def list_open_incidents(self) -> list[MailboxHealthIncident]:
        return [i for i in self._by_id.values() if i.status == STATUS_OPEN]


# ---------------------------------------------------------------------
# Pure episode-detection / triggering functions (PID §118.C/D)
# ---------------------------------------------------------------------


def detect_auth_required(mailbox: MailboxSource) -> bool:
    """`AUTH_REQUIRED` — immediate incident on confirmed credential/
    authentication failure (PID §118.D). Mailboxes with `status` other
    than `ACTIVE` are structurally excluded (PID §118.D's own explicit
    requirement) — never trigger an incident."""
    return mailbox.status == "ACTIVE" and mailbox.connection_state == CONNECTION_STATE_AUTH_REQUIRED


def detect_persistent_provider_error(
    most_recent_first_sweep_runs: Sequence[MailboxSweepRun],
    *,
    mailbox_status: str,
    consecutive_threshold: int = PERSISTENT_PROVIDER_ERROR_CONSECUTIVE_THRESHOLD,
) -> bool:
    """`PERSISTENT_PROVIDER_ERROR` — incident after `consecutive_threshold`
    (default 3) consecutive qualifying failed sweep attempts (PID
    §118.D), reusing the classification-job `max_attempts` pattern.
    ``most_recent_first_sweep_runs`` must already be ordered most-recent-
    first (``MailboxSweepRunRepository.list_runs``'s own documented
    ordering) — this function never re-sorts. A qualifying failed
    attempt is a terminal `FAILED` run; a `PARTIAL`/`SUCCEEDED` run
    anywhere within the most recent `consecutive_threshold` breaks the
    streak (it is not three CONSECUTIVE failures). Fewer than
    `consecutive_threshold` runs exist yet -> `False` (never speculatively
    triggered on an incomplete history)."""
    if mailbox_status != "ACTIVE":
        return False
    if len(most_recent_first_sweep_runs) < consecutive_threshold:
        return False
    most_recent = most_recent_first_sweep_runs[:consecutive_threshold]
    return all(run.status == "FAILED" for run in most_recent)


def detect_oauth_reconnect_incomplete(mailbox: MailboxSource, *, pending_state_expired: bool) -> bool:
    """`OAUTH_RECONNECT_INCOMPLETE` — incident after the mailbox's own
    governed pending-OAuth-state expiry elapses with no completion (PID
    §118.D). ``pending_state_expired`` is computed by the caller (the
    evaluator) by reading the real OAuth-state table's own `expires_at`/
    `consumed_at` columns — this pure function only encodes the
    triggering *decision*, never the I/O itself (mirrors every other
    pure predicate in this module)."""
    return mailbox.status == "ACTIVE" and mailbox.connection_state == CONNECTION_STATE_AUTH_REQUIRED and pending_state_expired


def detect_stale_ingestion(*args: Any, **kwargs: Any) -> bool:
    """`STALE_INGESTION` is defined in the taxonomy but its detection
    logic is explicitly NOT implemented in this delivery (PID §118.D/G:
    reserved, inactive, gated behind the not-yet-built recurring-sweep
    workstream). Always raises — a real check is not acceptable here;
    only an explicit stub is (WO §6.A item 2)."""
    raise NotImplementedError(
        "STALE_INGESTION detection is reserved and inactive until a separately governed recurring "
        "mailbox-sweep schedule exists (PID §118.D/G) — this delivery (WO-BAGMAN-118) does not "
        "implement it; do not call this function from any live evaluator path"
    )


# ---------------------------------------------------------------------
# Recovery proof (PID §118.H)
# ---------------------------------------------------------------------


def detect_recovery(
    incident: MailboxHealthIncident,
    mailbox: MailboxSource,
    most_recent_first_sweep_runs: Sequence[MailboxSweepRun],
) -> Optional[dict]:
    """Returns a `recovery_evidence` dict if `incident`'s own per-class
    PID §118.H proof holds, else `None`. Never closes an incident on a
    bare `connection_state == CONNECTED` transition alone, for any class
    (PID §118.H's own explicit requirement) — every class requires a
    REAL subsequent successful sweep (`MailboxSweepRun.status ==
    'SUCCEEDED'`) whose `completed_at` is genuinely after
    `incident.first_observed_at`, proving the mailbox did not merely
    flip a status flag but actually ingested again.

    * `AUTH_REQUIRED` — additionally requires `mailbox.connection_state
      == CONNECTED` (the remedy for this class IS reconnecting).
    * `PERSISTENT_PROVIDER_ERROR` — no connection_state requirement
      (this class was never an authentication failure — PID §118.H).
    * `OAUTH_RECONNECT_INCOMPLETE` — same as `AUTH_REQUIRED`: completion
      of the originally-intended reconnect flow, plus a successful
      sweep.
    """
    successful_runs_after_incident = [
        run
        for run in most_recent_first_sweep_runs
        if run.status == "SUCCEEDED" and run.completed_at is not None and run.completed_at > incident.first_observed_at
    ]
    if not successful_runs_after_incident:
        return None

    proving_run = successful_runs_after_incident[0]

    if incident.failure_class in (FAILURE_CLASS_AUTH_REQUIRED, FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE):
        if mailbox.connection_state != "CONNECTED":
            return None
    elif incident.failure_class != FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR:
        # STALE_INGESTION (or any future class) has no recovery proof
        # defined here — detect_stale_ingestion() is unimplemented, so
        # no incident of that class can exist yet in practice; defensive
        # only.
        return None

    return {
        "sweep_run_id": proving_run.sweep_run_id,
        # `recovery_evidence` is stored as JSONB — a raw `datetime` is
        # not JSON-serializable, so this is rendered as a contract
        # (RFC 3339 / `Z`-suffixed) string, exactly like every other
        # canonical timestamp this codebase persists in a JSON/JSONB
        # field (see core.timestamps.to_contract_string's own module
        # docstring).
        "completed_at": to_contract_string(proving_run.completed_at),
    }
