"""``MailboxHealthNotification`` — the durable notification outbox (PID
§118.E, WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.1.A/B/C).

Structurally mirrors ``core.entity.GovernedEntity``/``services.evidence
.classification_job.EvidenceClassificationJob``'s shape (frozen
dataclass + repository ABC + narrow in-memory reference implementation).
The in-memory implementation below reproduces the EXACT same
claim-generation-fencing / exhaustion-boundary / dual-predicate-
completion-fencing semantics the real PostgreSQL implementation
(``persistence.postgres.mailbox_health_repository
.PostgresMailboxHealthNotificationRepository``) proves under a genuine
database-level race — see that module's own docstring for the real SQL
this in-memory version is a same-process-only Python proxy for.

Corrected exhaustion boundary (WO §6.A.1.B — do not reintroduce the
"would reach" variant a prior audit round already found and fixed)
------------------------------------------------------------------------
A claim is permitted — and ``attempt_count`` incremented — whenever the
row's CURRENT ``attempt_count`` is STRICTLY LESS THAN ``max_attempts``
(``attempt_count < max_attempts``, checked *before* incrementing); only
once ``attempt_count`` has already REACHED ``max_attempts`` does the row
transition directly to ``TERMINAL_FAILURE`` instead of being claimed
again. For ``max_attempts=3``, claims 1, 2, AND 3 are all genuinely
permitted before exhaustion is declared — never one fewer (the
previously-found-defective "attempt_count reaches/would reach
max_attempts" wording would have silently refused the third, legitimately
-allowed claim).

``max_attempts`` governs claimed work cycles, not guaranteed provider
calls (WO §6.A.1.B)
------------------------------------------------------------------------
A claim can itself be abandoned (a worker crash) at any point after
``attempt_count`` is incremented but before the provider HTTP call is
ever issued — this design guarantees at most ``max_attempts`` claimed
work cycles, never a guarantee of that many real provider calls.
"""
from __future__ import annotations

import abc
import dataclasses
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from core import identity
from core.errors import NotFoundError, ValidationError
from core.timestamps import utc_now

PURPOSE_INITIAL = "INITIAL"
PURPOSE_REMINDER_24H = "REMINDER_24H"
PURPOSE_RECOVERY = "RECOVERY"
PURPOSES = frozenset({PURPOSE_INITIAL, PURPOSE_REMINDER_24H, PURPOSE_RECOVERY})

STATE_PENDING = "PENDING"
STATE_IN_PROGRESS = "IN_PROGRESS"
STATE_ACCEPTED = "ACCEPTED"
STATE_RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
STATE_TERMINAL_FAILURE = "TERMINAL_FAILURE"
STATE_CANCELLED = "CANCELLED"
STATES = frozenset(
    {STATE_PENDING, STATE_IN_PROGRESS, STATE_ACCEPTED, STATE_RETRYABLE_FAILURE, STATE_TERMINAL_FAILURE, STATE_CANCELLED}
)
#: States a not-yet-sent notification may sit in when the pre-dispatch
#: suppression check (PID §118.E.1.C) can still intercept it before an
#: actual Pushover call. `IN_PROGRESS` is included because the recheck
#: happens AFTER the claim (the worker is already `IN_PROGRESS` when it
#: re-reads the parent incident's status, immediately before dialing
#: Pushover) — see WO §6.A.1.C.
PRE_DISPATCH_SUPPRESSIBLE_STATES = frozenset({STATE_PENDING, STATE_RETRYABLE_FAILURE, STATE_IN_PROGRESS})
#: States the claim predicate ever picks up — `CANCELLED`/`ACCEPTED`/
#: `TERMINAL_FAILURE` are all terminal; a `RECOVERY` notification is
#: never subject to pre-dispatch suppression (PID §118.E.1.C), but it
#: still flows through the identical claim/completion machinery.
_CLAIMABLE_DUE_STATES = (STATE_PENDING, STATE_RETRYABLE_FAILURE)

DEFAULT_MAX_ATTEMPTS = 3
#: Governs `REMINDER_24H`'s own "no reminder unless the prior
#: notification for this incident completed >= 24h ago" invariant (PID
#: §118.E.2) — a plain, documented constant, never re-derived ad hoc at
#: each call site.
REMINDER_MIN_INTERVAL_SECONDS: float = 24 * 60 * 60

