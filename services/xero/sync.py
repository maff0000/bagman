"""``XeroSyncRun`` domain object/repository, and :func:`run_sync` — the
real, idempotent, atomic-at-the-projection-level Chart-of-Accounts sync
orchestration (CD-6 Slice 2, PID §98.4, architect spec §4/§5/§20).

Atomicity guarantee (architect spec §5/§20 — "a failed/partial sync
must never replace last-known-good data with an empty or partial set")
------------------------------------------------------------------------
Every account fetched successfully is upserted via
``services.xero.account.XeroAccountRepository.upsert_account`` — but
the `XeroSyncRun` row is only marked `SUCCEEDED` once EVERY fetched
account has been upserted without error; a failure partway through
marks the run `FAILED` and stops. The Postgres implementation
(`persistence.postgres.xero_repository.PostgresXeroSyncOrchestrator` —
see that module) wraps the whole "upsert every account + mark the run
SUCCEEDED" sequence in ONE database transaction, so a mid-sync database
failure rolls back every upsert from THIS run while leaving every
account row from the PREVIOUS successful run completely untouched —
literally "last-known-good is never replaced by an empty/partial set",
not merely "we try to update it that way". The in-memory implementation
below achieves the same effective guarantee for a single-process test
context by only calling `upsert_account` from inside this function
after `list_accounts()` has ALREADY returned a complete, successful
result — no partial write is ever attempted at all when the fetch
itself failed.

Failure taxonomy (architect spec §4 — "handles: token expiry..., refresh
failure..., 401/403..., rate limiting..., transient provider errors...,
malformed provider responses..., partial sync failure, database
failure")
------------------------------------------------------------------------
See :class:`SyncFailureReason` for the closed set `XeroSyncRun.error_code`
is drawn from, and :func:`run_sync`'s own docstring for exactly which
Xero-side condition maps to which reason AND whether it also changes the
owning `XeroConnection.status` (most do not — see
`services.xero.connection`'s own module docstring for why only a
refresh failure or a 401/403 change connection-level state).
"""
from __future__ import annotations

import abc
import dataclasses
import enum
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.xero.account import RawXeroAccount, XeroAccountRepository
from services.xero.client import (
    XeroAccountingClientProtocol,
    XeroOAuthClientProtocol,
    XeroOutcomeStatus,
)
from services.xero.connection import XeroConnection, XeroConnectionRepository
from services.xero.secrets import TokenStoreProtocol

_SCHEMA = "xero/bagman.xero_sync_run.v1.schema.json"
SCHEMA_VERSION = "bagman.xero_sync_run.v1"

STATUSES = frozenset({"RUNNING", "SUCCEEDED", "FAILED"})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "RUNNING": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
}

#: A run this old, still `RUNNING`, is presumed abandoned by a dead
#: process (the same class of gap `ai.invocation`'s own stale-`RUNNING`
#: backstop exists for) — used only by `is_stale`'s own staleness test
#: for the GUI/operator's benefit, never auto-recovered in this slice
#: (this slice's own sync attempts are short, synchronous HTTP calls
#: bounded by `client.py`'s own per-request timeout — there is no
#: plausible long-lived background-worker scenario here the way
#: Ask BAGMAN's subprocess-backed invocations have; a genuinely stuck
#: `RUNNING` row would mean the `bagman-api` process itself died
#: mid-request, which the NEXT sync attempt's own fresh `XeroSyncRun`
#: row supersedes naturally without needing a recovery backstop).
STALE_RUN_THRESHOLD_SECONDS: float = 120.0

#: How long since `last_successful_sync_at` before reference data is
#: considered STALE for GUI/operator purposes (architect spec §29 —
#: "stale sync state calculation... a clear, documented threshold,
#: mirroring the reliability delta's own `STALE_RUNNING_THRESHOLD_SECONDS`
#: precedent"). 24 hours: a Chart of Accounts changes rarely (new
#: accounts are typically added deliberately, not multiple times a
#: day) — this is generous enough to never nag an operator over
#: ordinary day-to-day timing, while still catching "this company's
#: sync has been silently broken for a long time" within one business
#: day.
REFERENCE_DATA_STALE_THRESHOLD_SECONDS: float = 24 * 60 * 60.0


class SyncFailureReason(str, enum.Enum):
    """The closed set `XeroSyncRun.error_code` is drawn from on
    `FAILED` — see module docstring's "Failure taxonomy"."""

    NOT_CONNECTED = "NOT_CONNECTED"
    CONFIG_ERROR = "CONFIG_ERROR"
    TOKEN_REFRESH_FAILED = "TOKEN_REFRESH_FAILED"
    AUTH_REVOKED = "AUTH_REVOKED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"


