"""Xero secrets discipline (CD-6 Slice 2, architect spec §3).

Two distinct kinds of secret, two distinct storage locations, both
under the same governed root — matching this codebase's established
secret-file discipline exactly
(``app/api/composition.py::_read_secret_file``,
``agent/claude_code/runner.py``'s own credential handling, both cited
directly by this WI's own dispatch):

1. **The Xero Developer App's own `client_id`/`client_secret`** — one
   pair, shared by every BAGMAN entity's connection (there is one Xero
   Developer App registration, not one per company). Read from
   ``/opt/bagman/secrets/xero/client_id`` and
   ``/opt/bagman/secrets/xero/client_secret`` (0700 dir / 0600 files).
   **These do not exist yet** (no Xero Developer App has been
   registered — PID §102.1's own stated constraint for this dispatch):
   :func:`read_xero_app_credentials` is missing-file-tolerant and
   returns ``None`` (never raises) when either file is absent/unreadable
   — every caller (``services.xero.client.XeroOAuthClient``,
   ``app/api/routers/xero.py``) turns that into an honest "Xero not
   configured" outcome (`XeroOutcomeStatus.CONFIG_ERROR` / a clear HTTP
   422) rather than crashing composition or `/ready`.

2. **Per-connection OAuth access/refresh tokens** — one pair per
   BAGMAN entity (not shared), stored as plain files under
   ``/opt/bagman/secrets/xero/tokens/<entity_id>/`` (0700 dir / 0600
   files: ``access_token``, ``refresh_token``, ``expires_at``).

Design choice recorded here, since this IS the decision the dispatch
asked for (architect spec §3: "pick ONE approach, document why"):
**per-connection FILES, not an encrypted-at-rest Postgres column.**
Reasons:

* This codebase has NO existing encryption-at-rest primitive anywhere
  (`requirements.txt` carries no `cryptography`/`pynacl`/equivalent
  dependency) — introducing one for exactly one field, in a slice
  whose own scope boundary explicitly forbids broadening unrelated
  infrastructure, would be a disproportionately large, novel piece of
  cryptographic engineering (key management, rotation, at-rest format)
  to get right for a single read-only-reference-data delivery.
* This codebase's EXISTING, already-audited secret-storage doctrine —
  files under a `/run/secrets`/`/srv/bagman-secrets`/`/opt/bagman/secrets`
  governed root, 0700/0600, read fresh off disk per use, never cached
  beyond one call's stack frame (see
  `ai/providers/litellm/client.py::_authorization_header`'s own
  identical "read fresh so a rotated secret takes effect without a
  restart" reasoning) — already exists, is already the house pattern
  for a real OAuth credential
  (`agent/claude_code/runner.py::HOME_ENV_VAR`'s own Claude Code
  session/auth directory), and needs zero new dependencies or design.
* A Postgres column, encrypted or not, still needs a KEY stored
  SOMEWHERE — which, absent a KMS this codebase has no integration
  with, would itself end up back on a governed filesystem path. Filing
  the token directly there is one fewer moving part for identical
  protection (filesystem permissions + host access control), and keeps
  the "where do BAGMAN's secrets live" answer to exactly one place
  (`/opt/bagman/secrets/`) rather than two.

Never logs, never returns via an HTTP response body, never appears in
an audit-event payload (architect spec §3/§24) — every function in this
module returns a value the CALLER is responsible for treating as
secret; nothing here does that treatment for the caller.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

#: Root secrets directory (architect spec §3's own literal path).
#: Overridable via `BAGMAN_XERO_SECRETS_DIR` purely for tests (this
#: WI's own disposable-test-fixture discipline — never point this at a
#: real host path from a test) — every production call site uses the
#: real default.
_SECRETS_DIR_ENV_VAR = "BAGMAN_XERO_SECRETS_DIR"
_DEFAULT_SECRETS_DIR = "/opt/bagman/secrets/xero"

_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _secrets_dir() -> Path:
    return Path(os.environ.get(_SECRETS_DIR_ENV_VAR, _DEFAULT_SECRETS_DIR))


@dataclass(frozen=True)
class XeroAppCredentials:
    client_id: str
    client_secret: str


def read_xero_app_credentials() -> Optional[XeroAppCredentials]:
    """Read the Xero Developer App's `client_id`/`client_secret` from
    ``<secrets_dir>/client_id`` / ``<secrets_dir>/client_secret``.

    Returns ``None`` (never raises) if either file is missing/unreadable
    — the honest "Xero not configured" state this dispatch requires
    (no real Xero Developer App exists yet). Read fresh from disk on
    every call — never cached — so a credential placed by the PL after
    this delivery lands takes effect without a process restart."""
    base = _secrets_dir()
    try:
        client_id = (base / "client_id").read_text(encoding="utf-8").strip()
        client_secret = (base / "client_secret").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not client_id or not client_secret:
        return None
    return XeroAppCredentials(client_id=client_id, client_secret=client_secret)


def _tokens_dir(entity_id: str) -> Path:
    return _secrets_dir() / "tokens" / entity_id


def _write_secret_file(path: Path, value: str) -> None:
    """Write `value` to `path` with the secret NEVER exposed at a wider
    mode than `_FILE_MODE`, even for a brief window. `Path.write_text`
    (the original implementation here) creates the file at the
    filesystem's default mode (typically `0644` under a common `0022`
    umask, since no `mode=` reaches `open()`) and only narrows it via a
    SEPARATE `chmod` call afterward — a real, live-findable secret-
    hygiene gap: between the create-with-content and the chmod, a
    freshly-written access/refresh token sits world/group-readable on
    disk. This is the same class of exposure this session's own live
    verification already caught once (insecure `644` editor-backup
    copies of the Xero `client_id`/`client_secret` files found and
    removed during Slice 2 setup) — closed here at the source by
    opening the file with `_FILE_MODE` baked into the `open()` call
    itself via `os.open`, so the file is never observable at any wider
    mode at any point in its existence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, _DIR_MODE)
    except OSError:
        pass  # best-effort on filesystems that do not support chmod (e.g. some CI mounts)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    finally:
        try:
            os.chmod(path, _FILE_MODE)
        except OSError:
            pass  # best-effort — the O_CREAT mode above already narrowed it for local filesystems that honor the mode argument; this is belt-and-braces for ones that apply umask to os.open too


