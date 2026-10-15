"""PostgreSQL durability proof for ``PostgresSessionRepository``
(WO-BAGMAN-117 §6.A.1/§6.A.7, PID §117.C) — mirrors
``tests/persistence/test_entity_persistence.py``'s own "a brand-new
repository instance, on its own fresh connection pool" durability
discipline exactly.

Runs against the real, disposable PostgreSQL container
``tests/persistence/conftest.py`` already starts for this directory
(``postgres_container``/``fresh_engine`` fixtures) — never mocked,
never SQLite.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.auth import generate_csrf_token, generate_session_token, hash_token
from core.timestamps import utc_now
from persistence.postgres.operator_session_repository import PostgresSessionRepository


def test_session_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresSessionRepository()
    now = utc_now()
    raw_token = generate_session_token()
    digest = hash_token(raw_token)
    csrf_token = generate_csrf_token()

    created = repo.create_session(
        token_digest=digest, csrf_token=csrf_token, created_at=now, expires_at=now + timedelta(hours=1)
    )

    # A brand-new repository instance, on its own fresh connection pool
    # (not the same Python object, not even the same engine) — the real
    # durability proof, not in-process object identity.
    fresh_repo = PostgresSessionRepository(engine=fresh_engine)
    fetched = fresh_repo.get_by_token_digest(digest)

    assert fetched == created
    assert fetched.csrf_token == csrf_token
    assert fetched.revoked_at is None
    assert fetched.created_at.tzinfo is not None
    assert fetched.expires_at.tzinfo is not None


def test_the_raw_token_is_never_persisted_only_its_digest(fresh_engine):
    """PID §117.C: 'Only a one-way SHA-256 digest of the token is
    persisted ... never the raw token.' Proves this directly against
    the real database row, not merely by code inspection."""
    import sqlalchemy as sa

    from persistence.postgres.session import get_engine

    repo = PostgresSessionRepository()
    now = utc_now()
    raw_token = generate_session_token()
    digest = hash_token(raw_token)
    repo.create_session(
        token_digest=digest, csrf_token=generate_csrf_token(), created_at=now, expires_at=now + timedelta(hours=1)
    )

    with get_engine().connect() as conn:
        row = conn.execute(
            sa.text("SELECT token_digest FROM operator_sessions WHERE token_digest = :digest"),
            {"digest": digest},
        ).one()

    assert row.token_digest == digest
    assert row.token_digest != raw_token
    # The raw token must not appear anywhere in the persisted row's own
    # column value (it never has its own column to begin with — this
    # double-checks the digest column itself was never accidentally
    # given the raw value instead).
    assert raw_token not in row.token_digest


def test_get_by_token_digest_returns_none_for_an_unknown_digest(fresh_engine):
    repo = PostgresSessionRepository(engine=fresh_engine)
    assert repo.get_by_token_digest("0" * 64) is None


def test_revoke_session_persists_across_a_fresh_repository_instance(fresh_engine):
    repo = PostgresSessionRepository()
    now = utc_now()
    raw_token = generate_session_token()
    digest = hash_token(raw_token)
    repo.create_session(
        token_digest=digest, csrf_token=generate_csrf_token(), created_at=now, expires_at=now + timedelta(hours=1)
    )

    repo.revoke_session(digest)

    fresh_repo = PostgresSessionRepository(engine=fresh_engine)
    fetched = fresh_repo.get_by_token_digest(digest)
    assert fetched.revoked_at is not None


def test_revoke_session_is_idempotent_for_an_unknown_digest(fresh_engine):
    repo = PostgresSessionRepository(engine=fresh_engine)
    repo.revoke_session("digest-that-was-never-created")  # must not raise


def test_revoke_all_sessions_revokes_every_active_session_durably(fresh_engine):
    repo = PostgresSessionRepository()
    now = utc_now()
    digests = []
    for _ in range(3):
        raw_token = generate_session_token()
        digest = hash_token(raw_token)
        repo.create_session(
            token_digest=digest, csrf_token=generate_csrf_token(), created_at=now, expires_at=now + timedelta(hours=1)
        )
        digests.append(digest)
    repo.revoke_session(digests[0])  # already revoked beforehand

    revoked_count = repo.revoke_all_sessions(now=now)
    assert revoked_count == 2

    fresh_repo = PostgresSessionRepository(engine=fresh_engine)
    for digest in digests:
        assert fresh_repo.get_by_token_digest(digest).revoked_at is not None


def test_create_session_with_an_unreachable_engine_raises_persistence_error(fresh_engine):
    """PID §117.C fail-closed proof: a genuine connectivity failure
    surfaces as `core.errors.PersistenceError` (-> HTTP 503 at the HTTP
    layer), never a silent success/allow."""
    from sqlalchemy import create_engine

    from core.errors import PersistenceError

    # A syntactically valid but entirely unreachable Postgres endpoint
    # — proves the real exception-translation path, not merely a typed
    # assertion with no real I/O behind it.
    broken_engine = create_engine(
        "postgresql+psycopg://bagman:wrong@127.0.0.1:1/bagman_unreachable",
        pool_pre_ping=False,
    )
    repo = PostgresSessionRepository(engine=broken_engine)
    now = utc_now()

    try:
        with pytest.raises(PersistenceError):
            repo.create_session(
                token_digest=hash_token(generate_session_token()),
                csrf_token=generate_csrf_token(),
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
    finally:
        broken_engine.dispose()
