"""``MailboxSweepRun`` domain object and repository (CD-6 Slice 4).
Mirrors ``services.xero.sync.XeroSyncRun``'s own role for the Xero
domain exactly — a durable, terminal ledger row for one sweep attempt.
See ``services/mailbox/sweep.py`` for the orchestration that creates
and completes these.

Stale-`RUNNING` recovery (Slice 3/4/5 governance-reconciliation delta)
------------------------------------------------------------------------
A real, independently-confirmed gap (found unanimously by parallel
audits of this subsystem): unlike this exact codebase's own established
pattern for an abandoned in-flight record (`ai.invocation
.is_stale_running`/`recover_stale_invocation`, `ai.jobs.is_stale_claim`/
`recover_stale_claim`), a `MailboxSweepRun` that reaches `RUNNING` had no
path back to a terminal state if the process running `run_sweep` were
killed (OOM, crash, forced restart) before it could call `complete_run`
itself. `services.mailbox.lock.MailboxSweepLock`'s own lease
(`DEFAULT_LEASE_DURATION_SECONDS`, 15 minutes) already self-heals so a
NEW sweep can proceed — but the OLD `MailboxSweepRun` row stayed
`RUNNING` forever, with no audit event and no reconciliation code path
anywhere, permanently corrupting the durable "prove exactly what
happened" ledger PID §98.8 requires, even though sweeping itself was
never actually blocked.

Fixed the same way the sibling patterns above already do it: a shared
predicate (:func:`is_stale_running`) and a shared recovery transition
(:func:`recover_stale_run`), invoked lazily by both concrete
repositories at the one point staleness matters — `run_sweep` calling
`create_run` for a mailbox whose PRIOR run is still (falsely) `RUNNING`
(see `services/mailbox/sweep.py::run_sweep`'s own call site). A
recovered run always targets `FAILED` (never `SUCCEEDED`/`PARTIAL`) —
`ALLOWED_TRANSITIONS["RUNNING"]` has no dedicated `TIMED_OUT` status the
way `AIInvocation` does, and reporting an abandoned run as anything but
an honest failure would misrepresent what actually happened.

This delta does not add a new audit-event emission for the recovery
itself (a deliberate, bounded choice — see the reconciliation record in
`PID.md` for why): the recovered row's own `error_code`/`error_detail`
already make the ledger honest again (the actual defect being fixed),
and adding a new audit-event mechanism here would require new
`audit_repository` composition wiring this bounded governance delta
does not otherwise need.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional, Sequence

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.mailbox.lock import DEFAULT_LEASE_DURATION_SECONDS

_SCHEMA = "mailbox/bagman.mailbox_sweep_run.v1.schema.json"
SCHEMA_VERSION = "bagman.mailbox_sweep_run.v1"

TRIGGER_MANUAL = "MANUAL"
TRIGGER_SCHEDULED = "SCHEDULED"
TRIGGERS = frozenset({TRIGGER_MANUAL, TRIGGER_SCHEDULED})

STATUSES = frozenset({"RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "RUNNING": frozenset({"SUCCEEDED", "PARTIAL", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "PARTIAL": frozenset(),
    "FAILED": frozenset(),
}


class SweepFailureReason:
    """Closed-vocabulary strings for `MailboxSweepRun.error_code` (not
    an enum.Enum — kept as plain string constants so the contract's own
    `error_code` field can stay an open string, matching
    `services.xero.sync.SyncFailureReason`'s sibling role but without
    forcing every caller to `.value` an enum member)."""

    NOT_CONNECTED = "NOT_CONNECTED"
    CONFIG_ERROR = "CONFIG_ERROR"
    TOKEN_REFRESH_FAILED = "TOKEN_REFRESH_FAILED"
    AUTH_REVOKED = "AUTH_REVOKED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    RESYNC_REQUIRED = "RESYNC_REQUIRED"
    PARTIAL_FAILURES = "PARTIAL_FAILURES"
    CONCURRENT_SWEEP_IN_PROGRESS = "CONCURRENT_SWEEP_IN_PROGRESS"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    #: An exception outside the closed vocabulary above escaped the
    #: sweep loop's own known-failure handling (see the final
    #: `except Exception` clause in `services/mailbox/sweep.py::run_sweep`).
    #: Distinct from `PERSISTENCE_ERROR`, which is reserved for
    #: `core.errors.PersistenceError` specifically.
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
    #: CD-6 architect amendment (recursive folder discovery) — the
    #: mailbox's own folder tree/well-known-folder resolution itself
    #: could not be completed this round (a whole-sweep precondition
    #: failure, distinct from a single folder's own delta-page failure
    #: — see `services/mailbox/sweep.py`'s own module docstring).
    FOLDER_DISCOVERY_FAILED = "FOLDER_DISCOVERY_FAILED"
    #: Slice 3/4/5 governance-reconciliation delta — stamped on a
    #: `MailboxSweepRun` discovered still `RUNNING` long after the
    #: process that would have completed it is gone (see module
    #: docstring's "Stale-`RUNNING` recovery" section). Distinct from
    #: every other code above: this is never set by `run_sweep` itself,
    #: only by :func:`recover_stale_run`.
    STALE_RECOVERY_TIMEOUT = "STALE_RECOVERY_TIMEOUT"


#: Bounded, deterministic stale-`RUNNING` threshold (module docstring's
#: "Stale-`RUNNING` recovery" section) — twice
#: `services.mailbox.lock.DEFAULT_LEASE_DURATION_SECONDS` (15 minutes),
#: so a run is only ever reconciled once its own `MailboxSweepLock`
#: lease has DEFINITELY already expired and could have been reclaimed
#: by a different attempt — comfortable headroom against the lock's own
#: expiry, never a race between "the lock says available" and "the run
#: still looks legitimately in flight".
STALE_RUNNING_THRESHOLD_SECONDS: float = DEFAULT_LEASE_DURATION_SECONDS * 2


@dataclass(frozen=True)
class MailboxSweepRun:
    sweep_run_id: str
    mailbox_id: str
    trigger: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    #: CD-6 architect amendment (recursive folder discovery) — each
    #: entry is a small {"folder_id": ..., "display_name": ...} mapping
    #: (never a raw folder id alone, never a display name alone — see
    #: this delivery's own contract description for why).
    folders_attempted: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    messages_seen: int = 0
    messages_new: int = 0
    evidence_created: int = 0
    duplicates: int = 0
    quarantined: int = 0
    failures: int = 0
    #: Operational-addendum aggregate reporting fields (CD-6 architect
    #: operational addendum ahead of the first real large historical
    #: sweep) — see `services/mailbox/sweep.py`'s own module docstring
    #: for exactly where/how each of these is computed. All default to
    #: `0` so a run built/completed before this addendum (or a test
    #: exercising a narrower slice of `complete_run`) still validates.
    unique_sender_domains: int = 0
    allowed_domain_messages: int = 0
    ignored_domain_messages: int = 0
    unknown_domain_messages: int = 0
    likely_financial_candidates: int = 0
    messages_with_attachments: int = 0
    graph_throttle_retries: int = 0
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "sweep_run_id": self.sweep_run_id,
            "mailbox_id": self.mailbox_id,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": to_contract_string(self.started_at),
            "completed_at": to_contract_string(self.completed_at) if self.completed_at is not None else None,
            "folders_attempted": [dict(f) for f in self.folders_attempted],
            "messages_seen": self.messages_seen,
            "messages_new": self.messages_new,
            "evidence_created": self.evidence_created,
            "duplicates": self.duplicates,
            "quarantined": self.quarantined,
            "failures": self.failures,
            "unique_sender_domains": self.unique_sender_domains,
            "allowed_domain_messages": self.allowed_domain_messages,
            "ignored_domain_messages": self.ignored_domain_messages,
            "unknown_domain_messages": self.unknown_domain_messages,
            "likely_financial_candidates": self.likely_financial_candidates,
            "messages_with_attachments": self.messages_with_attachments,
            "graph_throttle_retries": self.graph_throttle_retries,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "schema_version": self.schema_version,
        }


def transition(run: MailboxSweepRun, new_status: str, **field_updates) -> MailboxSweepRun:
    allowed = ALLOWED_TRANSITIONS.get(run.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"MailboxSweepRun '{run.sweep_run_id}' cannot transition from '{run.status}' to "
            f"'{new_status}'; allowed transitions from '{run.status}' are {sorted(allowed) or '(none)'}"
        )
    completed_at = utc_now() if new_status in TERMINAL_STATUSES else run.completed_at
    try:
        updated = dataclasses.replace(run, status=new_status, completed_at=completed_at, **field_updates)
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition MailboxSweepRun: {exc}") from exc
    return updated


def is_stale_running(run: MailboxSweepRun, *, now: Optional[datetime] = None) -> bool:
    """True if ``run`` is still `RUNNING` and has been sitting there
    longer than :data:`STALE_RUNNING_THRESHOLD_SECONDS` — the shared
    predicate both concrete repositories use (module docstring's
    "Stale-`RUNNING` recovery" section), mirroring
    `ai.invocation.is_stale_running`'s own identical role exactly.

    ``now`` is injectable purely for deterministic testing (construct a
    run with an artificially old `started_at` and call this directly,
    or freeze `now` instead of sleeping for real) — defaults to
    :func:`core.timestamps.utc_now`.
    """
    if run.status != "RUNNING":
        return False
    now = now if now is not None else utc_now()
    return (now - run.started_at).total_seconds() > STALE_RUNNING_THRESHOLD_SECONDS


def recover_stale_run(run: MailboxSweepRun) -> MailboxSweepRun:
    """Return the terminal `MailboxSweepRun` a stale, abandoned ``run``
    (see :func:`is_stale_running`) is recovered into — always `FAILED`
    (never `SUCCEEDED`/`PARTIAL`: an abandoned run must never be
    reported as anything but an honest failure — module docstring's
    "Stale-`RUNNING` recovery" section). Every other field
    (`messages_seen`/`messages_new`/... ) is left exactly as it was at
    the moment this run went stale — never fabricated to look like a
    completed sweep.

    Does not itself persist anything — callers (the two concrete
    repositories) are responsible for that.
    """
    return transition(
        run,
        "FAILED",
        error_code=SweepFailureReason.STALE_RECOVERY_TIMEOUT,
        error_detail=(
            f"recovered by the bounded stale-RUNNING backstop: started_at was older than "
            f"STALE_RUNNING_THRESHOLD_SECONDS ({STALE_RUNNING_THRESHOLD_SECONDS}s) with no terminal "
            "transition ever recorded — the process that would have completed this sweep is gone"
        ),
    )


class MailboxSweepRunRepository(abc.ABC):
    @abc.abstractmethod
    def create_run(self, *, mailbox_id: str, trigger: str) -> MailboxSweepRun:
        raise NotImplementedError

    @abc.abstractmethod
    def complete_run(
        self,
        sweep_run_id: str,
        *,
        new_status: str,
        folders_attempted: Sequence[Mapping[str, str]],
        messages_seen: int,
        messages_new: int,
        evidence_created: int,
        duplicates: int,
        quarantined: int,
        failures: int,
        unique_sender_domains: int = 0,
        allowed_domain_messages: int = 0,
        ignored_domain_messages: int = 0,
        unknown_domain_messages: int = 0,
        likely_financial_candidates: int = 0,
        messages_with_attachments: int = 0,
        graph_throttle_retries: int = 0,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> MailboxSweepRun:
        raise NotImplementedError

    @abc.abstractmethod
    def get_run(self, sweep_run_id: str) -> MailboxSweepRun:
        raise NotImplementedError

    @abc.abstractmethod
    def list_runs(self, *, mailbox_id: str, limit: Optional[int] = None) -> list[MailboxSweepRun]:
        """Most-recent-first, scoped to `mailbox_id`."""
        raise NotImplementedError

    @abc.abstractmethod
    def recover_stale_runs(
        self,
        *,
        mailbox_id: str,
        staleness_threshold_seconds: float = STALE_RUNNING_THRESHOLD_SECONDS,
        now: Optional[datetime] = None,
    ) -> list[MailboxSweepRun]:
        """Scan every `RUNNING` run for `mailbox_id` and recover any
        that is stale per :func:`is_stale_running`, using
        :func:`recover_stale_run` (module docstring's "Stale-`RUNNING`
        recovery" section). Called lazily by
        `services.mailbox.sweep.run_sweep`, right before it creates a
        new run for this same mailbox — never as a scheduled/background
        poll. Returns every recovered run (empty list if none were
        stale) — never raises for "nothing to recover"."""
        raise NotImplementedError


class InMemoryMailboxSweepRunRepository(MailboxSweepRunRepository):
    def __init__(self) -> None:
        self._by_id: dict[str, MailboxSweepRun] = {}

    def create_run(self, *, mailbox_id: str, trigger: str) -> MailboxSweepRun:
        try:
            candidate = MailboxSweepRun(
                sweep_run_id=identity.generate_id(),
                mailbox_id=mailbox_id,
                trigger=trigger,
                status="RUNNING",
                started_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create MailboxSweepRun: {exc}") from exc
        self._by_id[candidate.sweep_run_id] = candidate
        return candidate

    def complete_run(
        self,
        sweep_run_id: str,
        *,
        new_status: str,
        folders_attempted: Sequence[Mapping[str, str]],
        messages_seen: int,
        messages_new: int,
        evidence_created: int,
        duplicates: int,
        quarantined: int,
        failures: int,
        unique_sender_domains: int = 0,
        allowed_domain_messages: int = 0,
        ignored_domain_messages: int = 0,
        unknown_domain_messages: int = 0,
        likely_financial_candidates: int = 0,
        messages_with_attachments: int = 0,
        graph_throttle_retries: int = 0,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> MailboxSweepRun:
        current = self.get_run(sweep_run_id)
        updated = transition(
            current,
            new_status,
            folders_attempted=tuple(dict(f) for f in folders_attempted),
            messages_seen=messages_seen,
            messages_new=messages_new,
            evidence_created=evidence_created,
            duplicates=duplicates,
            quarantined=quarantined,
            failures=failures,
            unique_sender_domains=unique_sender_domains,
            allowed_domain_messages=allowed_domain_messages,
            ignored_domain_messages=ignored_domain_messages,
            unknown_domain_messages=unknown_domain_messages,
            likely_financial_candidates=likely_financial_candidates,
            messages_with_attachments=messages_with_attachments,
            graph_throttle_retries=graph_throttle_retries,
            error_code=error_code,
            error_detail=error_detail,
        )
        self._by_id[sweep_run_id] = updated
        return updated

    def get_run(self, sweep_run_id: str) -> MailboxSweepRun:
        try:
            return self._by_id[sweep_run_id]
        except KeyError:
            raise NotFoundError(f"no MailboxSweepRun with sweep_run_id '{sweep_run_id}'") from None

    def list_runs(self, *, mailbox_id: str, limit: Optional[int] = None) -> list[MailboxSweepRun]:
        runs = sorted(
            (r for r in self._by_id.values() if r.mailbox_id == mailbox_id),
            key=lambda r: r.started_at,
            reverse=True,
        )
        return runs if limit is None else runs[:limit]

    def recover_stale_runs(
        self,
        *,
        mailbox_id: str,
        staleness_threshold_seconds: float = STALE_RUNNING_THRESHOLD_SECONDS,
        now: Optional[datetime] = None,
    ) -> list[MailboxSweepRun]:
        now = now if now is not None else utc_now()
        recovered: list[MailboxSweepRun] = []
        for run in list(self._by_id.values()):
            if run.mailbox_id != mailbox_id:
                continue
            if not is_stale_running(run, now=now):
                continue
            updated = recover_stale_run(run)
            self._by_id[updated.sweep_run_id] = updated
            recovered.append(updated)
        return recovered