@dataclass(frozen=True)
class XeroSyncRun:
    sync_run_id: str
    entity_id: str
    tenant_id: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    accounts_seen_count: Optional[int] = None
    accounts_created_count: Optional[int] = None
    accounts_updated_count: Optional[int] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "sync_run_id": self.sync_run_id,
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "started_at": to_contract_string(self.started_at),
            "completed_at": to_contract_string(self.completed_at) if self.completed_at is not None else None,
            "accounts_seen_count": self.accounts_seen_count,
            "accounts_created_count": self.accounts_created_count,
            "accounts_updated_count": self.accounts_updated_count,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "schema_version": self.schema_version,
        }


def is_stale_run(run: XeroSyncRun, *, now: Optional[datetime] = None) -> bool:
    if run.status != "RUNNING":
        return False
    now = now if now is not None else utc_now()
    return (now - run.started_at).total_seconds() > STALE_RUN_THRESHOLD_SECONDS


def is_reference_data_stale(connection: XeroConnection, *, now: Optional[datetime] = None) -> bool:
    """True if `connection` has never successfully synced, or its last
    successful sync is older than
    :data:`REFERENCE_DATA_STALE_THRESHOLD_SECONDS`. A `PENDING`/
    `DISCONNECTED`/`REVOKED`/`ERROR` connection with no successful sync
    yet is also reported stale (there is no reference data at all to
    trust) — this function does not special-case `status`; a caller
    that only cares about `CONNECTED` connections filters before
    calling."""
    if connection.last_successful_sync_at is None:
        return True
    now = now if now is not None else utc_now()
    return (now - connection.last_successful_sync_at).total_seconds() > REFERENCE_DATA_STALE_THRESHOLD_SECONDS


def transition(run: XeroSyncRun, new_status: str, **field_updates) -> XeroSyncRun:
    allowed = ALLOWED_TRANSITIONS.get(run.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"XeroSyncRun '{run.sync_run_id}' cannot transition from '{run.status}' to "
            f"'{new_status}'; allowed transitions from '{run.status}' are {sorted(allowed) or '(none)'}"
        )
    completed_at = utc_now() if new_status in TERMINAL_STATUSES else run.completed_at
    try:
        updated = dataclasses.replace(run, status=new_status, completed_at=completed_at, **field_updates)
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition XeroSyncRun: {exc}") from exc
    return updated


class XeroSyncRunRepository(abc.ABC):
    """Repository abstraction for XeroSyncRun."""

    @abc.abstractmethod
    def create_run(self, *, entity_id: str, tenant_id: str) -> XeroSyncRun:
        raise NotImplementedError

    @abc.abstractmethod
    def succeed_run(
        self,
        sync_run_id: str,
        *,
        accounts_seen_count: int,
        accounts_created_count: int,
        accounts_updated_count: int,
    ) -> XeroSyncRun:
        raise NotImplementedError

    @abc.abstractmethod
    def fail_run(self, sync_run_id: str, *, error_code: str, error_detail: str) -> XeroSyncRun:
        raise NotImplementedError

    @abc.abstractmethod
    def get_run(self, sync_run_id: str) -> XeroSyncRun:
        raise NotImplementedError

    @abc.abstractmethod
    def list_runs(self, *, entity_id: str, limit: Optional[int] = None) -> list[XeroSyncRun]:
        """Most-recent-first, scoped to `entity_id` (same "required
        filter" cross-company-isolation discipline as
        `XeroAccountRepository.list_accounts` — see that module)."""
        raise NotImplementedError


