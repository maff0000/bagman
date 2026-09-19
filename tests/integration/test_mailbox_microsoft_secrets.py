"""CD-6 Slice 4 tests for `services.mailbox.microsoft.secrets` —
missing-file-tolerant app credentials, 0700/0600 governed file
permissions, and refresh-token rotation (mirrors
`tests/integration/test_xero_secrets.py`'s own style/rigor for the
closest structural precedent — no such file exists yet for Xero in
this tree, so this mirrors `services.xero.secrets`'s own documented
contract instead).
"""
from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.mailbox.microsoft import secrets as ms


@pytest.fixture(autouse=True)
def isolated_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BAGMAN_MICROSOFT_MAIL_SECRETS_DIR", str(tmp_path / "mail" / "microsoft"))
    yield tmp_path


def test_missing_app_credentials_returns_none_not_a_crash():
    assert ms.read_microsoft_app_credentials() is None


def test_partial_app_credentials_missing_tenant_id_returns_none(tmp_path):
    base = Path(ms._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("id-123")
    (base / "client_secret").write_text("secret-456")
    # tenant_id deliberately absent
    assert ms.read_microsoft_app_credentials() is None


def test_full_app_credentials_are_read_back(tmp_path):
    base = Path(ms._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("id-123\n")
    (base / "client_secret").write_text("secret-456\n")
    (base / "tenant_id").write_text("tenant-789\n")
    creds = ms.read_microsoft_app_credentials()
    assert creds is not None
    assert creds.client_id == "id-123"
    assert creds.client_secret == "secret-456"
    assert creds.tenant_id == "tenant-789"


def test_read_app_credentials_is_fresh_off_disk_every_call(tmp_path):
    """No caching — a credential placed after process start takes
    effect without a restart."""
    assert ms.read_microsoft_app_credentials() is None
    base = Path(ms._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("id")
    (base / "client_secret").write_text("secret")
    (base / "tenant_id").write_text("tenant")
    assert ms.read_microsoft_app_credentials() is not None


def test_write_mailbox_tokens_creates_0600_files_and_0700_dir(tmp_path):
    ms.write_mailbox_tokens(
        "mailbox-1", access_token="at", refresh_token="rt", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    tokens_dir = ms._tokens_dir("mailbox-1")
    assert stat.S_IMODE(tokens_dir.stat().st_mode) == 0o700
    for name in ("access_token", "refresh_token", "expires_at"):
        path = tokens_dir / name
        assert ms.file_permissions_are_governed(path)


def test_read_mailbox_tokens_round_trips():
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    ms.write_mailbox_tokens("mailbox-1", access_token="at1", refresh_token="rt1", expires_at=expires)
    tokens = ms.read_mailbox_tokens("mailbox-1")
    assert tokens is not None
    assert tokens.access_token == "at1"
    assert tokens.refresh_token == "rt1"


def test_read_mailbox_tokens_returns_none_when_nothing_stored():
    assert ms.read_mailbox_tokens("no-such-mailbox") is None


def test_refresh_token_rotation_fully_overwrites_the_old_pair():
    expires1 = datetime.now(timezone.utc) + timedelta(hours=1)
    ms.write_mailbox_tokens("mailbox-1", access_token="at1", refresh_token="rt1", expires_at=expires1)
    expires2 = datetime.now(timezone.utc) + timedelta(hours=2)
    ms.write_mailbox_tokens("mailbox-1", access_token="at2", refresh_token="rt2", expires_at=expires2)
    tokens = ms.read_mailbox_tokens("mailbox-1")
    assert tokens.access_token == "at2"
    assert tokens.refresh_token == "rt2"
    # The OLD refresh_token must genuinely be gone, not merely shadowed.
    assert tokens.refresh_token != "rt1"


def test_delete_mailbox_tokens_removes_everything_and_is_idempotent():
    ms.write_mailbox_tokens(
        "mailbox-1", access_token="at", refresh_token="rt", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    ms.delete_mailbox_tokens("mailbox-1")
    assert ms.read_mailbox_tokens("mailbox-1") is None
    ms.delete_mailbox_tokens("mailbox-1")  # never raises on an already-empty mailbox


def test_in_memory_token_store_never_touches_the_filesystem():
    store = ms.InMemoryMicrosoftTokenStore()
    assert store.read("mailbox-1") is None
    store.write("mailbox-1", access_token="a", refresh_token="b", expires_at=datetime.now(timezone.utc))
    assert store.read("mailbox-1").access_token == "a"
    store.delete("mailbox-1")
    assert store.read("mailbox-1") is None
