"""``MailboxFolderCursor`` — the durable, per-(mailbox, provider, folder)
Graph delta-query cursor (CD-6 Slice 4).

Not a JSON-Schema-contract-backed domain object (a deliberate,
documented scope judgment call — mirrors ``services.xero.oauth_state
.OAuthState``'s own identical decision, see that module's docstring for
the full reasoning this repeats): a cursor is internal sweep-engine
plumbing, never surfaced through a GET endpoint for a human to read
meaningfully (the raw ``delta_link`` is an opaque provider token, not
operator-facing data — see the "opaque cursor doctrine" below), never
displayed in the GUI, never referenced by another canonical object.

Opaque cursor doctrine — read before changing
-------------------------------------------------
``delta_link`` is the FULL ``@odata.deltaLink`` Microsoft Graph returns
at the end of a delta round. This module (and every caller) treats it
as an OPAQUE string: never parsed, never edited, never synthesised,
never logged, never placed in an audit-event payload. It is persisted
verbatim and handed back verbatim to the next delta request. Only
:meth:`MailboxFolderCursorRepository.advance_cursor` may ever replace
it — and only after an entire delta round has succeeded (see
``services/mailbox/sweep.py``'s own module docstring, "Cursor failure
semantics").

``bootstrap_timestamp`` is a SEPARATE, permanently-preserved field
------------------------------------------------------------------------
The 7-day first-sync bootstrap boundary (architect spec) is recorded
here, independently of ``delta_link``, specifically so a later governed
historical backfill can know "why older mail was never imported"
without destroying the live forward cursor — advancing ``delta_link``
via :meth:`advance_cursor` never touches ``bootstrap_timestamp``.
"""
from __future__ import annotations

import abc
import threading as _threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.errors import NotFoundError
from core.timestamps import utc_now


@dataclass(frozen=True)
class MailboxFolderCursor:
    mailbox_id: str
    provider_kind: str
    folder: str
    #: The full, opaque `@odata.deltaLink` from the most recent
    #: successful delta round. `None` before the first successful
    #: round has ever completed for this folder (a fresh bootstrap
    #: query is used instead — see `services/mailbox/sweep.py`).
    delta_link: Optional[str]
    #: The `receivedDateTime` boundary applied on THIS folder's very
    #: first (bootstrap) delta query — permanent, never touched again
    #: (see module docstring).
    bootstrap_timestamp: datetime
    created_at: datetime
    updated_at: datetime


class MailboxFolderCursorRepository(abc.ABC):
    @abc.abstractmethod
    def get_or_bootstrap(
        self, *, mailbox_id: str, provider_kind: str, folder: str, bootstrap_timestamp: datetime
    ) -> MailboxFolderCursor:
        """Resolve-or-create: an existing cursor for (mailbox_id,
        provider_kind, folder) is returned unchanged (its own, ORIGINAL
        `bootstrap_timestamp` is preserved — the caller's
        `bootstrap_timestamp` argument is only used the first time a
        cursor is created for this folder). A genuinely new cursor is
        created with `delta_link=None`."""
        raise NotImplementedError

    @abc.abstractmethod
    def advance_cursor(
        self, *, mailbox_id: str, provider_kind: str, folder: str, delta_link: str
    ) -> MailboxFolderCursor:
        """Replace `delta_link` — called ONLY after an entire delta
        round has succeeded (see module docstring's "Opaque cursor
        doctrine"). Raises `core.errors.NotFoundError` if no cursor
        exists yet for this folder (a caller must always
        `get_or_bootstrap` first)."""
        raise NotImplementedError


class InMemoryMailboxFolderCursorRepository(MailboxFolderCursorRepository):
    """Narrow in-memory reference implementation. Guarded by a real
    lock (mirrors `services.xero.oauth_state.InMemoryOAuthStateRepository`'s
    own reasoning — a real HTTP-driven caller may run across genuine OS
    threads even within one process)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], MailboxFolderCursor] = {}
        self._lock = _threading.Lock()

    def get_or_bootstrap(
        self, *, mailbox_id: str, provider_kind: str, folder: str, bootstrap_timestamp: datetime
    ) -> MailboxFolderCursor:
        key = (mailbox_id, provider_kind, folder)
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return existing
            now = utc_now()
            created = MailboxFolderCursor(
                mailbox_id=mailbox_id,
                provider_kind=provider_kind,
                folder=folder,
                delta_link=None,
                bootstrap_timestamp=bootstrap_timestamp,
                created_at=now,
                updated_at=now,
            )
            self._by_key[key] = created
            return created

    def advance_cursor(
        self, *, mailbox_id: str, provider_kind: str, folder: str, delta_link: str
    ) -> MailboxFolderCursor:
        key = (mailbox_id, provider_kind, folder)
        with self._lock:
            current = self._by_key.get(key)
            if current is None:
                raise NotFoundError(
                    f"no MailboxFolderCursor for mailbox_id={mailbox_id!r} folder={folder!r} — "
                    "get_or_bootstrap must be called before advance_cursor"
                )
            updated = MailboxFolderCursor(
                mailbox_id=current.mailbox_id,
                provider_kind=current.provider_kind,
                folder=current.folder,
                delta_link=delta_link,
                bootstrap_timestamp=current.bootstrap_timestamp,
                created_at=current.created_at,
                updated_at=utc_now(),
            )
            self._by_key[key] = updated
            return updated
