"""``MailboxSweepRun`` domain object and repository (CD-6 Slice 4).
Mirrors ``services.xero.sync.XeroSyncRun``'s own role for the Xero
domain exactly — a durable, terminal ledger row for one sweep attempt.
See ``services/mailbox/sweep.py`` for the orchestration that creates
and completes these.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

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


@dataclass(frozen=True)
class MailboxSweepRun:
    sweep_run_id: str
    mailbox_id: str
    trigger: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    folders_attempted: Sequence[str] = field(default_factory=tuple)
    messages_seen: int = 0
    messages_new: int = 0
    evidence_created: int = 0
    duplicates: int = 0
    quarantined: int = 0
    failures: int = 0
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
            "folders_attempted": list(self.folders_attempted),
            "messages_seen": self.messages_seen,
            "messages_new": self.messages_new,
            "evidence_created": self.evidence_created,
            "duplicates": self.duplicates,
            "quarantined": self.quarantined,
            "failures": self.failures,
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
        folders_attempted: Sequence[str],
        messages_seen: int,
        messages_new: int,
        evidence_created: int,
        duplicates: int,
        quarantined: int,
        failures: int,
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
        folders_attempted: Sequence[str],
        messages_seen: int,
        messages_new: int,
        evidence_created: int,
        duplicates: int,
        quarantined: int,
        failures: int,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> MailboxSweepRun:
        current = self.get_run(sweep_run_id)
        updated = transition(
            current,
            new_status,
            folders_attempted=tuple(folders_attempted),
            messages_seen=messages_seen,
            messages_new=messages_new,
            evidence_created=evidence_created,
            duplicates=duplicates,
            quarantined=quarantined,
            failures=failures,
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
