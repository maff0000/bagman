"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.secrets` — missing-file-tolerant app credential
reading, per-mailbox token round-trip/isolation, and a self-check that
nothing here can leak a token/secret. Mirrors
`tests/integration/test_mailbox_microsoft_secrets.py`'s own style.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.mailbox.gmail import secrets as gmail_secrets


@pytest.fixture(autouse=True)
def isolated_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BAGMAN_GMAIL_MAIL_SECRETS_DIR", str(tmp_path / "mail" / "gmail"))
    yield tmp_path


# -- app credentials ----------------------------------------------------


def test_missing_app_credentials_returns_none_not_a_crash():
    assert gmail_secrets.read_gmail_app_credentials() is None


def test_partial_app_credentials_missing_secret_returns_none():
    base = Path(gmail_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("fake-client-id")
    assert gmail_secrets.read_gmail_app_credentials() is None


def test_partial_app_credentials_missing_id_returns_none():
    base = Path(gmail_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_secret").write_text("fake-client-secret")
    assert gmail_secrets.read_gmail_app_credentials() is None


def test_empty_client_secret_file_returns_none():
    base = Path(gmail_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("fake-client-id")
    (base / "client_secret").write_text("   \n")
    assert gmail_secrets.read_gmail_app_credentials() is None


def test_full_app_credentials_are_read_back():
    base = Path(gmail_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("fake-client-id.apps.googleusercontent.com\n")
    (base / "client_secret").write_text("fake-client-secret\n")
    creds = gmail_secrets.read_gmail_app_credentials()
    assert creds is not None
    assert creds.client_id == "fake-client-id.apps.googleusercontent.com"
    assert creds.client_secret == "fake-client-secret"


def test_app_credentials_are_read_fresh_off_disk_every_call():
    assert gmail_secrets.read_gmail_app_credentials() is None
    base = Path(gmail_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "client_id").write_text("id")
    (base / "client_secret").write_text("secret")
    assert gmail_secrets.read_gmail_app_credentials() is not None


def test_default_secrets_dir_matches_the_documented_production_path(monkeypatch):
    monkeypatch.delenv("BAGMAN_GMAIL_MAIL_SECRETS_DIR", raising=False)
    assert str(gmail_secrets._secrets_dir()) == "/opt/bagman/secrets/mail/gmail"


# -- per-mailbox OAuth tokens --------------------------------------------


def _expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def test_reading_tokens_for_an_unconnected_mailbox_returns_none():
    assert gmail_secrets.read_mailbox_tokens("mb-never-connected") is None


def test_tokens_round_trip():
    expires_at = _expires_at()
    gmail_secrets.write_mailbox_tokens("mb-1", access_token="access-1", refresh_token="refresh-1", expires_at=expires_at)
    tokens = gmail_secrets.read_mailbox_tokens("mb-1")
    assert tokens is not None
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_at == expires_at


def test_two_different_mailbox_ids_get_two_different_token_directories():
    """The core cross-account isolation proof — two Gmail mailboxes
    (`mgs241171@gmail.com`/`matt.george.scott@gmail.com`) must never
    share token state."""
    gmail_secrets.write_mailbox_tokens("mb-account-1", access_token="access-A", refresh_token="refresh-A", expires_at=_expires_at())
    gmail_secrets.write_mailbox_tokens("mb-account-2", access_token="access-B", refresh_token="refresh-B", expires_at=_expires_at())

    tokens_1 = gmail_secrets.read_mailbox_tokens("mb-account-1")
    tokens_2 = gmail_secrets.read_mailbox_tokens("mb-account-2")
    assert tokens_1.access_token == "access-A"
    assert tokens_2.access_token == "access-B"
    assert tokens_1.access_token != tokens_2.access_token

    base = Path(gmail_secrets._secrets_dir())
    dir_1 = base / "tokens" / "mb-account-1"
    dir_2 = base / "tokens" / "mb-account-2"
    assert dir_1.exists() and dir_2.exists()
    assert dir_1 != dir_2


def test_one_mailboxs_tokens_are_never_visible_reading_a_different_mailbox_id():
    gmail_secrets.write_mailbox_tokens("mb-account-1", access_token="access-A", refresh_token="refresh-A", expires_at=_expires_at())
    assert gmail_secrets.read_mailbox_tokens("mb-account-2") is None


def test_write_overwrites_rather_than_accumulates_rotation():
    gmail_secrets.write_mailbox_tokens("mb-1", access_token="access-old", refresh_token="refresh-old", expires_at=_expires_at())
    new_expires = _expires_at() + timedelta(hours=1)
    gmail_secrets.write_mailbox_tokens("mb-1", access_token="access-new", refresh_token="refresh-new", expires_at=new_expires)
    tokens = gmail_secrets.read_mailbox_tokens("mb-1")
    assert tokens.access_token == "access-new"
    assert tokens.refresh_token == "refresh-new"


def test_delete_is_idempotent_safe_even_when_nothing_was_stored():
    gmail_secrets.delete_mailbox_tokens("mb-never-existed")  # must never raise


def test_delete_removes_stored_tokens():
    gmail_secrets.write_mailbox_tokens("mb-1", access_token="a", refresh_token="r", expires_at=_expires_at())
    gmail_secrets.delete_mailbox_tokens("mb-1")
    assert gmail_secrets.read_mailbox_tokens("mb-1") is None


def test_file_permissions_are_governed_0600():
    gmail_secrets.write_mailbox_tokens("mb-1", access_token="a", refresh_token="r", expires_at=_expires_at())
    path = gmail_secrets._tokens_dir("mb-1") / "access_token"
    assert gmail_secrets.file_permissions_are_governed(path)


# -- InMemoryGmailTokenStore ----------------------------------------------


def test_in_memory_token_store_isolates_by_mailbox_id():
    store = gmail_secrets.InMemoryGmailTokenStore()
    store.write("mb-1", access_token="a1", refresh_token="r1", expires_at=_expires_at())
    store.write("mb-2", access_token="a2", refresh_token="r2", expires_at=_expires_at())
    assert store.read("mb-1").access_token == "a1"
    assert store.read("mb-2").access_token == "a2"
    store.delete("mb-1")
    assert store.read("mb-1") is None
    assert store.read("mb-2") is not None


# -- secret-discipline self-check -----------------------------------------


def test_module_source_never_logs_or_prints_a_secret():
    """Grep-style self-check (mirrors
    `tests/integration/test_mailbox_imap_secrets.py`'s own identical
    check): no code path in this module may print/log a token/secret."""
    source = Path(gmail_secrets.__file__).read_text(encoding="utf-8")
    assert "print(" not in source
    assert "logging." not in source
    assert "logger." not in source
