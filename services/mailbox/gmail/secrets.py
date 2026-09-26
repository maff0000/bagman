"""Gmail app credential + per-mailbox OAuth token secret discipline
(CD-6 GUI-operations-foundation follow-on WO — third mailbox provider).

Mirrors ``services.mailbox.microsoft.secrets``'s doctrine exactly, minus
the `tenant_id` third value Microsoft's own multi-tenant Entra app
registration needs (a Google Cloud OAuth "Web application" client has no
equivalent tenant concept — `client_id`/`client_secret` alone are
sufficient to build every URL/request this package ever issues):

1. **The Gmail app's own `client_id`/`client_secret`** — ONE pair,
   shared by BOTH Gmail mailbox connections (`mgs241171@gmail.com` and
   `matt.george.scott@gmail.com`) — one Google Cloud OAuth app, not one
   per mailbox, exactly the same "one Entra app, N mailboxes" shape
   Microsoft's own module already establishes. Read from
   ``/opt/bagman/secrets/mail/gmail/{client_id,client_secret}`` (0700
   dir / 0600 files). **Neither file exists yet** — no real Google Cloud
   OAuth app has been provisioned (this delivery is built and tested
   entirely against ``FakeGmailOAuthClient``/``FakeGmailClient``, per
   this delivery's own hard constraint).
   :func:`read_gmail_app_credentials` is missing-file-tolerant and
   returns ``None`` (never raises) when either file is absent/unreadable
   — mirrors ``services.mailbox.microsoft.secrets.read_microsoft_app_credentials``
   exactly, including the same honest CONFIG_ERROR-not-a-crash
   downstream behaviour.

2. **Per-mailbox OAuth access/refresh tokens** — one pair per
   `mailbox_id`, stored as plain files under
   ``/opt/bagman/secrets/mail/gmail/tokens/<mailbox_id>/`` (0700 dir /
   0600 files: ``access_token``, ``refresh_token``, ``expires_at``).
   Two DIFFERENT Gmail mailboxes (two different `mailbox_id`s) therefore
   get two DIFFERENT token directories automatically — this is what
   gives the two Gmail accounts independent token state, not a second
   OAuth app (see module docstring, package-level, for the full "one
   app, N mailboxes" doctrine). Refresh-token ROTATION is satisfied the
   same way Microsoft's/Xero's own modules satisfy it: a write always
   fully overwrites whatever was there before via
   :func:`_write_secret_file`'s own atomic-mode-on-create discipline
   (copied verbatim from the already-hardened Microsoft/Xero versions).

Never logs, never returns via an HTTP response body, never appears in
an audit-event payload, never appears in an exception message. Every
function here returns a value the CALLER is responsible for treating as
secret; nothing here does that treatment for the caller (identical
discipline to ``services.mailbox.microsoft.secrets``/
``services.mailbox.imap.secrets``). Grep this module's own diff for the
literal strings ``token``/``secret`` in any f-string that might land in
an exception or log line before finishing a change here.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

#: Overridable via `BAGMAN_GMAIL_MAIL_SECRETS_DIR` purely for tests —
#: mirrors `services.mailbox.microsoft.secrets._SECRETS_DIR_ENV_VAR`
#: exactly.
_SECRETS_DIR_ENV_VAR = "BAGMAN_GMAIL_MAIL_SECRETS_DIR"
_DEFAULT_SECRETS_DIR = "/opt/bagman/secrets/mail/gmail"

_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _secrets_dir() -> Path:
    return Path(os.environ.get(_SECRETS_DIR_ENV_VAR, _DEFAULT_SECRETS_DIR))


@dataclass(frozen=True)
class GmailAppCredentials:
    client_id: str
    client_secret: str


def read_gmail_app_credentials() -> Optional[GmailAppCredentials]:
    """Read the Gmail app's `client_id`/`client_secret` from
    `<secrets_dir>/{client_id,client_secret}`.

    Returns `None` (never raises) if EITHER file is missing/empty/
    unreadable — the honest "Gmail not configured" state (no real
    Google Cloud OAuth app exists yet). Read fresh from disk on every
    call — never cached — so a credential the PL places after this
    delivery lands takes effect without a process restart (mirrors
    `services.mailbox.microsoft.secrets.read_microsoft_app_credentials`
    exactly).
    """
    base = _secrets_dir()
    try:
        client_id = (base / "client_id").read_text(encoding="utf-8").strip()
        client_secret = (base / "client_secret").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not client_id or not client_secret:
        return None
    return GmailAppCredentials(client_id=client_id, client_secret=client_secret)


def _tokens_dir(mailbox_id: str) -> Path:
    return _secrets_dir() / "tokens" / mailbox_id


def _write_secret_file(path: Path, value: str) -> None:
    """Byte-for-byte the same hardened create-with-mode discipline as
    `services.mailbox.microsoft.secrets._write_secret_file` (see that
    function's own docstring for the exact create/chmod race this
    closes) — copied rather than imported so this module has no runtime
    dependency on `services.mailbox.microsoft` at all (see this module's
    own top docstring for why it is a deliberate sibling, not a
    generalisation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, _DIR_MODE)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    finally:
        try:
            os.chmod(path, _FILE_MODE)
        except OSError:
            pass