#: Bounded exponential backoff (documented judgment call, mirroring the
#: shared bounded-backoff shape already proven in
#: `services/mailbox/sweep.py`/`services/xero/sync.py`/
#: `services/xero/client.py` — none of those modules' own exact
#: constants apply to an operator-alert cadence, so this delivery picks
#: its own bounded schedule: 1 minute, doubling, capped at 1 hour).
_BACKOFF_BASE_SECONDS = 60.0
_BACKOFF_CAP_SECONDS = 3600.0


def compute_next_attempt_at(attempt_count: int, *, now: Optional[datetime] = None) -> datetime:
    """Bounded exponential backoff keyed on the notification's own
    (already-incremented) `attempt_count` — see module docstring."""
    now = now if now is not None else utc_now()
    delay_seconds = min(_BACKOFF_BASE_SECONDS * (2 ** max(attempt_count - 1, 0)), _BACKOFF_CAP_SECONDS)
    return now + timedelta(seconds=delay_seconds)


@dataclass(frozen=True)
class MailboxHealthNotification:
    notification_id: str
    incident_id: str
    purpose: str
    state: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    created_at: datetime
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    claim_generation: int = 0
    completed_at: Optional[datetime] = None
    provider_response_metadata: Optional[Mapping[str, Any]] = None
    cancellation_reason: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> dict:
        """Plain-dict rendering for logging/reporting — no JSON Schema
        contract governs this shape (no external HTTP-facing surface in
        Stage A)."""
        return {
            "notification_id": self.notification_id,
            "incident_id": self.incident_id,
            "purpose": self.purpose,
            "state": self.state,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "next_attempt_at": self.next_attempt_at,
            "created_at": self.created_at,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "claim_generation": self.claim_generation,
            "completed_at": self.completed_at,
            "provider_response_metadata": (
                dict(self.provider_response_metadata) if self.provider_response_metadata is not None else None
            ),
            "cancellation_reason": dict(self.cancellation_reason) if self.cancellation_reason is not None else None,
        }


@dataclass(frozen=True)
class NotificationCreationResult:
    notification: MailboxHealthNotification
    created: bool


