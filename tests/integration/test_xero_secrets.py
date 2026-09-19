"""``services/xero/secrets.py`` proofs (CD-6 Slice 2, architect spec
§3) — the missing-file-tolerant Xero app-credential reader, and the
per-connection token file store's real round trip + governed file
permissions (0700 dir / 0600 files). Every test here points
``BAGMAN_XERO_SECRETS_DIR`` at a throwaway ``tmp_path`` — never the
real ``/opt/bagman/secrets/xero/`` host path.
"""
from __future__ import annotations

import stat
from datetime import datetime, timezone

import pytest

from services.xero.secrets import (
    FileTokenStore,
    InMemoryTokenStore,
    delete_connection_tokens,
    file_permissions_are_governed,
    read_connection_tokens,
    read_xero_app_credentials,
    write_connection_tokens,
)


@pytest.fixture(autouse=True)
def _isolated_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BAGMAN_XERO_SECRETS_DIR", str(tmp_path / "xero-secrets"))
    yield


def test_read_xero_app_credentials_returns_none_when_no_developer_app_registered_yet():
    """The real, current state of this dispatch: no Xero Developer App
    exists — must be an honest `None`, never a raise."""
    assert read_xero_app_credentials() is None


def test_read_xero_app_credentials_returns_none_when_only_one_file_present(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "xero-secrets"
    monkeypatch.setenv("BAGMAN_XERO_SECRETS_DIR", str(secrets_dir))
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "client_id").write_text("some-id")
    assert read_xero_app_credentials() is None


def test_read_xero_app_credentials_returns_the_real_pair_once_both_files_exist(tmp_path, monkeypatch):
    secrets_dir = tmp_path / "xero-secrets"
    monkeypatch.setenv("BAGMAN_XERO_SECRETS_DIR", str(secrets_dir))
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "client_id").write_text("real-client-id\n")
    (secrets_dir / "client_secret").write_text("real-client-secret\n")

    credentials = read_xero_app_credentials()
    assert credentials is not None
    assert credentials.client_id == "real-client-id"
    assert credentials.client_secret == "real-client-secret"


def test_connection_token_round_trip_and_file_permissions_are_governed():
    entity_id = "some-entity-id"
    expires_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    write_connection_tokens(entity_id, access_token="fake-access", refresh_token="fake-refresh", expires_at=expires_at)

    stored = read_connection_tokens(entity_id)
    assert stored is not None
    assert stored.access_token == "fake-access"
    assert stored.refresh_token == "fake-refresh"
    assert stored.expires_at == expires_at

    import os
    from pathlib import Path

    secrets_dir = Path(os.environ["BAGMAN_XERO_SECRETS_DIR"])
    token_dir = secrets_dir / "tokens" / entity_id
    for name in ("access_token", "refresh_token", "expires_at"):
        assert file_permissions_are_governed(token_dir / name), f"{name} is not 0600"
    dir_mode = stat.S_IMODE((token_dir).stat().st_mode)
    assert dir_mode == 0o700


def test_read_connection_tokens_returns_none_when_nothing_stored():
    assert read_connection_tokens("never-written-entity") is None


def test_delete_connection_tokens_removes_them_and_read_returns_none_afterwards():
    entity_id = "to-be-deleted"
    write_connection_tokens(entity_id, access_token="a", refresh_token="b", expires_at=datetime.now(timezone.utc))
    assert read_connection_tokens(entity_id) is not None

    delete_connection_tokens(entity_id)
    assert read_connection_tokens(entity_id) is None


def test_delete_connection_tokens_is_idempotent_never_raises_when_nothing_was_stored():
    delete_connection_tokens("was-never-written")  # must not raise


def test_a_token_refresh_overwrites_the_old_pair_entirely():
    entity_id = "rotating-entity"
    write_connection_tokens(entity_id, access_token="old-access", refresh_token="old-refresh", expires_at=datetime.now(timezone.utc))
    write_connection_tokens(entity_id, access_token="new-access", refresh_token="new-refresh", expires_at=datetime.now(timezone.utc))

    stored = read_connection_tokens(entity_id)
    assert stored.access_token == "new-access"
    assert stored.refresh_token == "new-refresh"


def test_file_token_store_satisfies_the_token_store_protocol_round_trip():
    store = FileTokenStore()
    entity_id = "protocol-entity"
    expires_at = datetime(2031, 6, 1, tzinfo=timezone.utc)
    store.write(entity_id, access_token="x", refresh_token="y", expires_at=expires_at)
    fetched = store.read(entity_id)
    assert fetched is not None and fetched.access_token == "x"
    store.delete(entity_id)
    assert store.read(entity_id) is None


def test_in_memory_token_store_never_touches_the_real_filesystem(tmp_path, monkeypatch):
    """Development/test composition's own store — must work identically
    to `FileTokenStore` from a caller's point of view, but never create
    anything under `BAGMAN_XERO_SECRETS_DIR` (proven by pointing it at a
    directory that does not exist and confirming it is still never
    created)."""
    nonexistent_dir = tmp_path / "must-never-be-created"
    monkeypatch.setenv("BAGMAN_XERO_SECRETS_DIR", str(nonexistent_dir))

    store = InMemoryTokenStore()
    entity_id = "in-memory-entity"
    expires_at = datetime(2031, 6, 1, tzinfo=timezone.utc)
    store.write(entity_id, access_token="x", refresh_token="y", expires_at=expires_at)
    assert store.read(entity_id).access_token == "x"
    assert not nonexistent_dir.exists()

    store.delete(entity_id)
    assert store.read(entity_id) is None
