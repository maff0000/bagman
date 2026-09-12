"""Fixtures for ``tests/app_api/`` (CD-3 WI-3).

Proves ``app/api/composition.py``'s hard "no fallback" invariant
(PID §45-46) against REAL, disposable PostgreSQL and MinIO containers
this session starts, stops/restarts mid-test, and tears down itself —
never mocks, exactly the same discipline
``tests/persistence/conftest.py`` already established for WI-1/WI-2.

Containers are named unmistakably as disposable WI-3 test fixtures
(``bagman-test-postgres-wi3``, ``bagman-test-minio-wi3``) so they can
never be confused with the real ``bagman-db``/``bagman-objects``
runtime containers a developer might also have running locally, or
with WI-1/WI-2's own ``bagman-test-postgres-wi1``/
``bagman-test-minio-wi2`` fixtures.
"""
from __future__ import annotations

import os
import secrets
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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _wait_for_postgres(timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "pg_isready", "-U", PG_USER, "-d", PG_DB],
            capture_output=True,
            text=True,
        )
        if last.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"{PG_CONTAINER} did not become ready in time: {last.stdout if last else ''}")


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


@pytest.fixture(scope="module")
def runtime_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """Start disposable Postgres + MinIO containers, run `alembic
    upgrade head`, point BAGMAN_RUNTIME_ENV=production's environment
    variables at them for the life of this test module, then tear
    everything down (containers removed, environment restored)."""
    _docker("rm", "-f", PG_CONTAINER, check=False)
    _docker("rm", "-f", MINIO_CONTAINER, check=False)

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

    try:
        _wait_for_postgres()
        _wait_for_minio()

        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(alembic_cfg, "head")

        yield {"pg_container": PG_CONTAINER, "minio_container": MINIO_CONTAINER}
    finally:
        from persistence.postgres import session as pg_session

        pg_session._engine_for_url.cache_clear()  # noqa: SLF001 - test-only cleanup

        from app.api.composition import reset_composition_for_tests

        reset_composition_for_tests()

        os.environ.clear()
        os.environ.update(env_backup)

        _docker("rm", "-f", PG_CONTAINER, check=False)
        _docker("rm", "-f", MINIO_CONTAINER, check=False)


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
