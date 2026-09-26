"""Per-mailbox sweep exclusivity lease (CD-6 Slice 4).

Judgment call, documented (architect spec's own "your call, document
it" for concurrent-sweep exclusion): a DEDICATED LEASE TABLE
(``mailbox_sweep_locks`` — one row per mailbox, acquired via an INSERT
that either succeeds or hits a unique-violation), rather than a
Postgres ``SELECT ... FOR UPDATE`` row lock on the mailbox's own row.

Why a lease table instead of ``with_for_update()`` on the mailbox row
------------------------------------------------------------------------
``persistence.postgres.mailbox_repository`` already uses
``with_for_update()`` for every mailbox mutation (see
``PostgresMailboxSourceRepository._locked_row``), but that lock is held
only for the duration of ONE database transaction, released the moment
that transaction commits — it cannot represent "a sweep is currently
in progress" across the many separate, short database transactions one
whole sweep attempt performs (create the run row, upsert several
messages, advance a cursor, ...). Using it here would mean either (a)
wrapping an entire sweep — including calls out to the real Microsoft
Graph provider, MIME fetch, and a malware scan — inside one single
long-lived database transaction (holding a row lock across
several-seconds-or-longer network I/O, generally considered poor
practice and a real availability risk for every OTHER caller of that
same row), or (b) accepting that the lock is released long before the
sweep it was meant to guard actually finishes. Neither is acceptable.
A dedicated, short-lived-transaction lease row instead lets "acquire"
and "release" be two fast, independent, ordinary transactions
bracketing the (possibly slow) sweep work in between — exactly the
shape a real mutual-exclusion PRIMITIVE needs, distinct from an
ordinary row-level write lock.

Expiry — the crash-recovery detail
--------------------------------------
A lease also carries an ``expires_at``: if the process holding it
crashes mid-sweep, a stale lease must not wedge every future sweep of
that mailbox forever. :meth:`MailboxSweepLock.acquire` treats an
EXPIRED lease exactly like no lease at all (safely reclaimable) —
never a manual/operator unlock step. The lease duration is generous
enough to comfortably outlast one ordinary sweep (network calls to
Graph + a malware scan per new message) while still being bounded.
"""
from __future__ import annotations

import abc
import threading as _threading
import uuid as _uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Optional

from core.timestamps import utc_now

#: Generous enough for a real sweep (network + scan per new message)
#: while still bounded — a crashed process's lease self-heals within
#: this window rather than needing an operator unlock action.
DEFAULT_LEASE_DURATION_SECONDS: float = 15 * 60.0


class MailboxSweepLockError(Exception):
    """Raised by :meth:`MailboxSweepLock.acquire` when a lease is
    already held (and not expired) for this mailbox — the caller
    (``services/mailbox/sweep.py::run_sweep``) catches this and returns
    a `MailboxSweepRun` with
    `error_code=SweepFailureReason.CONCURRENT_SWEEP_IN_PROGRESS` rather
    than letting a second concurrent sweep attempt run in parallel."""


@dataclass(frozen=True)
class _Lease:
    owner_token: str
    acquired_at: datetime
    expires_at: datetime


class MailboxSweepLock(abc.ABC):
    """Repository-shaped abstraction for the per-mailbox sweep lease."""

    @abc.abstractmethod
    def try_acquire(self, mailbox_id: str, *, lease_seconds: float = DEFAULT_LEASE_DURATION_SECONDS) -> str:
        """Attempt to acquire the lease for `mailbox_id`. Returns an
        opaque `owner_token` string on success.

        Raises:
            MailboxSweepLockError: a non-expired lease is already held
                by someone else.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def release(self, mailbox_id: str, owner_token: str) -> None:
        """Release the lease — a no-op (never raises) if `owner_token`
        does not match the current holder (e.g. this lease already
        expired and was reclaimed by someone else) or no lease exists
        at all; releasing must never itself become a new failure mode
        on the way out of an already-completed sweep."""
        raise NotImplementedError

    @contextmanager
    def held(self, mailbox_id: str, *, lease_seconds: float = DEFAULT_LEASE_DURATION_SECONDS) -> Iterator[None]:
        """Convenience context manager: acquire-or-raise, guaranteed
        release on the way out (success or exception)."""
        token = self.try_acquire(mailbox_id, lease_seconds=lease_seconds)
        try:
            yield
        finally:
            self.release(mailbox_id, token)


class InMemoryMailboxSweepLock(MailboxSweepLock):
    """Narrow in-memory reference implementation — a real
    `threading.Lock`-guarded dict, since a real HTTP-driven caller may
    run across genuine OS threads within one process (same reasoning as
    `services.xero.oauth_state.InMemoryOAuthStateRepository`)."""

    def __init__(self) -> None:
        self._by_mailbox: dict[str, _Lease] = {}
        self._guard = _threading.Lock()

    def try_acquire(self, mailbox_id: str, *, lease_seconds: float = DEFAULT_LEASE_DURATION_SECONDS) -> str:
        now = utc_now()
        with self._guard:
            existing = self._by_mailbox.get(mailbox_id)
            if existing is not None and existing.expires_at > now:
                raise MailboxSweepLockError(
                    f"mailbox '{mailbox_id}' is already being swept (lease held until "
                    f"{existing.expires_at.isoformat()})"
                )
            token = _uuid.uuid4().hex
            self._by_mailbox[mailbox_id] = _Lease(
                owner_token=token, acquired_at=now, expires_at=now + timedelta(seconds=lease_seconds)
            )
            return token

    def release(self, mailbox_id: str, owner_token: str) -> None:
        with self._guard:
            existing = self._by_mailbox.get(mailbox_id)
            if existing is not None and existing.owner_token == owner_token:
                del self._by_mailbox[mailbox_id]
