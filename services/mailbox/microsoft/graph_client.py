"""``MicrosoftOAuthClient``/``MicrosoftGraphClient`` — the real adapters
that speak to Microsoft identity and Microsoft Graph (CD-6 Slice 4).

Mirrors ``services.xero.client``'s own conventions exactly (the closest
and most authoritative precedent in this codebase for "one real
provider adapter + one deterministic ``Fake*`` substitute behind a
shared ``Protocol``", named directly by this delivery's own
instructions): stdlib ``urllib.request`` only (no new third-party HTTP
dependency), never raises for a transport/HTTP-level failure (the full
outcome space is returned as data via :class:`GraphOutcomeStatus`), no
eager I/O at construction time.

**No real Microsoft Entra app registration exists yet** (this
delivery's own hard constraint) — every test in this codebase drives
``FakeMicrosoftOAuthClient``/``FakeMicrosoftGraphClient``
(``services/mailbox/microsoft/fake_client.py``) instead of this module.
This module exists so the real, live adapter is ready the moment the PL
provisions real credentials, but it is never exercised against a live
endpoint by this delivery's own test suite.

Endpoints (Microsoft identity platform v2.0 + Microsoft Graph v1.0 —
re-verify against https://learn.microsoft.com if this module is ever
extended)
------------------------------------------------------------------------
* Authorization: ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize``
* Token exchange/refresh: ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token``
* Identity verification: ``GET https://graph.microsoft.com/v1.0/me``
* Delta query (per folder): ``GET https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta``
* Message content: ``GET https://graph.microsoft.com/v1.0/me/messages/{id}/$value``
* Scopes: ``openid profile offline_access User.Read Mail.Read`` — the
  architect's own minimum, read-only set. NEVER
  ``Mail.ReadWrite``/``Mail.Send``/an application-wide or admin-consent
  scope — see this module's own read-only discipline below.

``{tenant}`` is ALWAYS the real, governed ``tenant_id`` read via
``services.mailbox.microsoft.secrets.read_microsoft_app_credentials``
— this module never hardcodes ``/common`` as a permanent authority
(architect spec's explicit instruction); a caller with no real
tenant_id yet gets an honest ``CONFIG_ERROR`` outcome, never a crash.

Read-only discipline (architect spec — "build your FakeMicrosoftGraphClient
to only ever expose read operations")
------------------------------------------------------------------------
This module (and its Fake sibling) exposes ONLY: build an authorize
URL, exchange/refresh a token, read `/me`, read a folder's delta page,
read one message's raw content. No method anywhere in this module (or
`fake_client.py`) can mark a message read, move it, delete it, or send
mail — there is no such method to call, by construction, not merely by
convention. Any future PL-driven live acceptance run that finds this
module calling a write-shaped Graph endpoint is a genuine regression.
"""
from __future__ import annotations

import enum
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional, Protocol, Sequence

from services.mailbox.message import FOLDER_INBOX, FOLDER_JUNK

AUTHORIZE_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
GRAPH_DELTA_URL_TEMPLATE = "https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta"
GRAPH_MESSAGE_CONTENT_URL_TEMPLATE = "https://graph.microsoft.com/v1.0/me/messages/{message_id}/$value"

#: Architect's own minimum, read-only scope set — see module docstring.
OAUTH_SCOPES = "openid profile offline_access User.Read Mail.Read"

#: Microsoft Graph's own well-known folder ids for the exactly-two
#: folders this slice ever reads (architect spec: "never string-scrape
#: display names"). Keyed by this codebase's own generic
#: `services.mailbox.message` folder constants.
GRAPH_WELL_KNOWN_FOLDER = {FOLDER_INBOX: "inbox", FOLDER_JUNK: "junkemail"}

#: Every Graph request that touches a message identity uses this header
#: (architect spec — canonical uniqueness depends on it).
IMMUTABLE_ID_PREFER_HEADER = 'IdType="ImmutableId"'

#: CD-6 architect amendment (Stage A discovery, bounded — never a full
#: MIME `/$value` fetch). Requested on the SAME delta round-trip as
#: every other summary field: `internetMessageHeaders` carries the raw
#: header list (parsed for `Authentication-Results`/`Received-SPF` by
#: `_parse_auth_signals` below); `$expand=attachments(...)` carries
#: per-attachment filename/contentType/size WITHOUT attachment bytes.
GRAPH_DELTA_SELECT_FIELDS = (
    "id,internetMessageId,subject,sender,receivedDateTime,hasAttachments,internetMessageHeaders"
)
GRAPH_DELTA_EXPAND_ATTACHMENTS = "attachments($select=name,contentType,size)"

