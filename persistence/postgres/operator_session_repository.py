"""``PostgresSessionRepository`` — durable implementation of
``core.auth.SessionRepository`` (WO-BAGMAN-117 §6.A.1, PID §117.C).

Implements the exact same abstract interface as
``core.auth.InMemorySessionRepository`` — same method signatures, same
return types, same canonical-error behaviour on the same conditions —
mirroring ``persistence.postgres.entity_repository.PostgresEntityRepository``'s
own module docstring/shape precisely.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from core.auth import SessionRecord, SessionRepository
from core.errors import PersistenceError
from core.timestamps import utc_now
from persistence.postgres.operator_session_models import OperatorSessionRow
from persistence.postgres.session import get_engine, session_scope


def _row_to_session(row: OperatorSessionRow) -> SessionRecord:
    return SessionRecord(
        token_digest=row.token_digest,
        csrf_token=row.csrf_token,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


class PostgresSessionRepository(SessionRepository):
    """PostgreSQL-backed ``SessionRepository``. Holds no in-process
    state of its own — every method reads/writes the database directly
    (via a fresh ``Session`` per call, mirroring
    ``PostgresEntityRepository``'s own discipline), so durability across
    restarts or across a brand-new instance of this class is inherent."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

    def create_session(
        self,
        *,
        token_digest: str,
        csrf_token: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        row = OperatorSessionRow(
            token_digest=token_digest,
            csrf_token=csrf_token,
            created_at=created_at,
            expires_at=expires_at,
            revoked_at=None,
        )
        try:
            with session_scope(self._engine) as session:
                session.add(row)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not create operator session: {exc}") from exc
        return _row_to_session(row)

    def get_by_token_digest(self, token_digest: str) -> Optional[SessionRecord]:
        try:
            with session_scope(self._engine) as session:
                row = session.get(OperatorSessionRow, token_digest)
                return _row_to_session(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read operator session: {exc}") from exc

    def revoke_session(self, token_digest: str) -> None:
        try:
            with session_scope(self._engine) as session:
                row = session.get(OperatorSessionRow, token_digest, with_for_update=True)
                # Idempotent, never raises for an unknown or
                # already-revoked token_digest (core.auth.SessionRepository's
                # own abstract contract) — logout must always succeed
                # from the caller's perspective.
                if row is None or row.revoked_at is not None:
                    return
                row.revoked_at = utc_now()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not revoke operator session: {exc}") from exc

    def revoke_all_sessions(self, *, now: Optional[datetime] = None) -> int:
        resolved_now = now if now is not None else utc_now()
        try:
            with session_scope(self._engine) as session:
                result = session.execute(
                    update(OperatorSessionRow)
                    .where(OperatorSessionRow.revoked_at.is_(None))
                    .values(revoked_at=resolved_now)
                )
                return result.rowcount or 0
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not revoke all operator sessions: {exc}") from exc
