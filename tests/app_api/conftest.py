"""Fixtures for ``tests/app_api/`` (CD-3 WI-3, extended by CD-4 WI-3).

Proves ``app/api/composition.py``'s hard "no fallback" invariant
(PID §45-46) against REAL, disposable PostgreSQL and MinIO containers
this session starts, stops/restarts mid-test, and tears down itself —
never mocks, exactly the same discipline
``tests/persistence/conftest.py`` already established for WI-1/WI-2.
CD-4 WI-3 additionally stands up a REAL, disposable ``clamd`` daemon
(same shape as WI-2's own ``tests/integration/test_intake_scanner.py``
disposable container) so this module's production-mode composition has
a genuinely reachable scanner too — ``/ready`` now checks it (PID §60),
and this WI's own intake-endpoint tests exercise the REAL
``ClamAVScanner`` end-to-end through the HTTP layer against it (never a
mock), per PID §20/§63's "a stub is used in dev must never become the
real scanner is never exercised" doctrine.

Containers are named unmistakably as disposable WI-3 test fixtures
(``bagman-test-postgres-wi3``, ``bagman-test-minio-wi3``,
``bagman-test-clamav-wi3``) so they can never be confused with the real
``bagman-db``/``bagman-objects``/``bagman-scan`` runtime containers a
developer might also have running locally, or with WI-2's own
``bagman-test-clamav-wi2`` fixture.
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

PG_CONTAINER = "bagman-test-postgres-wi3"
PG_IMAGE = "postgres:17"
PG_HOST = "127.0.0.1"
PG_HOST_PORT = "55434"
PG_DB = "bagman_test_wi3"
PG_USER = "bagman_test_wi3"

MINIO_CONTAINER = "bagman-test-minio-wi3"
MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772"
MINIO_HOST_PORT = "19020"
MINIO_BUCKET = "bagman-test-wi3-objects"

#: CD-4 WI-3 addition — a differently-named/-ported disposable clamd
#: container from WI-2's own ``bagman-test-clamav-wi2``/``33100``, to
#: avoid any collision when both test modules happen to run in the
#: same CI environment.
CLAMAV_CONTAINER = "bagman-test-clamav-wi3"
#: CD-4 PR #4 Architect delta (2026-09-13): pinned to the SAME
#: immutable digest as ``deployment/compose/docker-compose.yml``'s
#: ``bagman-scan`` service, not merely the same ``:stable`` tag. This
#: disposable fixture container is genuinely torn down after every test
#: module run, so it is not itself the "proven CD-4 runtime" the
#: Architect's delta is about — a case could be made that a
#: throwaway, per-run container never needs an immutable identity the
#: way a long-lived production service does. The call made here is to
#: pin it anyway, for one concrete reason beyond "just match
#: production": without a shared digest, this fixture and the real
#: `bagman-scan` service could silently drift onto two different
#: ClamAV builds (this one whenever `:stable` next moves upstream)
#: without any test ever catching it — an intake test could then keep
#: passing against a different scanner build than production actually
#: runs, which defeats the point of these being real, non-mocked
#: ClamAV tests at all. Keep this constant equal to
#: docker-compose.yml's digest; update both together, deliberately,
#: whenever the pin is intentionally moved forward.
CLAMAV_IMAGE = (
    "clamav/clamav:stable@sha256:1fdfd24c6f0a0fb60788481487459a6d4eda8a9b448641594e04db8410d34422"
)
CLAMAV_HOST_PORT = "33101"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _wait_for_postgres(timeout_s: float = 60.0) -> None:
    """Block until PostgreSQL is genuinely ready for real queries.

    PL correction (2026-09-13, same fix as tests/persistence/conftest.py):
    a fresh official-image container briefly runs a TEMPORARY server
    (during `initdb`'s init-script phase) before the FINAL long-lived
    server starts. `pg_isready` alone can report success during that
    temporary window, so the first real query can land exactly as it's
    shutting down (`server closed the connection unexpectedly`) —
    reproduced live in CI. Requiring an actual `SELECT 1` to succeed on
    several consecutive attempts rides out that window instead of
    trusting a single `pg_isready` success.
    """
    _REQUIRED_CONSECUTIVE_OK = 3
    deadline = time.monotonic() + timeout_s
    consecutive_ok = 0
    last = None
    while time.monotonic() < deadline:
        last = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1"],
            capture_output=True,
            text=True,
        )
        if last.returncode == 0 and last.stdout.strip() == "1":
            consecutive_ok += 1
            if consecutive_ok >= _REQUIRED_CONSECUTIVE_OK:
                return
        else:
            consecutive_ok = 0
        time.sleep(0.5)
    raise RuntimeError(
        f"{PG_CONTAINER} did not become genuinely ready (real SELECT 1) in time: "
        f"{last.stdout if last else ''} {last.stderr if last else ''}"
    )


def _wait_for_minio(timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{MINIO_HOST_PORT}/minio/health/live", timeout=2
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{MINIO_CONTAINER} did not become ready in time")


def wait_for_postgres_container_healthy() -> None:
    """Public (module-level, no leading underscore) so test modules can
    re-wait after they themselves restart the container mid-test."""
    _wait_for_postgres()


def wait_for_minio_container_healthy() -> None:
    _wait_for_minio()


def _wait_for_clamav(timeout_s: float = 60.0) -> None:
    """Block until the disposable ``clamd`` daemon answers a real
    ``PING`` — same protocol check
    ``services.evidence.intake.scanner.ClamAVScanner.is_available()``
    itself performs, done directly here (not via importing that class)
    so this fixture module stays a plain, dependency-light bootstrapping
    layer."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(CLAMAV_HOST_PORT)), timeout=2) as sock:
                sock.sendall(b"zPING\0")
                if sock.recv(64).strip(b"\0") == b"PONG":
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{CLAMAV_CONTAINER} did not become ready (PING/PONG) in time")


def wait_for_clamav_container_healthy() -> None:
    """Public (module-level, no leading underscore) so test modules can
    re-wait after they themselves restart the container mid-test."""
    _wait_for_clamav()


@pytest.fixture(scope="module")
def runtime_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """Start disposable Postgres + MinIO containers, run `alembic
    upgrade head`, point BAGMAN_RUNTIME_ENV=production's environment
    variables at them for the life of this test module, then tear
    everything down (containers removed, environment restored)."""
    _docker("rm", "-f", PG_CONTAINER, check=False)
    _docker("rm", "-f", MINIO_CONTAINER, check=False)
    _docker("rm", "-f", CLAMAV_CONTAINER, check=False)

    secret_dir = tmp_path_factory.mktemp("bagman-test-wi3-secrets")

    pg_password = secrets.token_urlsafe(24)
    pg_password_file = secret_dir / "postgres_password"
    pg_password_file.write_text(pg_password, encoding="utf-8")
    pg_password_file.chmod(0o600)

    minio_user = "bagmantestwi3"
    minio_password = secrets.token_urlsafe(24)
    minio_user_file = secret_dir / "minio_user"
    minio_user_file.write_text(minio_user, encoding="utf-8")
    minio_user_file.chmod(0o600)
    minio_password_file = secret_dir / "minio_password"
    minio_password_file.write_text(minio_password, encoding="utf-8")
    minio_password_file.chmod(0o600)

    _docker(
        "run", "-d", "--name", PG_CONTAINER,
        "-e", f"POSTGRES_USER={PG_USER}",
        "-e", f"POSTGRES_PASSWORD={pg_password}",
        "-e", f"POSTGRES_DB={PG_DB}",
        "-p", f"{PG_HOST}:{PG_HOST_PORT}:5432",
        PG_IMAGE,
    )
    _docker(
        "run", "-d", "--name", MINIO_CONTAINER,
        "-e", f"MINIO_ROOT_USER={minio_user}",
        "-e", f"MINIO_ROOT_PASSWORD={minio_password}",
        "-p", f"127.0.0.1:{MINIO_HOST_PORT}:9000",
        MINIO_IMAGE, "server", "/data",
    )
    # CD-4 WI-3: a real, disposable clamd daemon — production
    # composition's scanner is mandatory (PID §60), so this test
    # module's composition needs one genuinely reachable, exactly as it
    # needs a genuinely reachable Postgres/MinIO.
    _docker(
        "run", "-d", "--name", CLAMAV_CONTAINER,
        "-p", f"127.0.0.1:{CLAMAV_HOST_PORT}:3310",
        CLAMAV_IMAGE,
    )

    env_backup = dict(os.environ)
    os.environ["BAGMAN_RUNTIME_ENV"] = "production"
    os.environ["BAGMAN_DB_HOST"] = PG_HOST
    os.environ["BAGMAN_DB_PORT"] = PG_HOST_PORT
    os.environ["BAGMAN_DB_NAME"] = PG_DB
    os.environ["BAGMAN_DB_USER"] = PG_USER
    os.environ["BAGMAN_DB_PASSWORD_FILE"] = str(pg_password_file)
    os.environ["BAGMAN_OBJECT_STORE_ENDPOINT_URL"] = f"http://127.0.0.1:{MINIO_HOST_PORT}"
    os.environ["BAGMAN_OBJECT_STORE_BUCKET"] = MINIO_BUCKET
    os.environ["BAGMAN_OBJECT_STORE_ACCESS_KEY_FILE"] = str(minio_user_file)
    os.environ["BAGMAN_OBJECT_STORE_SECRET_KEY_FILE"] = str(minio_password_file)
    os.environ["BAGMAN_SCANNER_HOST"] = "127.0.0.1"
    os.environ["BAGMAN_SCANNER_PORT"] = CLAMAV_HOST_PORT

    try:
        _wait_for_postgres()
        _wait_for_minio()
        _wait_for_clamav()

        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(alembic_cfg, "head")

        yield {
            "pg_container": PG_CONTAINER,
            "minio_container": MINIO_CONTAINER,
            "clamav_container": CLAMAV_CONTAINER,
        }
    finally:
        from persistence.postgres import session as pg_session

        pg_session._engine_for_url.cache_clear()  # noqa: SLF001 - test-only cleanup

        from app.api.composition import reset_composition_for_tests

        reset_composition_for_tests()

        os.environ.clear()
        os.environ.update(env_backup)

        _docker("rm", "-f", PG_CONTAINER, check=False)
        _docker("rm", "-f", MINIO_CONTAINER, check=False)
        _docker("rm", "-f", CLAMAV_CONTAINER, check=False)


@pytest.fixture
def client(runtime_stack: dict):
    """A fresh ``TestClient`` (and a freshly-rebuilt composition) for
    each test — so no test observes a composition another test already
    put into a "just failed" state."""
    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()

    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as test_client:
        yield test_client

    reset_composition_for_tests()