#: Graph's own header names this module parses for authentication
#: signals — matched case-insensitively (Graph/most MTAs are
#: inconsistent about header-name casing).
_AUTH_RESULTS_HEADER_NAMES = frozenset({"authentication-results", "arc-authentication-results"})
_RECEIVED_SPF_HEADER_NAME = "received-spf"

#: `Authentication-Results` embeds `spf=<verdict>`/`dkim=<verdict>`/
#: `dmarc=<verdict>` tokens per RFC 8601 — a deliberately simple,
#: bounded regex extraction (never a full RFC 8601 parser); a value
#: this cannot find stays `None` (this module never invents a verdict
#: the provider did not supply).
_AUTH_RESULT_TOKEN_PATTERN = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-zA-Z]+)")

DEFAULT_TIMEOUT_SECONDS = 15.0


class GraphOutcomeStatus(str, enum.Enum):
    """The full outcome space for one Microsoft identity/Graph call —
    never raised as an exception; always returned as data (mirrors
    `services.xero.client.XeroOutcomeStatus` exactly)."""

    OK = "OK"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    #: 401 — the access token was rejected/expired.
    AUTH_ERROR = "AUTH_ERROR"
    #: 403 — a genuine permission error (this app's own grant lacks a
    #: scope it should have) — distinct from AUTH_ERROR: a token
    #: refresh will never fix this.
    PERMISSION_ERROR = "PERMISSION_ERROR"
    #: 404 — the specific resource (e.g. one message's `$value`) no
    #: longer exists — used for the "message vanished between delta and
    #: MIME fetch" case.
    NOT_FOUND = "NOT_FOUND"
    #: 429 — rate limited; `retry_after_seconds` carries Graph's own
    #: `Retry-After` header when present.
    RATE_LIMITED = "RATE_LIMITED"
    #: Graph's own "delta token expired/invalid, full resync required"
    #: condition (a 410 Gone with a resync-required error code, per
    #: Graph's documented delta-query contract) — surfaced as its own
    #: distinct outcome so a caller NEVER silently restarts-and-
    #: duplicates; an explicit operator action is required (architect
    #: spec).
    RESYNC_REQUIRED = "RESYNC_REQUIRED"
    #: Any other non-2xx status.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    #: A 2xx response whose body could not be parsed as the expected
    #: shape.
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    #: BAGMAN's own local configuration is broken (no client_id/secret/
    #: tenant_id on disk yet) — never even reaches the network.
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass(frozen=True)
class MicrosoftTokenBundle:
    """A normalised OAuth token result — NEVER logged, NEVER placed in
    an audit-event payload, NEVER returned from an HTTP handler to the
    browser."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str = OAUTH_SCOPES


@dataclass(frozen=True)
class MicrosoftTokenResult:
    status: GraphOutcomeStatus
    tokens: Optional[MicrosoftTokenBundle] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class MicrosoftIdentity:
    """The real, server-verified identity claims from `GET /me` —
    architect spec: "compare the real verified `mail`/
    `userPrincipalName` claim against the mailbox's own stored
    `email_address` — never trust login_hint/browser-supplied
    address"."""

    mail: Optional[str]
    user_principal_name: Optional[str]


