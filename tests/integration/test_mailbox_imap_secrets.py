"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.imap.secrets` — missing-file-tolerant credential
reading, and a self-check that nothing here can leak the password (see
that module's own module docstring). Mirrors
`tests/integration/test_mailbox_microsoft_secrets.py`'s own style.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.mailbox.imap import secrets as imap_secrets


@pytest.fixture(autouse=True)
def isolated_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BAGMAN_NOUSTAI_MAIL_SECRETS_DIR", str(tmp_path / "mail" / "noustai"))
    yield tmp_path


def test_missing_credentials_returns_none_not_a_crash():
    assert imap_secrets.read_noustai_imap_credentials() is None


def test_partial_credentials_missing_password_returns_none():
    base = Path(imap_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "username").write_text("matt@noust.ai")
    # password deliberately absent
    assert imap_secrets.read_noustai_imap_credentials() is None


def test_partial_credentials_missing_username_returns_none():
    base = Path(imap_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "password").write_text("hunter2")
    assert imap_secrets.read_noustai_imap_credentials() is None


def test_empty_password_file_returns_none():
    base = Path(imap_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "username").write_text("matt@noust.ai")
    (base / "password").write_text("   \n")
    assert imap_secrets.read_noustai_imap_credentials() is None


def test_full_credentials_are_read_back():
    base = Path(imap_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "username").write_text("matt@noust.ai\n")
    (base / "password").write_text("correct-horse-battery-staple\n")
    creds = imap_secrets.read_noustai_imap_credentials()
    assert creds is not None
    assert creds.username == "matt@noust.ai"
    assert creds.password == "correct-horse-battery-staple"


def test_read_credentials_is_fresh_off_disk_every_call():
    assert imap_secrets.read_noustai_imap_credentials() is None
    base = Path(imap_secrets._secrets_dir())
    base.mkdir(parents=True, exist_ok=True)
    (base / "username").write_text("matt@noust.ai")
    (base / "password").write_text("secret")
    assert imap_secrets.read_noustai_imap_credentials() is not None


def test_default_secrets_dir_matches_the_documented_production_path(monkeypatch):
    monkeypatch.delenv("BAGMAN_NOUSTAI_MAIL_SECRETS_DIR", raising=False)
    assert str(imap_secrets._secrets_dir()) == "/opt/bagman/secrets/mail/noustai"


def test_module_source_never_contains_the_literal_word_password_in_a_log_or_print_call():
    """Grep-style self-check (WO's own explicit instruction): no code
    path in this module may print/log a password. This module has no
    print/log statements at all — assert that stays true rather than
    merely eyeballing it."""
    source = Path(imap_secrets.__file__).read_text(encoding="utf-8")
    assert "print(" not in source
    assert "logging." not in source
    assert "logger." not in source
