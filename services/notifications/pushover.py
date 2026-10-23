"""``PushoverClient`` — a narrow Pushover HTTP client (PID §118.I,
WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.6).

Stdlib-only (``urllib.request``/``urllib.error``), never a new
third-party HTTP dependency — mirrors
``services.mailbox.gmail.gmail_client``'s own convention exactly (this
codebase uses no third-party HTTP library anywhere; see that module's
own docstring for the full "why stdlib, never a vendor SDK" reasoning,
which applies identically here — Pushover's own API
(https://api.pushover.net/1/messages.json) is a plain HTTPS POST
endpoint, nothing about it requires a vendor SDK).

No new compiled dependency (WO §6.A.6's own "Native-ARM64 provenance
note")
------------------------------------------------------------------------
This module introduces no C-extension/compiled dependency of any kind —
confirmed, and recorded here honestly per the governing Work Order's own
instruction not to fabricate an ARM64-compatibility concern that does
not genuinely exist for this specific item.

Credential discipline — the existing `BAGMAN_*_FILE` convention
------------------------------------------------------------------------
Mirrors ``persistence.postgres.session``'s own
``BAGMAN_DB_PASSWORD_FILE`` doctrine exactly (an env var names a file
path; the file's contents, read fresh on every call — never cached —
are the actual secret; the password/token value itself is NEVER a
literal in code). :func:`read_pushover_credentials` is missing-file-
tolerant and returns `None` (never raises) when either file is
absent/unreadable — the honest "Pushover not configured yet" state,
not blocking this delivery's own repository acceptance (PID §118.I:
"not blocking this document's acceptance, only blocking eventual
production activation").

Never logs, never returns, never persists the credential value itself
(mirrors every other secrets module in this codebase — e.g.
``services.mailbox.gmail.secrets``'s own identical discipline).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"
DEFAULT_TIMEOUT_SECONDS = 10.0

#: `BAGMAN_*_FILE` secret-file convention (see module docstring) — new
#: env vars this delivery introduces; overridable purely for tests.
API_TOKEN_FILE_ENV_VAR = "BAGMAN_PUSHOVER_API_TOKEN_FILE"
USER_KEY_FILE_ENV_VAR = "BAGMAN_PUSHOVER_USER_KEY_FILE"
_DEFAULT_API_TOKEN_FILE = "/run/secrets/bagman_pushover_api_token"
_DEFAULT_USER_KEY_FILE = "/run/secrets/bagman_pushover_user_key"


class PushoverOutcomeStatus(str, Enum):
    """Mirrors `services.mailbox.gmail.gmail_client.GmailOutcomeStatus`'s
    own outcome-as-data pattern — this client NEVER raises for a
    transport/HTTP-level failure; the full outcome space is returned as
    data instead."""

    OK = "OK"
    CONFIG_ERROR = "CONFIG_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"


@dataclass(frozen=True)
class PushoverCredentials:
    api_token: str
    user_key: str


@dataclass(frozen=True)
class PushoverSendResult:
    """`status == OK` means Pushover's own API ACCEPTED the send
    request — never a claim of confirmed device delivery (PID §118.E.2:
    "`ACCEPTED` reflects only that the provider's API accepted the send
    request")."""

    status: PushoverOutcomeStatus
    http_status: Optional[int] = None
    provider_request_id: Optional[str] = None
    error_detail: Optional[str] = None


def _read_secret_file(path_str: str) -> Optional[str]:
    try:
        value = Path(path_str).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def read_pushover_credentials() -> Optional[PushoverCredentials]:
    """Read Pushover's `api_token`/`user_key` via the `BAGMAN_*_FILE`
    convention (see module docstring). Read fresh from disk on every
    call — never cached — so a credential the operator places after
    this delivery lands takes effect without a process restart. Returns
    `None` (never raises) if either file is missing/empty/unreadable."""
    api_token_file = os.environ.get(API_TOKEN_FILE_ENV_VAR, _DEFAULT_API_TOKEN_FILE)
    user_key_file = os.environ.get(USER_KEY_FILE_ENV_VAR, _DEFAULT_USER_KEY_FILE)
    api_token = _read_secret_file(api_token_file)
    user_key = _read_secret_file(user_key_file)
    if not api_token or not user_key:
        return None
    return PushoverCredentials(api_token=api_token, user_key=user_key)


class PushoverClient:
    """The real adapter for Pushover's `messages.json` endpoint. No
    eager I/O at construction time (mirrors every other provider client
    in this codebase)."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def send(self, *, credentials: PushoverCredentials, title: str, message: str) -> PushoverSendResult:
        """Send one notification. Never raises for a transport/HTTP-
        level failure — see :class:`PushoverOutcomeStatus`. Content is
        restricted entirely by the CALLER (PID §118.I: "mailbox
        identity, failure class, and relevant timestamps/pointers —
        never message content, tokens, or accounting data") — this
        client itself imposes no content restriction of its own; it
        only ever sends exactly the `title`/`message` strings it is
        given."""
        body = urllib.parse.urlencode(
            {
                "token": credentials.api_token,
                "user": credentials.user_key,
                "title": title,
                "message": message,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            PUSHOVER_MESSAGES_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
                http_status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            http_status = exc.code
            detail = raw.decode("utf-8", errors="replace")[:500]
            if exc.code in (401, 403):
                status = PushoverOutcomeStatus.AUTH_ERROR
            elif exc.code == 429:
                status = PushoverOutcomeStatus.RATE_LIMITED
            else:
                status = PushoverOutcomeStatus.PROVIDER_ERROR
            return PushoverSendResult(status=status, http_status=http_status, error_detail=detail)
        except Exception as exc:  # noqa: BLE001 - classified below; never leaks a raw exception
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = PushoverOutcomeStatus.TIMEOUT if is_timeout else PushoverOutcomeStatus.TRANSPORT_ERROR
            return PushoverSendResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return PushoverSendResult(
                status=PushoverOutcomeStatus.MALFORMED_RESPONSE,
                http_status=http_status,
                error_detail=f"could not parse Pushover response shape: {exc}",
            )

        if payload.get("status") == 1:
            return PushoverSendResult(
                status=PushoverOutcomeStatus.OK, http_status=http_status, provider_request_id=payload.get("request")
            )
        errors = payload.get("errors") or []
        return PushoverSendResult(
            status=PushoverOutcomeStatus.PROVIDER_ERROR,
            http_status=http_status,
            provider_request_id=payload.get("request"),
            error_detail="; ".join(errors) if errors else "Pushover reported a non-1 status with no errors[] detail",
        )


class FakePushoverClient:
    """Deterministic test substitute — never performs real I/O. Mirrors
    `services.mailbox.gmail.fake_gmail_client`'s own `Fake*` convention:
    records every `send()` call for assertions; the outcome returned is
    fully controlled by the caller via the constructor's `result`."""

    def __init__(self, *, result: Optional[PushoverSendResult] = None) -> None:
        self.calls: list[dict] = []
        self._result = result or PushoverSendResult(
            status=PushoverOutcomeStatus.OK, http_status=200, provider_request_id="fake-request-id"
        )

    def send(self, *, credentials: PushoverCredentials, title: str, message: str) -> PushoverSendResult:
        self.calls.append({"title": title, "message": message})
        return self._result