@dataclass(frozen=True)
class StoredTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime


def write_connection_tokens(entity_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
    """Persist ``entity_id``'s current access/refresh token pair,
    overwriting whatever was there before (a token refresh always
    supersedes the prior pair entirely — Xero's own refresh-token
    rotation means the OLD refresh_token may already be invalid the
    moment a new one is issued)."""
    directory = _tokens_dir(entity_id)
    _write_secret_file(directory / "access_token", access_token)
    _write_secret_file(directory / "refresh_token", refresh_token)
    _write_secret_file(directory / "expires_at", expires_at.astimezone(timezone.utc).isoformat())


def read_connection_tokens(entity_id: str) -> Optional[StoredTokens]:
    """Read back ``entity_id``'s current token pair. Returns ``None``
    (never raises) if nothing is stored yet (an entity with a
    `XeroConnection` in `PENDING`, or one whose tokens were cleared by
    :func:`delete_connection_tokens`)."""
    directory = _tokens_dir(entity_id)
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
    return StoredTokens(access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)


def delete_connection_tokens(entity_id: str) -> None:
    """Remove ``entity_id``'s stored token pair entirely (called on
    disconnect/revoke — a `DISCONNECTED`/`REVOKED` connection must never
    leave a live token sitting on disk). Never raises if nothing was
    stored (idempotent-safe — a disconnect of an already-tokenless
    connection, e.g. one that never got past `PENDING`, is not an
    error)."""
    directory = _tokens_dir(entity_id)
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
        pass  # not empty, or already gone — never fatal


class TokenStoreProtocol(Protocol):
    """What ``services.xero.sync``'s orchestration actually depends on
    — the same 'depend on the Protocol, not the concrete class' shape
    every other adapter pair in this codebase uses
    (`ai.providers.litellm.client.LiteLLMClientProtocol`,
    `agent.claude_code.runner.ClaudeCodeOperatorRunnerProtocol`)."""

    def read(self, entity_id: str) -> Optional[StoredTokens]: ...

    def write(self, entity_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None: ...

    def delete(self, entity_id: str) -> None: ...


class FileTokenStore:
    """Production `TokenStoreProtocol`: a thin wrapper around this
    module's own `read_connection_tokens`/`write_connection_tokens`/
    `delete_connection_tokens` free functions (kept as free functions,
    not methods, because :func:`read_xero_app_credentials` — a
    DIFFERENT secret, the shared app credential, not a per-connection
    token — has no natural "one instance per entity" shape and stays a
    free function too; this class exists only to satisfy
    `TokenStoreProtocol` for dependency injection into
    `services.xero.sync`)."""

    def read(self, entity_id: str) -> Optional[StoredTokens]:
        return read_connection_tokens(entity_id)

    def write(self, entity_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        write_connection_tokens(entity_id, access_token=access_token, refresh_token=refresh_token, expires_at=expires_at)

    def delete(self, entity_id: str) -> None:
        delete_connection_tokens(entity_id)


class InMemoryTokenStore:
    """Development/test `TokenStoreProtocol` — never touches the real
    filesystem (mirrors this codebase's established dev/test-vs-
    production composition split for every other adapter, e.g.
    `InMemoryObjectStore` vs. `MinIOObjectStore` —
    `app/api/composition.py`'s own module docstring). A real developer
    machine has no `/opt/bagman/secrets/xero/` directory at all, so
    development/test composition must never depend on
    :class:`FileTokenStore`."""

    def __init__(self) -> None:
        self._by_entity: dict[str, StoredTokens] = {}

    def read(self, entity_id: str) -> Optional[StoredTokens]:
        return self._by_entity.get(entity_id)

    def write(self, entity_id: str, *, access_token: str, refresh_token: str, expires_at: datetime) -> None:
        self._by_entity[entity_id] = StoredTokens(
            access_token=access_token, refresh_token=refresh_token, expires_at=expires_at
        )

    def delete(self, entity_id: str) -> None:
        self._by_entity.pop(entity_id, None)


def file_permissions_are_governed(path: Path) -> bool:
    """True if ``path`` (a file) has mode `0600` — used only by this
    module's own tests to prove the write helpers above actually set
    restrictive permissions, not by any production code path."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode == _FILE_MODE
