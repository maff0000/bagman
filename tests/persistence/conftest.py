"""Shared fixtures for `tests/persistence/` (CD-3 WI-1 + WI-2, PID §41/§42/§63).

This directory holds two independent groups of tests, merged into one
conftest because pytest allows only one `conftest.py` per directory:

* **PostgreSQL persistence tests** (WI-1, PID §8-14/§21-23/§42) —
  exercise `persistence.postgres.*` against a REAL disposable
  PostgreSQL container this session starts and tears down itself —
  never mocks, never SQLite, never an in-memory substitute. The whole
  point of CD-3 WI-1 is proving durability against an actual
  PostgreSQL server, including its real constraint enforcement
  (unique/foreign-key violations translated by
  `persistence/postgres/db_errors.py`). The `postgres_container`
  fixture is session-scoped and autoused (via `_clean_tables`) for
  every test in this file's directory tree, so these tests always run
  when `pytest tests/` is invoked in an environment with Docker
  available.

* **Object-store tests** (WI-2, PID §15-19/§41-42) — exercise
  `persistence.objects.minio_store.MinIOObjectStore` against a REAL,
  disposable MinIO instance. Unlike the PostgreSQL tests above, these
  are NOT auto-started by this conftest: every test using them is
  skipped (not failed) if nothing answers at the configured endpoint
  (`requires_live_minio`), so a plain `pytest tests/` in an environment
  with no separately-started MinIO instance still passes cleanly. See
  `tests/persistence/test_minio_store.py`'s own module docstring for
  the exact disposable-container command to start one by hand.

Postgres container/credentials (WI-1)
--------------------------------------
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

MinIO container/credentials (WI-2)
------------------------------------
See `tests/persistence/test_minio_store.py`'s own docstring for the
`bagman-test-minio-wi2` container command. PL correction (before this
branch was ever pushed): the original version of this file hardcoded a
literal fallback access/secret key. Gitleaks correctly flagged that
(RuleID `generic-api-key`) — a credential-shaped literal is not
acceptable in Git history regardless of how low-stakes it actually is
(CD-1 security doctrine). Fixed by generating the throwaway secret at
import time via `secrets.token_urlsafe()` instead of a string literal
(PID §30's own sanctioned "ephemeral credentials generated within
isolated CI/runtime contexts" pattern).
"""
from __future__ import annotations

import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Iterator

import boto3
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from persistence.objects.minio_store import MinIOConfig, MinIOObjectStore
from persistence.postgres import session as pg_session

# ---------------------------------------------------------------------
# PostgreSQL (WI-1)
# ---------------------------------------------------------------------

