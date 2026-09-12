"""PL bug fix proof (CD-3 WI-3 review): `GET /internal/evidence/{id}`
with a syntactically-invalid (non-UUID-shaped) id string must return
HTTP 404 (`core.errors.NotFoundError`), not HTTP 503
(`core.errors.PersistenceError`) — see
`persistence/postgres/evidence_repository.py::get_evidence` and
`persistence/postgres/db_errors.py::is_invalid_uuid_format` for the
underlying fix, and `tests/persistence/test_evidence_persistence.py`
for the repository-level proof of the same behaviour. This module
proves it end-to-end through the real HTTP router + centralised
BagmanError -> HTTP-status translation in `app/api/main.py`, against a
real, production-mode, Postgres/MinIO-backed composition (the `client`
fixture from `tests/app_api/conftest.py`) — not a mock.
"""
from __future__ import annotations


def test_get_evidence_with_a_malformed_id_returns_404_not_503(client):
    response = client.get("/internal/evidence/not-a-valid-uuid")
    assert response.status_code == 404
    body = response.json()
    assert body["error_code"] == "NOT_FOUND"


def test_get_evidence_with_a_wellformed_but_nonexistent_id_still_returns_404(client):
    """Counterpart proof: the well-formed-but-nonexistent case was
    already correct before this fix — must still be 404 after it."""
    import uuid

    response = client.get(f"/internal/evidence/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["error_code"] == "NOT_FOUND"