@dataclass(frozen=True)
class MicrosoftIdentityResult:
    status: GraphOutcomeStatus
    identity: Optional[MicrosoftIdentity] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GraphMessageSummary:
    """One message entry from a delta page. `immutable_id` is the
    provider's own immutable, case-sensitive message id (fetched with
    `Prefer: IdType="ImmutableId"` — architect spec)."""

    immutable_id: str
    internet_message_id: Optional[str]
    subject: Optional[str]
    sender_address: Optional[str]
    sender_display_name: Optional[str]
    received_at: Optional[datetime]
    has_attachments: bool
    #: `True` if this delta entry represents a REMOVAL (Graph delta
    #: queries can report a message as deleted/moved-out-of-folder via
    #: an `@removed` annotation) — this slice's sweep orchestration
    #: never treats a removal as new evidence; it exists purely so the
    #: adapter's own parsing is complete/honest rather than silently
    #: dropping a shape it does not recognise.
    removed: bool = False
    #: CD-6 architect amendment (Stage A discovery — "attachment
    #: metadata sufficient for discovery"). Bounded, discovery-only
    #: per-attachment metadata: `{"filename": ..., "content_type": ...,
    #: "size_bytes": ...}`. The REAL client requests this via
    #: `$expand=attachments($select=name,contentType,size)` on the SAME
    #: delta request (see `GRAPH_DELTA_SELECT_EXPAND` below) — never a
    #: second per-message round trip, and never attachment CONTENT.
    #: Empty when the delta item carried no `attachments` array (e.g.
    #: `has_attachments` is `false`, or the real endpoint's `$expand`
    #: was not honoured for this item).
    attachment_metadata: Sequence[Mapping[str, Optional[object]]] = field(default_factory=tuple)
    #: CD-6 architect amendment (§7 — authentication/spoofing). Parsed,
    #: best-effort SPF/DKIM/DMARC verdict tokens from the message's own
    #: `Authentication-Results`/`Received-SPF` headers — see
    #: `_parse_auth_signals` below. The REAL client requests raw headers
    #: via `$select=...,internetMessageHeaders` on the SAME delta
    #: request (never a full `/$value` MIME fetch). Keys present only
    #: when the provider actually returned a parseable header for that
    #: mechanism — this module never invents a verdict Graph did not
    #: supply.
    auth_signals: Mapping[str, Optional[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphDeltaPageResult:
    status: GraphOutcomeStatus
    messages: Sequence[GraphMessageSummary] = field(default_factory=tuple)
    #: Set when Graph's response carries `@odata.nextLink` — more pages
    #: remain in THIS round (in-memory pagination only, never the
    #: durable cursor — see `services/mailbox/cursor.py`).
    next_link: Optional[str] = None
    #: Set ONLY on the final page of a round (Graph's own
    #: `@odata.deltaLink`) — the opaque value the durable cursor
    #: advances to.
    delta_link: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GraphMessageContentResult:
    status: GraphOutcomeStatus
    #: The raw `message/rfc822` bytes, present only when `status ==
    #: GraphOutcomeStatus.OK`.
    content: Optional[bytes] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


class MicrosoftOAuthClientProtocol(Protocol):
    def is_configured(self) -> bool: ...

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> MicrosoftTokenResult: ...

    def refresh(self, *, refresh_token: str) -> MicrosoftTokenResult: ...

    def get_me(self, *, access_token: str) -> MicrosoftIdentityResult: ...


class MicrosoftGraphClientProtocol(Protocol):
    def fetch_delta(
        self,
        *,
        access_token: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ) -> GraphDeltaPageResult: ...

    def fetch_message_content(self, *, access_token: str, immutable_message_id: str) -> GraphMessageContentResult: ...


def _read_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:  # noqa: BLE001 - best-effort diagnostic only
        return ""


def _retry_after_seconds(exc: urllib.error.HTTPError) -> Optional[float]:
    value = exc.headers.get("Retry-After") if exc.headers is not None else None
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_resync_required(exc: urllib.error.HTTPError) -> bool:
    """Graph reports an expired/invalid delta token as a 410 Gone with
    `error.code == "ResyncRequired"` in the JSON body (Graph's own
    documented delta-query contract)."""
    if exc.code != 410:
        return False
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace"))
        return body.get("error", {}).get("code") == "ResyncRequired"
    except Exception:  # noqa: BLE001 - malformed body is simply "not resync"
        return False


class MicrosoftOAuthClient:
    """The real adapter for Microsoft identity endpoints. `credentials_provider`
    is called FRESH on every request that needs one (mirrors
    `services.xero.client.XeroOAuthClient`'s identical discipline)."""

    def __init__(self, *, credentials_provider=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if credentials_provider is None:
            from services.mailbox.microsoft.secrets import read_microsoft_app_credentials

            credentials_provider = read_microsoft_app_credentials
        self._credentials_provider = credentials_provider
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return self._credentials_provider() is not None

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        credentials = self._credentials_provider()
        tenant = credentials.tenant_id if credentials else "common"
        params = {
            "client_id": credentials.client_id if credentials else "",
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": OAUTH_SCOPES,
            "state": state,
        }
        return f"{AUTHORIZE_URL_TEMPLATE.format(tenant=tenant)}?{urllib.parse.urlencode(params)}"

    def _token_request(self, form: Mapping[str, str]) -> MicrosoftTokenResult:
        credentials = self._credentials_provider()
        if credentials is None:
            return MicrosoftTokenResult(
                status=GraphOutcomeStatus.CONFIG_ERROR,
                error_detail="Microsoft mail is not configured: no client_id/client_secret/tenant_id on disk yet",
            )

        body = urllib.parse.urlencode(
            {**form, "client_id": credentials.client_id, "client_secret": credentials.client_secret, "scope": OAUTH_SCOPES}
        ).encode("utf-8")
        request = urllib.request.Request(
            TOKEN_URL_TEMPLATE.format(tenant=credentials.tenant_id),
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = GraphOutcomeStatus.AUTH_ERROR if exc.code in (400, 401) else GraphOutcomeStatus.PROVIDER_ERROR
            return MicrosoftTokenResult(status=status, error_detail=f"HTTP {exc.code}: {_read_body(exc)}")
        except Exception as exc:  # noqa: BLE001 - classified below; never leaks raw
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = GraphOutcomeStatus.TIMEOUT if is_timeout else GraphOutcomeStatus.TRANSPORT_ERROR
            return MicrosoftTokenResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            import time as _time

            tokens = MicrosoftTokenBundle(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=datetime.fromtimestamp(_time.time() + int(payload["expires_in"]), tz=timezone.utc),
                scope=payload.get("scope", OAUTH_SCOPES),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return MicrosoftTokenResult(
                status=GraphOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Microsoft token response shape: {exc}",
            )
        return MicrosoftTokenResult(status=GraphOutcomeStatus.OK, tokens=tokens)

    def exchange_code(self, *, code: str, redirect_uri: str) -> MicrosoftTokenResult:
        return self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    def refresh(self, *, refresh_token: str) -> MicrosoftTokenResult:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def get_me(self, *, access_token: str) -> MicrosoftIdentityResult:
        request = urllib.request.Request(
            GRAPH_ME_URL,
            method="GET",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401,):
                return MicrosoftIdentityResult(
                    status=GraphOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                )
            return MicrosoftIdentityResult(
                status=GraphOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = GraphOutcomeStatus.TIMEOUT if is_timeout else GraphOutcomeStatus.TRANSPORT_ERROR
            return MicrosoftIdentityResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            identity = MicrosoftIdentity(mail=payload.get("mail"), user_principal_name=payload.get("userPrincipalName"))
        except (json.JSONDecodeError, TypeError) as exc:
            return MicrosoftIdentityResult(
                status=GraphOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not parse /me response: {exc}"
            )
        return MicrosoftIdentityResult(status=GraphOutcomeStatus.OK, identity=identity)


def _parse_auth_signals(headers: Optional[Sequence[Mapping]]) -> Mapping[str, Optional[str]]:
    """Best-effort SPF/DKIM/DMARC verdict extraction from a Graph
    `internetMessageHeaders` list (`[{"name": ..., "value": ...}, ...]`)
    — see `_AUTH_RESULT_TOKEN_PATTERN`'s own docstring for the bounded,
    non-exhaustive parsing this performs. Returns `{}` (never a dict of
    `None`s) when no relevant header was present at all — this module
    never fabricates a verdict Graph did not supply."""
    signals: dict[str, Optional[str]] = {}
    for header in headers or ():
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "")
        if name in _AUTH_RESULTS_HEADER_NAMES:
            for mechanism, verdict in _AUTH_RESULT_TOKEN_PATTERN.findall(value):
                signals.setdefault(mechanism.lower(), verdict.lower())
        elif name == _RECEIVED_SPF_HEADER_NAME:
            first_token = value.strip().split(" ", 1)[0].lower() if value.strip() else None
            if first_token:
                signals.setdefault("spf", first_token)
    return signals


def _parse_attachment_metadata(item: Mapping) -> Sequence[Mapping[str, Optional[object]]]:
    """Bounded, discovery-only per-attachment metadata from a Graph
    delta item's own (`$expand`-ed) `attachments` array — NEVER
    attachment content. Empty when the item carries no such array
    (e.g. a real endpoint response that did not honour `$expand`, or a
    message with no attachments)."""
    raw_attachments = item.get("attachments")
    if not isinstance(raw_attachments, list):
        return ()
    parsed = []
    for attachment in raw_attachments:
        if not isinstance(attachment, Mapping):
            continue
        parsed.append(
            {
                "filename": attachment.get("name"),
                "content_type": attachment.get("contentType"),
                "size_bytes": attachment.get("size"),
            }
        )
    return tuple(parsed)


def _parse_message_summary(item: Mapping) -> Optional[GraphMessageSummary]:
    if "@removed" in item:
        return GraphMessageSummary(
            immutable_id=item.get("id", ""),
            internet_message_id=None,
            subject=None,
            sender_address=None,
            sender_display_name=None,
            received_at=None,
            has_attachments=False,
            removed=True,
        )
    received_raw = item.get("receivedDateTime")
    received_at = None
    if isinstance(received_raw, str) and received_raw:
        try:
            received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
        except ValueError:
            received_at = None
    sender = (item.get("sender") or {}).get("emailAddress") or {}
    return GraphMessageSummary(
        immutable_id=item["id"],
        internet_message_id=item.get("internetMessageId"),
        subject=item.get("subject"),
        sender_address=sender.get("address"),
        sender_display_name=sender.get("name"),
        received_at=received_at,
        has_attachments=bool(item.get("hasAttachments", False)),
        removed=False,
        attachment_metadata=_parse_attachment_metadata(item),
        auth_signals=_parse_auth_signals(item.get("internetMessageHeaders")),
    )


class MicrosoftGraphClient:
    """The real adapter for the Graph mail delta/content endpoints."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def fetch_delta(
        self,
        *,
        access_token: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ) -> GraphDeltaPageResult:
        if next_link is not None:
            url = next_link
        elif delta_link is not None:
            url = delta_link
        else:
            well_known = GRAPH_WELL_KNOWN_FOLDER[folder]
            # `$select`/`$expand` are requested on every FIRST-page-of-
            # a-round request (bootstrap or fresh delta_link round) —
            # bounded (headers + attachment metadata only, never body/
            # `$value`), on the SAME request as everything else this
            # module already fetches (see GRAPH_DELTA_SELECT_FIELDS/
            # GRAPH_DELTA_EXPAND_ATTACHMENTS' own docstring above).
            params = {"$select": GRAPH_DELTA_SELECT_FIELDS, "$expand": GRAPH_DELTA_EXPAND_ATTACHMENTS}
            if bootstrap_timestamp is not None:
                iso = bootstrap_timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                params["$filter"] = f"receivedDateTime ge {iso}"
            base = GRAPH_DELTA_URL_TEMPLATE.format(folder=well_known)
            url = f"{base}?{urllib.parse.urlencode(params)}" if params else base

        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Prefer": IMMUTABLE_ID_PREFER_HEADER,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return GraphDeltaPageResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP 401: {_read_body(exc)}")
            if exc.code == 403:
                return GraphDeltaPageResult(
                    status=GraphOutcomeStatus.PERMISSION_ERROR, error_detail=f"HTTP 403: {_read_body(exc)}"
                )
            if exc.code == 429:
                return GraphDeltaPageResult(
                    status=GraphOutcomeStatus.RATE_LIMITED,
                    retry_after_seconds=_retry_after_seconds(exc),
                    error_detail=f"HTTP 429: {_read_body(exc)}",
                )
            if _is_resync_required(exc):
                return GraphDeltaPageResult(
                    status=GraphOutcomeStatus.RESYNC_REQUIRED, error_detail="delta token expired (ResyncRequired)"
                )
            return GraphDeltaPageResult(
                status=GraphOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = GraphOutcomeStatus.TIMEOUT if is_timeout else GraphOutcomeStatus.TRANSPORT_ERROR
            return GraphDeltaPageResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            messages = tuple(m for item in payload.get("value", []) if (m := _parse_message_summary(item)) is not None)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return GraphDeltaPageResult(
                status=GraphOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not parse delta response: {exc}"
            )
        return GraphDeltaPageResult(
            status=GraphOutcomeStatus.OK,
            messages=messages,
            next_link=payload.get("@odata.nextLink"),
            delta_link=payload.get("@odata.deltaLink"),
        )

    def fetch_message_content(self, *, access_token: str, immutable_message_id: str) -> GraphMessageContentResult:
        url = GRAPH_MESSAGE_CONTENT_URL_TEMPLATE.format(message_id=immutable_message_id)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {access_token}", "Prefer": IMMUTABLE_ID_PREFER_HEADER},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return GraphMessageContentResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP 401: {_read_body(exc)}")
            if exc.code == 403:
                return GraphMessageContentResult(
                    status=GraphOutcomeStatus.PERMISSION_ERROR, error_detail=f"HTTP 403: {_read_body(exc)}"
                )
            if exc.code == 404:
                return GraphMessageContentResult(status=GraphOutcomeStatus.NOT_FOUND, error_detail=f"HTTP 404: {_read_body(exc)}")
            if exc.code == 429:
                return GraphMessageContentResult(
                    status=GraphOutcomeStatus.RATE_LIMITED,
                    retry_after_seconds=_retry_after_seconds(exc),
                    error_detail=f"HTTP 429: {_read_body(exc)}",
                )
            return GraphMessageContentResult(
                status=GraphOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = GraphOutcomeStatus.TIMEOUT if is_timeout else GraphOutcomeStatus.TRANSPORT_ERROR
            return GraphMessageContentResult(status=status, error_detail=str(exc)[:500])

        return GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=raw)
