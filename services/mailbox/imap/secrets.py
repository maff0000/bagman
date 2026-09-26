"""``matt@noust.ai`` IMAP credential discipline (CD-6 GUI-operations-
foundation follow-on WO — second mailbox provider).

Mirrors ``services.mailbox.microsoft.secrets``'s doctrine exactly, but
simpler — a static username/password pair, no OAuth tokens, no
rotation, no write path (the operator places these two files by hand;
BAGMAN never writes to this directory itself):

* Read from ``<secrets_dir>/{username,password}`` where
  ``_DEFAULT_SECRETS_DIR = "/opt/bagman/secrets/mail/noustai"`` —
  overridable via ``BAGMAN_NOUSTAI_MAIL_SECRETS_DIR`` purely for tests
  (mirrors ``BAGMAN_MICROSOFT_MAIL_SECRETS_DIR`` exactly).
* ``0700`` dir / ``0600`` files — the directory ALREADY EXISTS on the
  production host with ``0700`` permissions (created by the PL); this
  module never assumes it needs to create that directory for reading.
  There is deliberately no write helper here at all (unlike
  ``services.mailbox.microsoft.secrets``'s own
  ``_write_secret_file``/token-rotation machinery) — there is no token
  rotation for a static username/password pair, so there is nothing for
  BAGMAN to ever write into this directory.
* :func:`read_noustai_imap_credentials` is missing-file-tolerant and
  returns ``None`` (never raises) when either file is missing/empty/
  unreadable — the honest "not configured yet" state. NEITHER file
  exists yet on production at the time this module is delivered — the
  operator will place them independently of this deploy, exactly like
  the Microsoft Entra app credential triple before it. Read fresh from
  disk on EVERY call — never cached — so a credential the PL places
  after this delivery lands takes effect without a process restart.

Secret doctrine — read this twice
------------------------------------
Never logs, never returns via an HTTP response body, never appears in
an audit-event payload, never appears in an exception message. Every
function here returns a value the CALLER is responsible for treating as
secret; nothing here does that treatment for the caller (identical
discipline to ``services.mailbox.microsoft.secrets``/
``services.xero.secrets``). Grep this module's own diff for the literal
string ``password`` before finishing a change here — no code path may
let it leak into a log line, exception, or response.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Overridable via `BAGMAN_NOUSTAI_MAIL_SECRETS_DIR` purely for tests —
#: mirrors `services.mailbox.microsoft.secrets._SECRETS_DIR_ENV_VAR`
#: exactly.
_SECRETS_DIR_ENV_VAR = "BAGMAN_NOUSTAI_MAIL_SECRETS_DIR"
_DEFAULT_SECRETS_DIR = "/opt/bagman/secrets/mail/noustai"

_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _secrets_dir() -> Path:
    return Path(os.environ.get(_SECRETS_DIR_ENV_VAR, _DEFAULT_SECRETS_DIR))


@dataclass(frozen=True)
class NoustAIImapCredentials:
    username: str
    password: str


def read_noustai_imap_credentials() -> Optional[NoustAIImapCredentials]:
    """Read the `matt@noust.ai` IMAP username/password from
    `<secrets_dir>/{username,password}`.

    Returns `None` (never raises) if EITHER file is missing/empty/
    unreadable — the honest "IMAP mail not configured yet" state (no
    real credential has been placed on disk yet). Read fresh from disk
    on every call — never cached — see module docstring.
    """
    base = _secrets_dir()
    try:
        username = (base / "username").read_text(encoding="utf-8").strip()
        password = (base / "password").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not username or not password:
        return None
    return NoustAIImapCredentials(username=username, password=password)


def credentials_directory_permissions_are_governed() -> bool:
    """`True` if the secrets directory exists and carries mode `0700` —
    a diagnostic/test helper only (this module never creates or chmods
    the directory itself — see module docstring: the PL owns that,
    out-of-band). Returns `False` (never raises) if the directory does
    not exist at all."""
    import stat

    try:
        mode = stat.S_IMODE(_secrets_dir().stat().st_mode)
    except OSError:
        return False
    return mode == _DIR_MODE
