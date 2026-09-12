"""Shared fixtures for `tests/persistence/` (PID §42/§63).

Every test in this directory runs against a REAL disposable PostgreSQL
container this session starts and tears down itself — never mocks,
never SQLite, never an in-memory substitute. This is deliberate: the
whole point of CD-3 WI-1 is proving durability against an actual
PostgreSQL server, including its real constraint enforcement (unique/
foreign-key violations translated by
`persistence/postgres/db_errors.py`).

Container/credentials
----------------------
`bagman-test-postgres-wi1` — unmistakably a disposable test container
(Forge Engineer contract rule 8), started fresh for this test session
and removed (verified removed) at teardown. Credentials are generated
fresh in a throwaway temp file for this session only — never read from
or written to `/srv/bagman-secrets/` (out of bounds for this work item)
and never a literal in any committed file.

Bound only to `127.0.0.1:55432` (not `0.0.0.0`) — host port 5432 was
free at authoring time but is deliberately avoided in favour of an
unambiguous, unlikely-to-collide port, consistent with "port 9000 is
already occupied by something else on this host" caution for the wider
environment.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from persistence.postgres import session as pg_session

CONTAINER_NAME = "bagman-test-postgres-wi1"
IMAGE = "postgres:17"
HOST = "127.0.0.1"
HOST_PORT = "55432"
DB_NAME = "bagman_test"
DB_USER = "bagman_test"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The six canonical tables this work item's migration must create.
CANONICAL_TABLES = (
    "audit_events",
    "governed_entities",
    "sources",
    "evidence_items",
    "external_references",
    "provenance",
)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _wait_for_postgres(timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_result = None
    while time.monotonic() < deadline:
        last_result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "pg_isready", "-U", DB_USER, "-d", DB_NAME],
            capture_output=True,
            text=True,
        )
        if last_result.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"{CONTAINER_NAME} did not become ready within {timeout_s}s: "
        f"{last_result.stdout if last_result else '<no attempt>'} "
        f"{last_result.stderr if last_result else ''}"
    )


@pytest.fixture(scope="session")
def postgres_container(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Start a disposable, throwaway-credentialed PostgreSQL container
    for the life of this test session, run the real `alembic upgrade
    head` against it (no manual SQL — PID §42/§63), then stop and
    remove the container at teardown (verifying it is actually gone).
    """
    # Defensive: remove any stale container left by a prior interrupted
    # run before starting a fresh one.
    _docker("rm", "-f", CONTAINER_NAME, check=False)

    password = secrets.token_urlsafe(24)
    password_dir = tmp_path_factory.mktemp("bagman-test-db-secret")
    password_file = password_dir / "password"
    password_file.write_text(password, encoding="utf-8")
    password_file.chmod(0o600)

    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-e",
        f"POSTGRES_USER={DB_USER}",
        "-e",
        f"POSTGRES_PASSWORD={password}",
        "-e",
        f"POSTGRES_DB={DB_NAME}",
        "-p",
        f"{HOST}:{HOST_PORT}:5432",
        IMAGE,
    )

    os.environ["BAGMAN_DB_HOST"] = HOST
    os.environ["BAGMAN_DB_PORT"] = HOST_PORT
    os.environ["BAGMAN_DB_NAME"] = DB_NAME
    os.environ["BAGMAN_DB_USER"] = DB_USER
    os.environ["BAGMAN_DB_PASSWORD_FILE"] = str(password_file)

    try:
        _wait_for_postgres()

        # The real acceptance proof (PID §42/§63): a clean, empty
        # Postgres database reaches current schema via `alembic upgrade
        # head` alone — no manual SQL anywhere in this fixture.
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(alembic_cfg, "head")

        yield
    finally:
        # Drop this process's pooled connections before removing the
        # container so nothing holds a stale socket open.
        pg_session._engine_for_url.cache_clear()  # noqa: SLF001 - test-only cleanup
        _docker("rm", "-f", CONTAINER_NAME, check=False)


@pytest.fixture(autouse=True)
def _clean_tables(postgres_container: None) -> Iterator[None]:
    """Truncate every canonical table before each test so tests in
    this directory are independent of each other despite sharing one
    container/database for the whole session."""
    engine = pg_session.get_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {', '.join(CANONICAL_TABLES)} CASCADE"))
    yield


@pytest.fixture
def fresh_engine(postgres_container: None) -> Iterator[Engine]:
    """A brand-new SQLAlchemy `Engine` — its own fresh connection pool,
    built directly via `create_engine`, NOT through the process-wide
    `get_engine()` cache — so a repository constructed with it can
    never be accused of merely reading through a warm, already-open
    connection/session from an earlier step in the same test. This is
    what makes a "fresh repository instance" test genuinely prove
    durability rather than in-process memoization.
    """
    engine = create_engine(pg_session.get_database_url(), pool_pre_ping=True, future=True)
    try:
        yield engine
    finally:
        engine.dispose()
