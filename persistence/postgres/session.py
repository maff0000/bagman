"""Engine/session factory for BAGMAN's PostgreSQL persistence layer
(CD-3 WI-1, PID §29-30).

Connection configuration is read from environment variables; the
database password is never a literal in this code — it is always read
from a file, whose path is itself given by an environment variable
(``BAGMAN_DB_PASSWORD_FILE``), matching PID §30's secret-file doctrine
(e.g. a Docker/Compose secret mounted at ``/run/secrets/...``).

``alembic/env.py`` imports :func:`get_database_url` from this module
rather than duplicating any connection-string logic of its own (PL
instruction) — this module is the single place that knows how to build
BAGMAN's PostgreSQL connection URL.
"""
from __future__ import annotations

import functools
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

#: Sensible local-dev defaults only — never used for a real password
#: value, only for host/port/dbname/user and the *path* to the secret
#: file. None of these defaults is itself a credential.
_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = "5432"
_DEFAULT_DBNAME = "bagman"
_DEFAULT_USER = "bagman"
_DEFAULT_PASSWORD_FILE = "/run/secrets/bagman_db_password"


def _read_password(password_file: str) -> str:
    """Read the database password from a file — never a literal in
    code (PID §30). Raises a plain, descriptive error (not a canonical
    BAGMAN error: this is startup/config wiring, before any repository
    or domain code exists to catch it) if the file cannot be read.
    """
    path = Path(password_file)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            "could not read the BAGMAN database password from "
            f"BAGMAN_DB_PASSWORD_FILE={password_file!r}: {exc}. BAGMAN never "
            "hardcodes a database password; point BAGMAN_DB_PASSWORD_FILE at "
            "a readable file containing the password (e.g. a mounted Docker/"
            "Compose secret, or a throwaway file for local/test use)."
        ) from exc


def get_database_url() -> str:
    """Build BAGMAN's PostgreSQL connection URL (``postgresql+psycopg://...``)
    from environment variables plus the password file they point to.

    Read on every call (not cached) so a test/dev process that points
    at a different database (different env vars) between calls always
    gets the URL matching its *current* environment — the URL itself
    is not memoized, only the SQLAlchemy ``Engine`` built from it (see
    :func:`get_engine`).
    """
    host = os.environ.get("BAGMAN_DB_HOST", _DEFAULT_HOST)
    port = os.environ.get("BAGMAN_DB_PORT", _DEFAULT_PORT)
    dbname = os.environ.get("BAGMAN_DB_NAME", _DEFAULT_DBNAME)
    user = os.environ.get("BAGMAN_DB_USER", _DEFAULT_USER)
    password_file = os.environ.get("BAGMAN_DB_PASSWORD_FILE", _DEFAULT_PASSWORD_FILE)

    password = _read_password(password_file)

    return (
        f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(dbname)}"
    )


@functools.lru_cache(maxsize=None)
def _engine_for_url(url: str) -> Engine:
    """One pooled ``Engine`` per distinct connection URL, cached for
    the life of the process. ``pool_pre_ping`` ensures a stale/dropped
    connection is detected and replaced rather than silently reused."""
    return create_engine(url, pool_pre_ping=True, future=True)


def get_engine() -> Engine:
    """Return the (process-wide, cached-by-URL) SQLAlchemy ``Engine``
    for BAGMAN's current PostgreSQL configuration.

    Caching is keyed on the connection URL itself, so a process that
    changes its environment (e.g. a test pointing at a different
    disposable database) between calls transparently gets a distinct
    engine/connection pool — never a stale one from an earlier
    configuration.
    """
    return _engine_for_url(get_database_url())


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to ``engine`` (default: the
    current :func:`get_engine`)."""
    return sessionmaker(bind=engine or get_engine(), autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Open a ``Session`` as an explicit transaction (PID §55): commits
    on clean exit, rolls back and re-raises on any exception, always
    closes. Used by repository methods that must perform more than one
    write as a single atomic database transaction (e.g.
    ``PostgresEvidenceRepository.register_evidence`` writing both the
    ``EvidenceItem`` row and its self-sufficient-idempotency
    ``ExternalReference`` row together).
    """
    session = get_sessionmaker(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