class InMemoryXeroSyncRunRepository(XeroSyncRunRepository):
    def __init__(self) -> None:
        self._by_id: dict[str, XeroSyncRun] = {}

    def create_run(self, *, entity_id: str, tenant_id: str) -> XeroSyncRun:
        try:
            candidate = XeroSyncRun(
                sync_run_id=identity.generate_id(),
                entity_id=entity_id,
                tenant_id=tenant_id,
                status="RUNNING",
                started_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroSyncRun: {exc}") from exc
        self._by_id[candidate.sync_run_id] = candidate
        return candidate

    def succeed_run(
        self,
        sync_run_id: str,
        *,
        accounts_seen_count: int,
        accounts_created_count: int,
        accounts_updated_count: int,
    ) -> XeroSyncRun:
        current = self.get_run(sync_run_id)
        updated = transition(
            current,
            "SUCCEEDED",
            accounts_seen_count=accounts_seen_count,
            accounts_created_count=accounts_created_count,
            accounts_updated_count=accounts_updated_count,
        )
        self._by_id[sync_run_id] = updated
        return updated

    def fail_run(self, sync_run_id: str, *, error_code: str, error_detail: str) -> XeroSyncRun:
        current = self.get_run(sync_run_id)
        updated = transition(current, "FAILED", error_code=error_code, error_detail=error_detail)
        self._by_id[sync_run_id] = updated
        return updated

    def get_run(self, sync_run_id: str) -> XeroSyncRun:
        try:
            return self._by_id[sync_run_id]
        except KeyError:
            raise NotFoundError(f"no XeroSyncRun with sync_run_id '{sync_run_id}'") from None

    def list_runs(self, *, entity_id: str, limit: Optional[int] = None) -> list[XeroSyncRun]:
        runs = sorted(
            (r for r in self._by_id.values() if r.entity_id == entity_id),
            key=lambda r: r.started_at,
            reverse=True,
        )
        return runs if limit is None else runs[:limit]


#: How long before `token_expires_at` this module pre-emptively
#: refreshes rather than waiting for a live 401 (a sensible safety
#: margin against clock skew/request latency — refreshing a few minutes
#: early is always safe; Xero's own refresh-token grant has no
#: meaningful "too early" penalty).
_REFRESH_SKEW_SECONDS = 120.0

#: Bounded retry-once policy for a rate-limited `/Accounts` call
#: (architect spec §4: "respect `Retry-After`... back off sensibly");
#: never more than this even if Xero's own `Retry-After` asks for
#: longer — a sync attempt must have a bounded worst-case duration.
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 30.0


def run_sync(
    *,
    entity_id: str,
    connection_repository: XeroConnectionRepository,
    account_repository: XeroAccountRepository,
    sync_run_repository: XeroSyncRunRepository,
    accounting_client: XeroAccountingClientProtocol,
    oauth_client: XeroOAuthClientProtocol,
    token_store: TokenStoreProtocol,
    sleep_fn=time.sleep,
) -> XeroSyncRun:
    """Run exactly one sync attempt for `entity_id`'s current
    `XeroConnection`. Always returns a terminal `XeroSyncRun` — never
    raises for an ordinary provider/auth/rate-limit/malformed-response
    failure (all of those are `FAILED` runs, per module docstring's
    "Failure taxonomy"); raises only for a genuine caller-error
    precondition (`entity_id` has no connection at all, or its
    connection is not `CONNECTED`) via `core.errors.ConflictError`,
    since there is no sync run to even create in that case — a router
    calling this is expected to have already checked
    `GET /internal/xero/{entity_id}` and not offer 'Sync now' at all
    when disconnected (architect spec §16's GUI honesty requirement);
    this is a defensive backstop, not the primary UX guard.

    Sequence (see module docstring for the atomicity/failure-taxonomy
    reasoning behind each step):

    1. Resolve the entity's `CONNECTED` connection; stamp
       `last_attempted_sync_at` immediately (so a stuck/crashed attempt
       is still observable even if nothing further completes).
    2. Read stored tokens; refresh pre-emptively if within
       `_REFRESH_SKEW_SECONDS` of `expires_at` (architect spec §4:
       "token expiry (auto-refresh, retried once)").
    3. Call `GET /Accounts`. If it reports `AUTH_ERROR` despite a
       token this module believed was fresh, attempt exactly ONE
       reactive refresh + ONE retry before concluding access is
       genuinely revoked (`REVOKED`) rather than merely stale.
    4. On `RATE_LIMITED`, back off (bounded, see
       `_MAX_RATE_LIMIT_BACKOFF_SECONDS`) and retry exactly once.
    5. On success, upsert every returned account and mark the run
       `SUCCEEDED`; on any other outcome, mark the run `FAILED` with
       the matching `SyncFailureReason` — the existing `XeroAccount`
       projection is never touched on a `FAILED` run.
    """
    connection = connection_repository.get_by_entity(entity_id)
    if connection is None or connection.status != "CONNECTED" or not connection.tenant_id:
        raise ConflictError(
            f"entity '{entity_id}' has no CONNECTED XeroConnection — cannot sync "
            "(the caller should not offer 'Sync now' in this state)"
        )

    connection_repository.record_sync_attempt(connection.xero_connection_id)
    run = sync_run_repository.create_run(entity_id=entity_id, tenant_id=connection.tenant_id)

    tokens = token_store.read(entity_id)
    if tokens is None:
        return sync_run_repository.fail_run(
            run.sync_run_id,
            error_code=SyncFailureReason.CONFIG_ERROR.value,
            error_detail="no stored OAuth tokens for this connection",
        )

    now = utc_now()
    if (tokens.expires_at - now).total_seconds() <= _REFRESH_SKEW_SECONDS:
        refreshed = oauth_client.refresh(refresh_token=tokens.refresh_token)
        if refreshed.status != XeroOutcomeStatus.OK or refreshed.tokens is None:
            connection_repository.fail_refresh(
                connection.xero_connection_id,
                error_detail=refreshed.error_detail or "token refresh failed",
            )
            return sync_run_repository.fail_run(
                run.sync_run_id,
                error_code=SyncFailureReason.TOKEN_REFRESH_FAILED.value,
                error_detail=refreshed.error_detail or "token refresh failed",
            )
        token_store.write(
            entity_id,
            access_token=refreshed.tokens.access_token,
            refresh_token=refreshed.tokens.refresh_token,
            expires_at=refreshed.tokens.expires_at,
        )
        tokens = token_store.read(entity_id)

    result = accounting_client.list_accounts(tenant_id=connection.tenant_id, access_token=tokens.access_token)

    if result.status == XeroOutcomeStatus.AUTH_ERROR:
        # Reactive path: a token this module believed was fresh was
        # rejected anyway — one refresh + one retry before concluding
        # REVOKED (see docstring, step 3).
        refreshed = oauth_client.refresh(refresh_token=tokens.refresh_token)
        if refreshed.status != XeroOutcomeStatus.OK or refreshed.tokens is None:
            connection_repository.fail_refresh(
                connection.xero_connection_id,
                error_detail=refreshed.error_detail or "token refresh failed after a reactive 401/403",
            )
            return sync_run_repository.fail_run(
                run.sync_run_id,
                error_code=SyncFailureReason.TOKEN_REFRESH_FAILED.value,
                error_detail=refreshed.error_detail or "token refresh failed after a reactive 401/403",
            )
        token_store.write(
            entity_id,
            access_token=refreshed.tokens.access_token,
            refresh_token=refreshed.tokens.refresh_token,
            expires_at=refreshed.tokens.expires_at,
        )
        result = accounting_client.list_accounts(
            tenant_id=connection.tenant_id, access_token=refreshed.tokens.access_token
        )
        if result.status == XeroOutcomeStatus.AUTH_ERROR:
            connection_repository.fail_auth_revoked(
                connection.xero_connection_id,
                error_detail=result.error_detail or "401/403 persisted after a successful token refresh",
            )
            return sync_run_repository.fail_run(
                run.sync_run_id,
                error_code=SyncFailureReason.AUTH_REVOKED.value,
                error_detail=result.error_detail or "401/403 persisted after a successful token refresh",
            )

    if result.status == XeroOutcomeStatus.RATE_LIMITED:
        backoff = min(result.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
        sleep_fn(backoff)
        result = accounting_client.list_accounts(tenant_id=connection.tenant_id, access_token=tokens.access_token)
        if result.status == XeroOutcomeStatus.RATE_LIMITED:
            return sync_run_repository.fail_run(
                run.sync_run_id,
                error_code=SyncFailureReason.RATE_LIMITED.value,
                error_detail=result.error_detail or "rate limited after one bounded retry",
            )

    if result.status == XeroOutcomeStatus.MALFORMED_RESPONSE:
        return sync_run_repository.fail_run(
            run.sync_run_id,
            error_code=SyncFailureReason.MALFORMED_RESPONSE.value,
            error_detail=result.error_detail or "malformed /Accounts response",
        )

    if result.status != XeroOutcomeStatus.OK:
        return sync_run_repository.fail_run(
            run.sync_run_id,
            error_code=SyncFailureReason.PROVIDER_ERROR.value,
            error_detail=result.error_detail or f"provider outcome {result.status.value}",
        )

    try:
        # ONE call, ONE atomic unit — see
        # `XeroAccountRepository.upsert_accounts`'s own docstring for
        # the all-or-nothing guarantee this relies on (module
        # docstring's "Atomicity guarantee").
        created, updated = account_repository.upsert_accounts(
            entity_id=entity_id,
            tenant_id=connection.tenant_id,
            raws=result.accounts,
            sync_run_id=run.sync_run_id,
        )
    except Exception as exc:  # noqa: BLE001 - a partial-write failure is a FAILED run, never a crash
        return sync_run_repository.fail_run(
            run.sync_run_id,
            error_code=SyncFailureReason.PERSISTENCE_ERROR.value,
            error_detail=str(exc)[:500],
        )

    succeeded = sync_run_repository.succeed_run(
        run.sync_run_id,
        accounts_seen_count=len(result.accounts),
        accounts_created_count=created,
        accounts_updated_count=updated,
    )
    connection_repository.record_sync_success(
        connection.xero_connection_id, tenant_name=connection.tenant_name, token_expires_at=tokens.expires_at
    )
    return succeeded
