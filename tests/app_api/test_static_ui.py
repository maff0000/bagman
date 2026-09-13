"""Proves mounting the CD-4 WI-4 Documents GUI (``app/api/static/``,
served via Starlette's ``StaticFiles`` at ``"/"`` — see
``app/api/main.py``'s module docstring) does not disturb any
pre-existing JSON route, and that the static assets themselves are
actually served by the real FastAPI app.

Deliberately independent of ``tests/app_api/conftest.py``'s
``runtime_stack``/``client`` fixtures (which stand up real, disposable
PostgreSQL/MinIO/ClamAV containers for CD-3/CD-4 WI-3's own tests) —
none of that is needed to prove routing/static-file behaviour, so this
module builds its own lightweight ``TestClient`` against
development/test (in-memory) composition instead, keeping this proof
fast and dependency-free.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def dev_client(monkeypatch):
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "test")

    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()

    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as test_client:
        yield test_client

    reset_composition_for_tests()


# ---------------------------------------------------------------------
# existing JSON routes must keep working exactly as before
# ---------------------------------------------------------------------


def test_health_still_works(dev_client):
    response = dev_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_still_works(dev_client):
    response = dev_client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True


def test_version_still_works(dev_client):
    response = dev_client.get("/version")
    assert response.status_code == 200
    assert "git_commit" in response.json()


def test_internal_intake_list_still_works(dev_client):
    response = dev_client.get("/internal/intake")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []


def test_internal_evidence_list_still_works(dev_client):
    response = dev_client.get("/internal/evidence")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []


def test_internal_evidence_not_found_still_maps_to_404(dev_client):
    import uuid

    response = dev_client.get(f"/internal/evidence/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


def test_openapi_and_docs_routes_unaffected(dev_client):
    response = dev_client.get("/openapi.json")
    assert response.status_code == 200
    response = dev_client.get("/docs")
    assert response.status_code == 200


# ---------------------------------------------------------------------
# the static Documents GUI is actually served
# ---------------------------------------------------------------------


def test_root_serves_the_bagman_shell_index_html(dev_client):
    response = dev_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>BAGMAN</title>" in response.text
    assert "Documents" in response.text


def test_style_css_is_served(dev_client):
    response = dev_client.get("/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_app_js_is_served(dev_client):
    response = dev_client.get("/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert ".innerHTML" not in response.text  # no unescaped-HTML injection sink anywhere


def test_unknown_static_path_is_a_plain_404_not_a_json_api_error(dev_client):
    response = dev_client.get("/this-path-does-not-exist-anywhere")
    assert response.status_code == 404
