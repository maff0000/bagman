"""PID §45-46 REQUIRED FAILURE PROOF, for the WI-3 app/api layer.

WI-3's own contract states this explicitly: "Prove this with a test:
point composition.py at a real-but-currently-down Postgres/MinIO (stop
the container mid-test) and confirm /ready returns 503, not a fallback
success." This module is that test.

Each test is self-contained: it forces a successful ``/ready`` call
FIRST (so ``app.api.composition`` has already built a real,
Postgres/MinIO-backed composition), only THEN stops the relevant
dependency's container, and always restarts it in a ``finally`` block
regardless of outcome — so test order/failure never leaves the shared
``runtime_stack`` container in a stopped state for a later test.

Run as (real Docker required — these are not mocked):

    pytest -s tests/app_api/test_no_fallback.py -v
"""
from __future__ import annotations

import subprocess

from tests.app_api.conftest import wait_for_minio_container_healthy, wait_for_postgres_container_healthy


def _docker_stop(name: str) -> None:
    subprocess.run(["docker", "stop", name], check=True, capture_output=True)


def _docker_start(name: str) -> None:
    subprocess.run(["docker", "start", name], check=True, capture_output=True)


def test_ready_is_green_with_both_dependencies_up(client):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["runtime_environment"] == "production"
    assert body["checks"] == {"postgres": "ok", "object_store": "ok"}


def test_postgres_down_fails_ready_with_no_fallback(client, runtime_stack):
    # Force a real, Postgres/MinIO-backed composition to exist first.
    assert client.get("/ready").status_code == 200

    from persistence.postgres.entity_repository import PostgresEntityRepository
    from app.api.composition import get_composition

    _docker_stop(runtime_stack["pg_container"])
    try:
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        assert body["failed_dependency"] == "postgres"
        # Not merely "the HTTP status looks right" — the hard PID §46
        # invariant is that composition itself never swapped backends:
        # still explicitly "production", and still the REAL Postgres
        # repository instance, not an in-memory substitute.
        assert body["runtime_environment"] == "production"
        composition = get_composition()
        assert composition.runtime_environment == "production"
        assert isinstance(composition.api.entity_repository, PostgresEntityRepository)
    finally:
        _docker_start(runtime_stack["pg_container"])
        wait_for_postgres_container_healthy()

    # Recovery: the same (never-swapped) composition reports ready
    # again once the real dependency is back — proving this was a live
    # check, not a latched/cached failure.
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_object_store_down_fails_ready_with_no_fallback(client, runtime_stack):
    assert client.get("/ready").status_code == 200

    from persistence.objects.minio_store import MinIOObjectStore
    from app.api.composition import get_composition

    _docker_stop(runtime_stack["minio_container"])
    try:
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        assert body["failed_dependency"] == "object_store"
        assert body["runtime_environment"] == "production"
        composition = get_composition()
        assert composition.runtime_environment == "production"
        # Still the real MinIO-backed store — never silently replaced
        # by persistence.objects.memory_store.InMemoryObjectStore.
        assert isinstance(composition.object_store, MinIOObjectStore)
    finally:
        _docker_start(runtime_stack["minio_container"])
        wait_for_minio_container_healthy()

    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_development_mode_ready_is_unconditionally_green(monkeypatch):
    """The counterpart proof: development/test composition mode is
    explicitly, honestly in-memory — /ready says so plainly rather than
    ambiguously, and never pretends to have checked a dependency it
    does not have."""
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")

    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()
    try:
        from fastapi.testclient import TestClient

        from app.api.main import app

        with TestClient(app) as test_client:
            response = test_client.get("/ready")
            assert response.status_code == 200
            body = response.json()
            assert body["ready"] is True
            assert body["runtime_environment"] == "development"
            assert body["checks"] == {}
    finally:
        reset_composition_for_tests()
