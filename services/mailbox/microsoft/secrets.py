"""Microsoft mail app credential + per-mailbox token secret discipline
(CD-6 Slice 4).

Judgment call, documented (architect spec's own "reuse/extend that
module or create a clearly analogous secrets.py, your call, document
it"): a NEW, analogous module rather than generalising
``services.xero.secrets``. Reasoning: Xero's secrets module is keyed
per-``entity_id`` (one BAGMAN company maps to at most one Xero
organisation); this module is keyed per-``mailbox_id`` (a mailbox is
not an entity — see ``services/mailbox/mailbox.py``'s own "a mailbox is
NOT a company" doctrine) and additionally carries a THIRD governed
value Xero's module has no equivalent of (``tenant_id`` — see
``services/mailbox/microsoft/oauth_state.py``'s sibling module for the
same "duplicate rather than risk the already-deployed Xero flow"
reasoning, which applies here too: Xero's own closed-GREEN Slice 2
secrets discipline is already live/tested/deployed and this delivery
must not risk destabilising it for the sake of a shared abstraction
neither module's own shape strictly needs).

Two distinct kinds of secret, two distinct storage locations, both
under the SAME governed root style Xero's module established
(``services.xero.secrets``'s own module docstring — file storage, not
an encrypted Postgres column; the full "why files not Postgres"
reasoning there applies identically here and is not repeated):

1. **The Microsoft mail app's own `client_id`/`client_secret`/
   `tenant_id`** — one triple, shared by every mailbox connection (one
   Microsoft Entra app registration, not one per mailbox). Read from
   ``/opt/bagman/secrets/mail/microsoft/{client_id,client_secret,tenant_id}``
   (0700 dir / 0600 files). **None of these exist yet** — no real
   Microsoft Entra app registration exists (this slice is built and
   tested entirely against `FakeMicrosoftOAuthClient`/
   `FakeMicrosoftGraphClient`, per this delivery's own hard constraint).
   :func:`read_microsoft_app_credentials` is missing-file-tolerant and
   returns ``None`` (never raises) when any of the three files is
   absent/unreadable — mirrors
   `services.xero.secrets.read_xero_app_credentials` exactly, including
   the same honest CONFIG_ERROR-not-a-crash downstream behaviour.
   `tenant_id`'s own absence is handled identically (never a crash, an
   honest "not configured" outcome) — see this module's own
   :func:`read_microsoft_app_credentials` docstring for why `tenant_id`
   is bundled into the SAME credential triple rather than read
   separately: all three are equally required before ANY real Graph
   call (including the authorize-URL's own tenant-specific authority)
   can be attempted at all.

2. **Per-mailbox OAuth access/refresh tokens** — one pair per
   `mailbox_id`, stored as plain files under
   ``/opt/bagman/secrets/mail/microsoft/tokens/<mailbox_id>/`` (0700
   dir / 0600 files: ``access_token``, ``refresh_token``,
   ``expires_at``). Refresh-token ROTATION (architect spec — "when
   Microsoft returns a replacement refresh token, persist the
   replacement atomically, old material replaced not accumulated") is
   satisfied the same way Xero's own module satisfies it: a write
   always fully overwrites whatever was there before via
   :func:`_write_secret_file`'s own atomic-mode-on-create discipline
   (see that function's docstring, copied verbatim from Xero's already-
   hardened version — the exact fix for the real create/chmod race PID
   review already caught once in Slice 2).

Never logs, never returns via an HTTP response body, never appears in
an audit-event payload — every function here returns a value the
CALLER is responsible for treating as secret; nothing here does that
treatment for the caller (identical discipline to
`services.xero.secrets`).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

#: Overridable via `BAGMAN_MICROSOFT_MAIL_SECRETS_DIR` purely for tests
#: — mirrors `services.xero.secrets._SECRETS_DIR_ENV_VAR` exactly.
_SECRETS_DIR_ENV_VAR = "BAGMAN_MICROSOFT_MAIL_SECRETS_DIR"
_DEFAULT_SECRETS_DIR = "/opt/bagman/secrets/mail/microsoft"

_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _secrets_dir() -> Path:
    return Path(os.environ.get(_SECRETS_DIR_ENV_VAR, _DEFAULT_SECRETS_DIR))


@dataclass(frozen=True)
class MicrosoftAppCredentials:
    client_id: str
    client_secret: str
    #: A real, provisioned Microsoft Entra tenant id. Deliberately part
    #: of the SAME credential bundle as client_id/client_secret (rather
    #: than its own separately-missing-tolerant read) — the OAuth
    #: authorize URL needs a TENANT-SPECIFIC authority
    #: (`https://login.microsoftonline.com/<tenant_id>/...`), never the
    #: permanent `/common` multi-tenant authority (architect spec's
    #: explicit "never hardcode /common as a permanent choice"), so
    #: there is no meaningful partial-configured state where client_id/
    #: secret exist but tenant_id does not — all three or none.
    tenant_id: str


def read_microsoft_app_credentials() -> Optional[MicrosoftAppCredentials]:
    """Read the Microsoft mail app's `client_id`/`client_secret`/
    `tenant_id` from `<secrets_dir>/{client_id,client_secret,tenant_id}`.

    Returns `None` (never raises) if ANY of the three files is missing/
    empty/unreadable — the honest "Microsoft mail not configured" state
    (no real Entra app registration exists yet). Read fresh from disk
    on every call — never cached — so a credential the PL places after
    this delivery lands takes effect without a process restart (mirrors
    `services.xero.secrets.read_xero_app_credentials` exactly).
    """
    base = _secrets_dir()
    try:
        client_id = (base / "client_id").read_text(encoding="utf-8").strip()
        client_secret = (base / "client_secret").read_text(encoding="utf-8").strip()
        tenant_id = (base / "tenant_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not client_id or not client_secret or not tenant_id:
        return None
    return MicrosoftAppCredentials(client_id=client_id, client_secret=client_secret, tenant_id=tenant_id)


def _tokens_dir(mailbox_id: str) -> Path:
    return _secrets_dir() / "tokens" / mailbox_id


def _write_secret_file(path: Path, value: str) -> None:
    """Byte-for-byte the same hardened create-with-mode discipline as
    `services.xero.secrets._write_secret_file` (see that function's own
    docstring for the exact create/chmod race this closes) — copied
    rather than imported so this module has no runtime dependency on
    `services.xero` at all (see this module's own top docstring for why
    it is a deliberate sibling, not a generalisation)."""
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
class StoredMicrosoftTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime


def write_mailbox_tokens(mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
    """Persist `mailbox_id`'s current access/refresh token pair,
    overwriting whatever was there before (refresh-token rotation — see
    module docstring)."""
    directory = _tokens_dir(mailbox_id)
    _write_secret_file(directory / "access_token", access_token)
    _write_secret_file(directory / "refresh_token", refresh_token)
    _write_secret_file(directory / "expires_at", expires_at.astimezone(timezone.utc).isoformat())


def read_mailbox_tokens(mailbox_id: str) -> Optional[StoredMicrosoftTokens]:
    """Read back `mailbox_id`'s current token pair. Returns `None`
    (never raises) if nothing is stored yet."""
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
    return StoredMicrosoftTokens(access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)


def delete_mailbox_tokens(mailbox_id: str) -> None:
    """Remove `mailbox_id`'s stored token pair entirely (called on
    disconnect — see `services/mailbox/mailbox.py::disconnect_microsoft`).
    Never raises if nothing was stored (idempotent-safe)."""
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


class MicrosoftTokenStoreProtocol(Protocol):
    def read(self, mailbox_id: str) -> Optional[StoredMicrosoftTokens]: ...

    def write(self, mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None: ...

    def delete(self, mailbox_id: str) -> None: ...


class FileMicrosoftTokenStore:
    """Production `MicrosoftTokenStoreProtocol` — a thin wrapper around
    this module's own free functions (mirrors
    `services.xero.secrets.FileTokenStore` exactly)."""

    def read(self, mailbox_id: str) -> Optional[StoredMicrosoftTokens]:
        return read_mailbox_tokens(mailbox_id)

    def write(self, mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        write_mailbox_tokens(mailbox_id, access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)

    def delete(self, mailbox_id: str) -> None:
        delete_mailbox_tokens(mailbox_id)


class InMemoryMicrosoftTokenStore:
    """Development/test `MicrosoftTokenStoreProtocol` — never touches
    the real filesystem (mirrors `services.xero.secrets
    .InMemoryTokenStore` exactly)."""

    def __init__(self) -> None:
        self._by_mailbox: dict[str, StoredMicrosoftTokens] = {}

    def read(self, mailbox_id: str) -> Optional[StoredMicrosoftTokens]:
        return self._by_mailbox.get(mailbox_id)

    def write(self, mailbox_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        self._by_mailbox[mailbox_id] = StoredMicrosoftTokens(
            access_token=access_token, refresh_token=refresh_token, expires_at=expires_at
        )

    def delete(self, mailbox_id: str) -> None:
        self._by_mailbox.pop(mailbox_id, None)


def file_permissions_are_governed(path: Path) -> bool:
    """`True` if `path` (a file) has mode `0600` — test-only helper,
    mirrors `services.xero.secrets.file_permissions_are_governed`
    exactly."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode == _FILE_MODE