@dataclass(frozen=True)
class StoredGmailTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime


def write_mailbox_tokens(mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
    """Persist `mailbox_id`'s current access/refresh token pair,
    overwriting whatever was there before (refresh-token rotation — see
    module docstring). Two different `mailbox_id`s always resolve to two
    different, non-overlapping directories."""
    directory = _tokens_dir(mailbox_id)
    _write_secret_file(directory / "access_token", access_token)
    _write_secret_file(directory / "refresh_token", refresh_token)
    _write_secret_file(directory / "expires_at", expires_at.astimezone(timezone.utc).isoformat())


def read_mailbox_tokens(mailbox_id: str) -> Optional[StoredGmailTokens]:
    """Read back `mailbox_id`'s current token pair. Returns `None`
    (never raises) if nothing is stored yet — one mailbox's tokens are
    never visible when reading a DIFFERENT `mailbox_id` (two Gmail
    accounts are independent by construction — see module docstring)."""
    directory = _tokens_dir(mailbox_id)
    try:
        access_token = (directory / "access_token").read_text(encoding="utf-8").strip()
        refresh_token = (directory / "refresh_token").read_text(encoding="utf-8").strip()
        expires_at_raw = (directory / "expires_at").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        return None
    if not access_token or not refresh_token:
        return None
    return StoredGmailTokens(access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)


def delete_mailbox_tokens(mailbox_id: str) -> None:
    """Remove `mailbox_id`'s stored token pair entirely (called on
    disconnect). Never raises if nothing was stored (idempotent-safe)."""
    directory = _tokens_dir(mailbox_id)
    for name in ("access_token", "refresh_token", "expires_at"):
        path = directory / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


class GmailTokenStoreProtocol(Protocol):
    def read(self, mailbox_id: str) -> Optional[StoredGmailTokens]: ...

    def write(self, mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None: ...

    def delete(self, mailbox_id: str) -> None: ...


class FileGmailTokenStore:
    """Production `GmailTokenStoreProtocol` — a thin wrapper around this
    module's own free functions (mirrors
    `services.mailbox.microsoft.secrets.FileMicrosoftTokenStore`
    exactly)."""

    def read(self, mailbox_id: str) -> Optional[StoredGmailTokens]:
        return read_mailbox_tokens(mailbox_id)

    def write(self, mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        write_mailbox_tokens(mailbox_id, access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)

    def delete(self, mailbox_id: str) -> None:
        delete_mailbox_tokens(mailbox_id)


class InMemoryGmailTokenStore:
    """Development/test `GmailTokenStoreProtocol` — never touches the
    real filesystem (mirrors
    `services.mailbox.microsoft.secrets.InMemoryMicrosoftTokenStore`
    exactly). Keyed by `mailbox_id`, so two different mailbox ids are
    independent by construction — never a shared dict key."""

    def __init__(self) -> None:
        self._by_mailbox: dict[str, StoredGmailTokens] = {}

    def read(self, mailbox_id: str) -> Optional[StoredGmailTokens]:
        return self._by_mailbox.get(mailbox_id)

    def write(self, mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        self._by_mailbox[mailbox_id] = StoredGmailTokens(
            access_token=access_token, refresh_token=refresh_token, expires_at=expires_at
        )

    def delete(self, mailbox_id: str) -> None:
        self._by_mailbox.pop(mailbox_id, None)


def file_permissions_are_governed(path: Path) -> bool:
    """`True` if `path` (a file) has mode `0600` — test-only helper,
    mirrors `services.mailbox.microsoft.secrets.file_permissions_are_governed`
    exactly."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode == _FILE_MODE
