"""PostgreSQL-backed implementation of the Gmail-adapter mailbox-scoped
OAuth CSRF-state repository (CD-6 GUI-operations-foundation follow-on
WO — Gmail OAuth state persistence correction).

Mirrors
`persistence/postgres/mailbox_microsoft_repository.py::PostgresMailboxMicrosoftOAuthStateRepository`
exactly: "fresh Session per call, `with_for_update` row-locking on
`mark_consumed` (never trust a pre-lock read alone — the exact race
this discipline closes), translate constraint violations, never let a
raw SQLAlchemy exception escape" — the SAME already-proven algorithm
applied to Gmail's identically-shaped `GmailOAuthState`
domain object. See `services.mailbox.gmail.oauth_state`'s own module
docstring for why this is a deliberate, documented duplication of
Microsoft's implementation rather than a shared abstraction.

This is the production-only persistence correction: the just-built
Gmail mailbox provider used `InMemoryGmailOAuthStateRepository` even
in `app/api/composition.py::_build_production()`, which does not
survive a container restart/deploy mid-OAuth-flow — inconsistent with
Microsoft's own Postgres-backed precedent. Development/test
composition keeps using `InMemoryGmailOAuthStateRepository` unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.errors import NotFoundError, OAuthStateError, PersistenceError
from core.timestamps import utc_now
from persistence.postgres.mailbox_gmail_models import MailboxGmailOAuthStateRow
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.gmail.oauth_state import GmailOAuthState, GmailOAuthStateRepository


class PostgresMailboxGmailOAuthStateRepository(GmailOAuthStateRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_state(self, *, mailbox_id: str) -> GmailOAuthState:
        from services.mailbox.gmail.oauth_state import STATE_TTL_SECONDS, generate_state_value

        now = utc_now()
        candidate = GmailOAuthState(
            state=generate_state_value(), mailbox_id=mailbox_id, created_at=now,
            expires_at=now + timedelta(seconds=STATE_TTL_SECONDS), consumed_at=None,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(
                    MailboxGmailOAuthStateRow(
                        state=candidate.state, mailbox_id=candidate.mailbox_id, created_at=candidate.created_at,
                        expires_at=candidate.expires_at, consumed_at=None,
                    )
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create GmailOAuthState: {exc}") from exc
        return candidate

    def get_state(self, state: str) -> Optional[GmailOAuthState]:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxGmailOAuthStateRow, state)
                if row is None:
                    return None
                return GmailOAuthState(
                    state=row.state, mailbox_id=row.mailbox_id, created_at=row.created_at,
                    expires_at=row.expires_at, consumed_at=row.consumed_at,
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read GmailOAuthState: {exc}") from exc

    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> GmailOAuthState:
        resolved_now = now if now is not None else utc_now()
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxGmailOAuthStateRow, state, with_for_update=True)
                if row is None:
                    raise NotFoundError(f"no GmailOAuthState with state '{state}'")
                if row.consumed_at is not None:
                    raise OAuthStateError(
                        f"Gmail OAuth state value was already consumed at {row.consumed_at.isoformat()} — "
                        "refusing a replayed callback"
                    )
                if resolved_now > row.expires_at:
                    raise OAuthStateError(
                        f"Gmail OAuth state value expired at {row.expires_at.isoformat()} "
                        f"(now {resolved_now.isoformat()}) — refusing a stale callback"
                    )
                row.consumed_at = resolved_now
                return GmailOAuthState(
                    state=row.state, mailbox_id=row.mailbox_id, created_at=row.created_at,
                    expires_at=row.expires_at, consumed_at=row.consumed_at,
                )
        except (NotFoundError, OAuthStateError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not consume GmailOAuthState: {exc}") from exc