CONTAINER_NAME = "bagman-test-postgres-wi1"
IMAGE = "postgres:17"
HOST = "127.0.0.1"
HOST_PORT = "55432"
DB_NAME = "bagman_test"
DB_USER = "bagman_test"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The canonical tables the migrations under `alembic/versions/` must
#: create. `intake_records` (CD-4 WI-1) added alongside the original
#: six CD-2/CD-3 tables — truncated together in one statement below, so
#: table order here does not matter (CASCADE handles FK ordering).
CANONICAL_TABLES = (
    "audit_events",
    "governed_entities",
    "sources",
    "evidence_items",
    "external_references",
    "provenance",
    "intake_records",
)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _wait_for_postgres(timeout_s: float = 60.0) -> None:
    """Block until PostgreSQL is genuinely ready for real queries — not
    merely until `pg_isready` reports success once.

    PL correction (2026-09-13): the official `postgres` image, on a
    fresh (first-run) container, runs `initdb`, then briefly starts a
    TEMPORARY server to execute init scripts, stops it, and only then
    starts the FINAL long-lived server. `pg_isready` can report success
    during that fleeting temporary-server window — it only checks that
    something is accepting TCP connections on the port, not that it is
    the final server. A caller's very first real query can then land
    exactly as the temporary server is shutting down, surfacing as
    `psycopg.OperationalError: ... server closed the connection
    unexpectedly` — reproduced live in CI (a slower/more loaded runner
    makes the race far more likely to bite than on a fast, idle
    development host, which is why this never failed locally).

    Fixed by waiting for an actual `SELECT 1` to succeed (not just
    `pg_isready`), and requiring it to succeed on `_REQUIRED_CONSECUTIVE_OK`
    consecutive attempts before declaring readiness — riding out the
    brief window between the temporary server's shutdown and the final
    server's startup, rather than trusting a single success.
    """
    _REQUIRED_CONSECUTIVE_OK = 3
    deadline = time.monotonic() + timeout_s
    consecutive_ok = 0
    last_result = None
    while time.monotonic() < deadline:
        last_result = subprocess.run(
            [
                "docker", "exec", CONTAINER_NAME,
                "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", "SELECT 1",
            ],
            capture_output=True,
            text=True,
        )
        if last_result.returncode == 0 and last_result.stdout.strip() == "1":
            consecutive_ok += 1
            if consecutive_ok >= _REQUIRED_CONSECUTIVE_OK:
                return
        else:
            consecutive_ok = 0
        time.sleep(0.5)
    raise RuntimeError(
        f"{CONTAINER_NAME} did not become genuinely ready (real SELECT 1) within "
        f"{timeout_s}s: {last_result.stdout if last_result else '<no attempt>'} "
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


# ---------------------------------------------------------------------
# Object store / MinIO (WI-2)
# ---------------------------------------------------------------------

#: Throwaway credentials for this disposable test instance only — never
#: real BAGMAN runtime credentials, never read from
#: `/srv/bagman-secrets/` (WI-2 contract §4/§5).
_ENDPOINT_URL = os.environ.get("BAGMAN_TEST_MINIO_ENDPOINT", "http://127.0.0.1:19000")
_ACCESS_KEY = os.environ.get("BAGMAN_TEST_MINIO_ACCESS_KEY", "bagman-test-wi2")
_SECRET_KEY = os.environ.get("BAGMAN_TEST_MINIO_SECRET_KEY") or secrets.token_urlsafe(24)
_BUCKET = "bagman-test-wi2-objects"


def _minio_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{_ENDPOINT_URL}/minio/health/live", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


requires_live_minio = pytest.mark.skipif(
    not _minio_is_up(),
    reason=(
        f"no disposable MinIO answering at {_ENDPOINT_URL} — these tests "
        "require a REAL object store, not a mock (WI-2 contract); start the "
        "bagman-test-minio-wi2 container documented in test_minio_store.py's "
        "module docstring"
    ),
)


@pytest.fixture
def bucket_name() -> str:
    return _BUCKET


@pytest.fixture
def minio_config() -> MinIOConfig:
    return MinIOConfig(
        endpoint_url=_ENDPOINT_URL,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        bucket=_BUCKET,
    )


@pytest.fixture
def store(minio_config: MinIOConfig) -> MinIOObjectStore:
    return MinIOObjectStore(minio_config)


@pytest.fixture
def raw_s3_client(minio_config: MinIOConfig):
    """A bare `boto3` S3 client against the same disposable instance,
    used ONLY to simulate real corruption/pre-existing-content
    scenarios by writing bytes directly, bypassing
    `MinIOObjectStore.put()`'s own hash/immutability checks entirely —
    exactly as the WI-2 contract's test plan requires."""
    return boto3.client(
        "s3",
        endpoint_url=minio_config.endpoint_url,
        aws_access_key_id=minio_config.access_key,
        aws_secret_access_key=minio_config.secret_key,
        region_name=minio_config.region_name,
    )


@pytest.fixture
def evidence_id() -> str:
    """A fresh, unique evidence_id per test so object keys never
    collide across tests sharing the one disposable bucket."""
    return str(uuid.uuid4())