class MailboxHealthNotificationRepository(abc.ABC):
    """Repository abstraction for `MailboxHealthNotification` (PID
    §118.E)."""

    @abc.abstractmethod
    def create_notification(
        self,
        *,
        incident_id: str,
        purpose: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: Optional[datetime] = None,
        session: Optional[Any] = None,
    ) -> NotificationCreationResult:
        """Idempotent, concurrency-safe create-or-get on the real
        `(incident_id, purpose)` unique constraint (WO §6.A.1.A): each
        purpose occurs at most once, ever, per incident. A concurrent
        creator racing for the same pair resolves to exactly one row —
        both callers receive it, `created` distinguishing which one
        actually inserted it. `session` is the §118.F shared-transaction
        extension (see
        `services.mailbox.health.incident.MailboxHealthIncidentRepository
        .create_incident`'s own docstring)."""
        raise NotImplementedError

    @abc.abstractmethod
    def claim_due_or_expired(
        self,
        *,
        limit: int,
        claimed_by: str,
        claim_timeout_seconds: float,
        now: Optional[datetime] = None,
    ) -> list[MailboxHealthNotification]:
        """Atomically claim up to `limit` rows matching the PID §118.E.1
        claim predicate (due `PENDING`/`RETRYABLE_FAILURE` work, and
        expired `IN_PROGRESS` work alike), incrementing `attempt_count`
        and `claim_generation` in the SAME update — but only for rows
        whose CURRENT `attempt_count < max_attempts` (WO §6.A.1.B's
        corrected exhaustion boundary). A row that already matches the
        claim predicate but has `attempt_count >= max_attempts` is
        transitioned directly to `TERMINAL_FAILURE` instead (this method
        performs that exhaustion pass first, unconditionally, before the
        bounded claim pass) — never claimed again, never silently
        ignored.

        Returns fewer than `limit` rows (including zero) if fewer are
        genuinely claimable — never raises for "nothing to claim"."""
        raise NotImplementedError

    @abc.abstractmethod
    def complete_claim(
        self,
        notification_id: str,
        *,
        claim_generation: int,
        outcome: str,
        next_attempt_at: Optional[datetime] = None,
        provider_response_metadata: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[MailboxHealthNotification]:
        """The fenced completion write (PID §118.E.1 + WO §6.A.1.B's
        dual-predicate correction): applies only if the row is still
        `IN_PROGRESS` AND still carries `claim_generation ==
        claim_generation` — if a later reclaim has already superseded
        this worker's own claim, this write affects zero rows and
        returns `None` (a correct, silent no-op; the caller must NOT
        treat this as an error). `outcome` is one of `ACCEPTED`/
        `RETRYABLE_FAILURE`/`TERMINAL_FAILURE`; `RETRYABLE_FAILURE`
        requires `next_attempt_at` (the caller's own backoff
        computation, e.g. :func:`compute_next_attempt_at`)."""
        raise NotImplementedError

    @abc.abstractmethod
    def cancel_claimed(
        self,
        notification_id: str,
        *,
        claim_generation: int,
        cancellation_reason: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> Optional[MailboxHealthNotification]:
        """Pre-dispatch suppression write (WO §6.A.1.C): `IN_PROGRESS ->
        CANCELLED`, fenced identically to :meth:`complete_claim`. Returns
        `None` (a correct, silent no-op) if the claim was already
        superseded — never raises."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_notification(self, notification_id: str) -> MailboxHealthNotification:
        raise NotImplementedError

    @abc.abstractmethod
    def list_by_incident(self, incident_id: str) -> list[MailboxHealthNotification]:
        raise NotImplementedError

    @abc.abstractmethod
    def get_latest_completed(self, incident_id: str) -> Optional[MailboxHealthNotification]:
        """The most recently `completed_at`-stamped notification (any
        purpose, any terminal outcome) for `incident_id` — the input to
        PID §118.E.2's `REMINDER_24H` gating invariant ("no reminder
        unless the prior notification for this incident completed >= 24h
        ago"). `None` if no notification for this incident has ever
        completed."""
        raise NotImplementedError


class InMemoryMailboxHealthNotificationRepository(MailboxHealthNotificationRepository):
    """Narrow in-memory reference implementation (PID §21 Option A). A
    single `threading.Lock` reproduces the exact same claim-generation-
    fencing / exhaustion / dual-predicate-completion semantics the real
    PostgreSQL implementation proves under a genuine race — this is a
    same-process-only proxy (see module docstring)."""

    def __init__(self) -> None:
        self._by_id: dict[str, MailboxHealthNotification] = {}
        self._by_incident_purpose: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def create_notification(
        self,
        *,
        incident_id: str,
        purpose: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: Optional[datetime] = None,
        session: Optional[Any] = None,
    ) -> NotificationCreationResult:
        if purpose not in PURPOSES:
            raise ValidationError(f"'{purpose}' is not a governed purpose — must be one of {sorted(PURPOSES)}")
        with self._lock:
            key = (incident_id, purpose)
            existing_id = self._by_incident_purpose.get(key)
            if existing_id is not None:
                return NotificationCreationResult(notification=self._by_id[existing_id], created=False)

            resolved_now = now if now is not None else utc_now()
            candidate = MailboxHealthNotification(
                notification_id=identity.generate_id(),
                incident_id=incident_id,
                purpose=purpose,
                state=STATE_PENDING,
                attempt_count=0,
                max_attempts=max_attempts,
                next_attempt_at=resolved_now,
                created_at=resolved_now,
            )
            self._by_id[candidate.notification_id] = candidate
            self._by_incident_purpose[key] = candidate.notification_id
            return NotificationCreationResult(notification=candidate, created=True)

    def _is_claim_due(self, row: MailboxHealthNotification, *, now: datetime, claim_timeout_seconds: float) -> bool:
        if row.state in _CLAIMABLE_DUE_STATES and row.next_attempt_at <= now:
            return True
        if row.state == STATE_IN_PROGRESS and row.claimed_at is not None:
            return (now - row.claimed_at).total_seconds() > claim_timeout_seconds
        return False

    def claim_due_or_expired(
        self,
        *,
        limit: int,
        claimed_by: str,
        claim_timeout_seconds: float,
        now: Optional[datetime] = None,
    ) -> list[MailboxHealthNotification]:
        if limit <= 0:
            return []
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            # Exhaustion pass FIRST, unconditionally (WO §6.A.1.B) — a
            # due/expired row with no budget left is terminated, never
            # claimed again.
            for row in list(self._by_id.values()):
                if self._is_claim_due(row, now=resolved_now, claim_timeout_seconds=claim_timeout_seconds) and row.attempt_count >= row.max_attempts:
                    updated = dataclasses.replace(row, state=STATE_TERMINAL_FAILURE, completed_at=resolved_now)
                    self._by_id[row.notification_id] = updated

            candidates = sorted(
                (
                    row
                    for row in self._by_id.values()
                    if self._is_claim_due(row, now=resolved_now, claim_timeout_seconds=claim_timeout_seconds)
                    and row.attempt_count < row.max_attempts
                ),
                key=lambda r: (r.next_attempt_at, r.notification_id),
            )
            claimed: list[MailboxHealthNotification] = []
            for row in candidates[:limit]:
                updated = dataclasses.replace(
                    row,
                    state=STATE_IN_PROGRESS,
                    claimed_by=claimed_by,
                    claimed_at=resolved_now,
                    claim_generation=row.claim_generation + 1,
                    attempt_count=row.attempt_count + 1,
                )
                self._by_id[row.notification_id] = updated
                claimed.append(updated)
            return claimed

    def complete_claim(
        self,
        notification_id: str,
        *,
        claim_generation: int,
        outcome: str,
        next_attempt_at: Optional[datetime] = None,
        provider_response_metadata: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[MailboxHealthNotification]:
        if outcome not in (STATE_ACCEPTED, STATE_RETRYABLE_FAILURE, STATE_TERMINAL_FAILURE):
            raise ValidationError(f"'{outcome}' is not a valid completion outcome")
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            row = self._by_id.get(notification_id)
            if row is None:
                raise NotFoundError(f"no MailboxHealthNotification with notification_id '{notification_id}'")
            # The dual predicate (WO §6.A.1.B): state must still be
            # IN_PROGRESS AND claim_generation must still match — a
            # stale/superseded worker's write affects nothing.
            if row.state != STATE_IN_PROGRESS or row.claim_generation != claim_generation:
                return None

            field_updates: dict[str, Any] = {
                "state": outcome,
                "provider_response_metadata": dict(provider_response_metadata) if provider_response_metadata is not None else None,
            }
            if outcome == STATE_RETRYABLE_FAILURE:
                if next_attempt_at is None:
                    raise ValidationError("next_attempt_at is required when outcome is RETRYABLE_FAILURE")
                field_updates["next_attempt_at"] = next_attempt_at
            else:
                field_updates["completed_at"] = resolved_now

            updated = dataclasses.replace(row, **field_updates)
            self._by_id[notification_id] = updated
            return updated

    def cancel_claimed(
        self,
        notification_id: str,
        *,
        claim_generation: int,
        cancellation_reason: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> Optional[MailboxHealthNotification]:
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            row = self._by_id.get(notification_id)
            if row is None:
                raise NotFoundError(f"no MailboxHealthNotification with notification_id '{notification_id}'")
            if row.state != STATE_IN_PROGRESS or row.claim_generation != claim_generation:
                return None
            updated = dataclasses.replace(
                row, state=STATE_CANCELLED, completed_at=resolved_now, cancellation_reason=dict(cancellation_reason)
            )
            self._by_id[notification_id] = updated
            return updated

    def get_notification(self, notification_id: str) -> MailboxHealthNotification:
        try:
            return self._by_id[notification_id]
        except KeyError:
            raise NotFoundError(f"no MailboxHealthNotification with notification_id '{notification_id}'") from None

    def list_by_incident(self, incident_id: str) -> list[MailboxHealthNotification]:
        return [row for row in self._by_id.values() if row.incident_id == incident_id]

    def get_latest_completed(self, incident_id: str) -> Optional[MailboxHealthNotification]:
        completed = [
            row for row in self._by_id.values() if row.incident_id == incident_id and row.completed_at is not None
        ]
        if not completed:
            return None
        return max(completed, key=lambda r: (r.completed_at, r.notification_id))


def reminder_is_due(latest_completed: Optional[MailboxHealthNotification], *, now: Optional[datetime] = None) -> bool:
    """PID §118.E.2's own `REMINDER_24H` gating invariant: no reminder
    unless the prior notification for this incident (any purpose)
    completed >= 24 hours ago. `latest_completed=None` (no prior
    notification has ever completed yet — e.g. the `INITIAL` one is
    still `PENDING`/`IN_PROGRESS`) means NOT due — a reminder is only
    ever meaningful once something else has already finished."""
    if latest_completed is None or latest_completed.completed_at is None:
        return False
    now = now if now is not None else utc_now()
    return (now - latest_completed.completed_at).total_seconds() >= REMINDER_MIN_INTERVAL_SECONDS
