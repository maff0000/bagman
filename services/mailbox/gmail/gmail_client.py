"""``GmailOAuthClient``/``GmailClient`` — the real adapters that speak
Gmail's OAuth2 endpoints and the Gmail API v1 (CD-6 GUI-operations-
foundation follow-on WO — third mailbox provider).

Mirrors ``services.mailbox.microsoft.graph_client``'s own conventions
exactly (the closest and most authoritative precedent in this codebase
for "one real provider adapter + one deterministic ``Fake*`` substitute
behind a shared ``Protocol``"): stdlib ``urllib.request`` only (no new
third-party HTTP dependency, and in particular NEVER
``googleapiclient``/``google_auth_oauthlib`` — see this package's own
``__init__.py`` docstring for the hard, test-enforced constraint), never
raises for a transport/HTTP-level failure (the full outcome space is
returned as data via :class:`GmailOutcomeStatus`), no eager I/O at
construction time.

Why stdlib HTTP, never Google's own SDK — the same reasoning
``services.mailbox.microsoft.graph_client``'s own module docstring
applies to ``msal``, applied identically here to
``googleapiclient``/``google_auth_oauthlib``
------------------------------------------------------------------------
The Gmail API is a plain REST API (``https://www.googleapis.com/gmail/v1/...``),
its OAuth2 token endpoint (``https://oauth2.googleapis.com/token``) and
authorization endpoint
(``https://accounts.google.com/o/oauth2/v2/auth``) are plain HTTPS
endpoints — nothing about them requires Google's own client library.
Using stdlib ``urllib``/``json`` keeps this package's dependency surface
identical to every other provider adapter in this codebase (Microsoft's
own ``graph_client.py``, IMAP's own ``imap_client.py`` via ``imaplib``)
and keeps the outcome-as-data / never-raise-for-transport discipline
uniform across all three providers, rather than adopting a differently-
shaped (exception-raising, session-object-based) idiom a third-party SDK
would impose.

**No real Google Cloud OAuth app exists yet** (this delivery's own hard
constraint) — every test in this codebase drives
``FakeGmailOAuthClient``/``FakeGmailClient``
(``services/mailbox/gmail/fake_gmail_client.py``) instead of this
module. This module exists so the real, live adapter is ready the
moment the PL provisions real credentials, but it is never exercised
against a live endpoint by this delivery's own test suite.

Endpoints
------------------------------------------------------------------------
* Authorization: ``https://accounts.google.com/o/oauth2/v2/auth``
* Token exchange/refresh: ``https://oauth2.googleapis.com/token``
* Identity verification: ``GET https://www.googleapis.com/gmail/v1/users/me/profile``
  (Gmail API's own ``users.getProfile`` — mirrors Microsoft's own
  ``GET /me`` identity-confirmation call in spirit: a plain
  authenticated GET returning JSON, used to confirm which real Gmail
  address was actually authorized (never trusted from which mailbox row
  initiated the flow). This endpoint is used, rather than Google's
  generic OAuth2 ``userinfo`` endpoint, because it is fully covered by
  this module's own ``gmail.readonly`` scope — no additional
  ``openid``/``email``/``profile`` scope grant is ever needed, and the
  response's own ``emailAddress`` field is exactly as authoritative a
  server-verified claim as ``/me``'s ``mail``/``userPrincipalName``
  (Google does not cryptographically verify anything for us either
  way — trust here rests entirely on the token having come from
  Google's own token endpoint over TLS, matching Microsoft's own
  identical trust model). Historical note for future maintainers: an
  earlier version of this module used the generic userinfo endpoint;
  that failed this delivery's first live acceptance attempt because
  userinfo needs ``openid``/``email``/``profile`` scope, which BAGMAN
  deliberately does not request (see "Scope" below) — ``users.getProfile``
  is the corrected, deliberate design, not a stopgap.
* Labels: ``GET https://www.googleapis.com/gmail/v1/users/me/labels``
* Message list (paginated, per-label): ``GET https://www.googleapis.com/gmail/v1/users/me/messages``
* Message metadata (headers-only, bounded): ``GET https://www.googleapis.com/gmail/v1/users/me/messages/{id}?format=metadata&metadataHeaders=...``
* Message raw (full MIME, governed fetch only): ``GET https://www.googleapis.com/gmail/v1/users/me/messages/{id}?format=raw``
* Scope: ``https://www.googleapis.com/auth/gmail.readonly`` — READ-ONLY
  ONLY. Never ``gmail.modify``/``gmail.send``/``gmail.compose``/any
  broader scope. ``access_type=offline`` (required to receive a refresh
  token at all) and ``prompt=consent`` (Google otherwise only issues a
  refresh token on a user's VERY FIRST authorization of this app;
  ``prompt=consent`` forces the consent screen — and therefore a fresh
  refresh token — on every authorization, which this delivery needs
  since an operator may need to reconnect/re-authorize later) are always
  included on the authorize URL.

Read-only discipline (mirrors ``services.mailbox.microsoft.graph_client``'s
own "no method anywhere in this module can mark a message read, move
it, delete it, or send mail" doctrine, and
``services.mailbox.imap.imap_client``'s identical "their absence IS the
read-only guarantee" doctrine)
------------------------------------------------------------------------
This module exposes ONLY: build an authorize URL, exchange/refresh a
token, read the authenticated mailbox's own profile identity
(`users.getProfile`), list labels, list message ids for one label
(paginated), read one message's metadata headers, read one message's
raw RFC822 bytes. There is NO ``messages.send``, NO ``messages.modify``
(label changes), NO ``messages.trash``, NO ``messages.delete``, NO
``drafts.*``, and no generic "call any Gmail endpoint" escape hatch —
these methods do not exist ANYWHERE on this class or its ``Fake*``
sibling, by construction, not merely by convention. Any future PL-driven
live acceptance run that finds this module calling a write-shaped Gmail
endpoint is a genuine regression.

Outcome-status pattern (mirrors ``graph_client.GraphOutcomeStatus``/
``imap_client.ImapOutcomeStatus``)
------------------------------------------------------------------------
:class:`GmailOutcomeStatus` deliberately reuses the SAME member
names/string values as ``GraphOutcomeStatus``/``ImapOutcomeStatus`` for
every status the providers share — see
``services.mailbox.imap.imap_client``'s own module docstring for the
full "why shared vocabulary, via plain string-enum equality" reasoning,
which applies identically here: ``GmailOutcomeStatus.AUTH_ERROR ==
GraphOutcomeStatus.AUTH_ERROR`` is `True`, which is what lets
``services/mailbox/sweep.py``'s existing status comparisons keep working
UNCHANGED for a Gmail-sourced result, with ZERO changes to that file's
own status-comparison logic beyond the one additive
``evaluate_message_authentication`` dispatch branch this delivery adds.
Gmail never actually PRODUCES ``RESYNC_REQUIRED`` (this module's own
bounded/resumable discovery design — see ``gmail_adapter.py``'s own
module docstring — uses Gmail's ``nextPageToken`` + a governed ``after:``
search-window lower bound, never a Graph-style delta token that can
expire) — kept purely for vocabulary parity, exactly like IMAP's own
identical unused members.

Token refresh — Google does NOT always return a new refresh_token
------------------------------------------------------------------------
A REAL, documented Gmail-specific difference from Microsoft's own OAuth
behaviour: Google's token-refresh response normally omits
``refresh_token`` entirely (Google keeps the existing one valid rather
than rotating it on every refresh, unlike Microsoft's own rotate-every-
time doctrine — see ``services.mailbox.microsoft.secrets``'s own module
docstring for that contrast). :meth:`GmailOAuthClient.refresh` therefore
returns a :class:`GmailTokenBundle` whose ``refresh_token`` is
``Optional[str]`` — ``None`` means "unchanged, keep whatever this
mailbox already has stored" — never a false/empty value written over a
still-valid stored refresh token. See ``gmail_adapter.py``'s own
token-persistence logic for exactly how this is merged before being
written to ``services.mailbox.gmail.secrets``.
"""
from __future__ import annotations

import base64
import enum
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Protocol, Sequence

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Gmail API's own `users.getProfile` — the identity-verification
#: endpoint (see module docstring's "Endpoints" section for why this
#: replaced Google's generic OAuth2 userinfo endpoint). Same host
#: convention as every other `GMAIL_*_URL` constant below.
GMAIL_PROFILE_URL = "https://www.googleapis.com/gmail/v1/users/me/profile"
GMAIL_LABELS_URL = "https://www.googleapis.com/gmail/v1/users/me/labels"
GMAIL_MESSAGES_LIST_URL = "https://www.googleapis.com/gmail/v1/users/me/messages"
GMAIL_MESSAGE_GET_URL_TEMPLATE = "https://www.googleapis.com/gmail/v1/users/me/messages/{message_id}"

#: The architect's own minimum, read-only scope — see module docstring.
#: NEVER `gmail.modify`/`gmail.send`/`gmail.compose`/a broader scope.
OAUTH_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

#: `access_type=offline` — required to receive a refresh token at all.
#: `prompt=consent` — forces the consent screen (and therefore a fresh
#: refresh token) on EVERY authorization, not just a user's very first
#: one ever (Google's own documented default behaviour) — see module
#: docstring.
_AUTHORIZE_EXTRA_PARAMS = {"access_type": "offline", "prompt": "consent"}

#: The metadata-only headers this module requests on every bounded
#: headers-only fetch — sufficient for `services/mailbox/sweep.py`'s own
#: Stage-A discovery (subject/sender/internet-message-id/received-at)
#: PLUS every header `services.mailbox.gmail.authentication
#: .assess_gmail_authentication` structurally requires to reach a
#: decisive (non-`UNKNOWN`) verdict on a real, legitimately authenticated
#: message. `Received` is MANDATORY, not optional: that selector's own
#: eligibility criterion 3 (see that module's docstring) requires the
#: nearest PRECEDING header before the candidate `Authentication-Results`
#: header to be a `Received` header whose `by` host is exactly
#: `mx.google.com` — with `Received` never fetched, the selector can
#: never find an eligible candidate and always fails closed to `UNKNOWN`,
#: even for genuinely, legitimately authenticated mail (the exact
#: metadata-contract bug this tuple now fixes: the selector's own logic
#: was already correct and proven, but the adapter was never asking
#: Gmail for the one header that logic depends on). `ARC-Seal`/
#: `ARC-Message-Signature` are included alongside the already-present
#: `ARC-Authentication-Results` so the selector's bounded evidence output
#: (`arc_seal_header_count`/`arc_message_signature_header_count`)
#: reflects true ARC presence on the real production path too — ARC
#: remains strictly evidence-only and never gates (see that module's
#: docstring, "ARC — evidence-only, never gates"); adding these headers
#: to what is REQUESTED changes no gating logic whatsoever. Bounded and
#: explicit — never `metadataHeaders` omitted entirely (Gmail returns NO
#: headers at all for `format=metadata` without at least one
#: `metadataHeaders` value).
#:
#: `Content-Disposition` was added on top of the above (discovery-
#: signal fix, CD-6 follow-on) for exactly one purpose: a bounded,
#: metadata-only ATTACHMENT-PRESENCE signal — NOT real MIME part
#: enumeration. Gmail's `format=metadata` fetch has no parsed
#: `payload.parts` tree, so this module can never know real per-
#: attachment filenames/content-types for Gmail (that stays exactly as
#: true as it already was — see `gmail_adapter.py`'s own
#: `_derive_has_attachments` docstring). The TOP-LEVEL
#: `Content-Disposition` header, when present, is one more honest,
#: bounded clue (alongside the top-level `Content-Type` this module
#: already requested) that the message carries at least one attachment
#: — nothing more.
DEFAULT_METADATA_HEADERS: tuple[str, ...] = (
    "Subject",
    "From",
    "Message-ID",
    "Date",
    "Content-Type",
    "Content-Disposition",
    "Received",
    "Authentication-Results",
    "ARC-Authentication-Results",
    "ARC-Seal",
    "ARC-Message-Signature",
)

#: Bounded page size for one `messages.list` round — mirrors
#: `services.mailbox.imap.imap_adapter._PAGE_SIZE`'s own "bounded,
#: observable, resumable" reasoning, applied to Gmail's own
#: `nextPageToken` pagination mechanics (see `gmail_adapter.py`'s own
#: module docstring for the full delta/resume-link design this bounds).
DEFAULT_PAGE_SIZE = 25

DEFAULT_TIMEOUT_SECONDS = 15.0


class GmailOutcomeStatus(str, enum.Enum):
    """The full outcome space for one Gmail OAuth2/API call — never
    raised as an exception; always returned as data. See module
    docstring's "Outcome-status pattern" section for why these member
    names/values deliberately mirror `GraphOutcomeStatus`/
    `ImapOutcomeStatus`."""

    OK = "OK"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    #: 401 — the access token was rejected/expired.
    AUTH_ERROR = "AUTH_ERROR"
    #: 403 — a genuine permission/quota error this app's own grant
    #: lacks — distinct from AUTH_ERROR: a token refresh will never fix
    #: this.
    PERMISSION_ERROR = "PERMISSION_ERROR"
    #: 404 — the specific message no longer exists (vanished between
    #: list and get).
    NOT_FOUND = "NOT_FOUND"
    #: 429 — rate limited; `retry_after_seconds` carries Gmail's own
    #: `Retry-After` header when present.
    RATE_LIMITED = "RATE_LIMITED"
    #: Kept only for vocabulary parity with `GraphOutcomeStatus` — this
    #: module has no Graph-style expiring delta token (see module
    #: docstring's "Token refresh" section and `gmail_adapter.py`'s own
    #: bounded/resumable design); never produced by this client.
    RESYNC_REQUIRED = "RESYNC_REQUIRED"
    #: Any other non-2xx status.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    #: A 2xx response whose body could not be parsed as the expected
    #: shape.
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    #: BAGMAN's own local configuration is broken (no client_id/
    #: client_secret on disk yet) — never even reaches the network.
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass(frozen=True)
class GmailTokenBundle:
    """A normalised OAuth token result — NEVER logged, NEVER placed in
    an audit-event payload, NEVER returned from an HTTP handler to the
    browser.

    `refresh_token` is `Optional[str]`: always populated on a real
    `exchange_code` result (a `None` there is treated as
    `MALFORMED_RESPONSE` — see module docstring, "access_type=offline +
    prompt=consent should always yield one"); usually `None` on a
    `refresh` result (Google's own documented behaviour — see module
    docstring's "Token refresh" section), meaning "unchanged, the
    mailbox's already-stored refresh_token remains valid"."""

    access_token: str
    refresh_token: Optional[str]
    expires_at: datetime
    scope: str = OAUTH_SCOPE


@dataclass(frozen=True)
class GmailTokenResult:
    status: GmailOutcomeStatus
    tokens: Optional[GmailTokenBundle] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailIdentity:
    """The real, server-verified identity claim from `GET
    /gmail/v1/users/me/profile` (`emailAddress`) — architect spec:
    "confirm which Gmail address was actually authorized... never
    silently trust which mailbox row initiated the flow"."""

    email: Optional[str]


@dataclass(frozen=True)
class GmailIdentityResult:
    status: GmailOutcomeStatus
    identity: Optional[GmailIdentity] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailLabel:
    """One entry from `GET /labels`. `label_type` is Gmail's own
    `"system"`/`"user"` discriminator — used by
    `gmail_adapter.py::discover_monitored_folders` to identify
    `INBOX`/`SPAM`/`TRASH` (all `"system"`), never guessed from
    `name` (mirrors Microsoft's own "never guessed from display name"
    well-known-folder doctrine)."""

    label_id: str
    name: str
    label_type: str


@dataclass(frozen=True)
class GmailLabelListResult:
    status: GmailOutcomeStatus
    labels: Sequence[GmailLabel] = field(default_factory=tuple)
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailMessageListPageResult:
    """One page of `messages.list` — ids/threadIds ONLY (Gmail's own
    list endpoint never returns headers/content — see module docstring).
    `next_page_token` is Gmail's own `nextPageToken`, present only when
    more pages remain in THIS round."""

    status: GmailOutcomeStatus
    message_ids: Sequence[str] = field(default_factory=tuple)
    next_page_token: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailMessageMetadata:
    message_id: str
    #: `[{"name": ..., "value": ...}, ...]` — the SAME shape
    #: `GraphMessageSummary.raw_headers`/`ImapMessageSummary.raw_headers`
    #: carry.
    raw_headers: Sequence[Mapping[str, Optional[str]]] = field(default_factory=tuple)
    #: Gmail's own current label set for this message at fetch time —
    #: used only for diagnostics/provenance, never for identity.
    label_ids: Sequence[str] = field(default_factory=tuple)
    #: Gmail's own `internalDate` (ms since the Unix epoch, UTC) —
    #: `gmail_adapter.py` uses this both as `received_at` when no usable
    #: `Date` header is present, and as the resumable round watermark
    #: (see that module's own docstring).
    internal_date: Optional[datetime] = None


@dataclass(frozen=True)
class GmailMessageMetadataResult:
    status: GmailOutcomeStatus
    metadata: Optional[GmailMessageMetadata] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailMessageRawResult:
    status: GmailOutcomeStatus
    #: The raw `message/rfc822` bytes (base64url-decoded from Gmail's
    #: own `raw` field), present only when `status == OK`.
    content: Optional[bytes] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


class GmailOAuthClientProtocol(Protocol):
    def is_configured(self) -> bool: ...

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> GmailTokenResult: ...

    def refresh(self, *, refresh_token: str) -> GmailTokenResult: ...


class GmailClientProtocol(Protocol):
    def get_profile(self, *, access_token: str) -> GmailIdentityResult: ...

    def list_labels(self, *, access_token: str) -> GmailLabelListResult: ...

    def list_messages(
        self, *, access_token: str, label_id: str, query: Optional[str] = None, page_token: Optional[str] = None,
        max_results: int = DEFAULT_PAGE_SIZE,
    ) -> GmailMessageListPageResult: ...

    def fetch_message_metadata(
        self, *, access_token: str, message_id: str, metadata_headers: Sequence[str] = DEFAULT_METADATA_HEADERS
    ) -> GmailMessageMetadataResult: ...

    def fetch_message_raw(self, *, access_token: str, message_id: str) -> GmailMessageRawResult: ...


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


def _decode_base64url(value: str) -> bytes:
    """Gmail's own `raw` field is base64url-encoded, frequently WITHOUT
    the standard `=` padding `base64.urlsafe_b64decode` requires — pad
    it out explicitly rather than relying on the caller to have done
    so."""
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded)


def _parse_raw_headers(headers: Optional[Sequence[Mapping]]) -> tuple:
    """Normalise Gmail's own `payload.headers` list into the plain
    `{"name": ..., "value": ...}` tuple shape every other provider's own
    `raw_headers` already carries — mirrors
    `services.mailbox.microsoft.graph_client._parse_raw_headers`
    exactly."""
    return tuple(
        {"name": h.get("name"), "value": h.get("value")} for h in (headers or ()) if isinstance(h, Mapping)
    )


class GmailOAuthClient:
    """The real adapter for Gmail's OAuth2 endpoints.
    `credentials_provider` is called FRESH on every request that needs
    one (mirrors `services.mailbox.microsoft.graph_client
    .MicrosoftOAuthClient`'s identical discipline)."""

    def __init__(self, *, credentials_provider=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if credentials_provider is None:
            from services.mailbox.gmail.secrets import read_gmail_app_credentials

            credentials_provider = read_gmail_app_credentials
        self._credentials_provider = credentials_provider
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return self._credentials_provider() is not None

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        credentials = self._credentials_provider()
        params = {
            "client_id": credentials.client_id if credentials else "",
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPE,
            "state": state,
            **_AUTHORIZE_EXTRA_PARAMS,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _token_request(self, form: Mapping[str, str], *, require_refresh_token: bool) -> GmailTokenResult:
        credentials = self._credentials_provider()
        if credentials is None:
            return GmailTokenResult(
                status=GmailOutcomeStatus.CONFIG_ERROR,
                error_detail="Gmail is not configured: no client_id/client_secret on disk yet",
            )

        body = urllib.parse.urlencode(
            {**form, "client_id": credentials.client_id, "client_secret": credentials.client_secret}
        ).encode("utf-8")
        request = urllib.request.Request(
            TOKEN_URL, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = GmailOutcomeStatus.AUTH_ERROR if exc.code in (400, 401) else GmailOutcomeStatus.PROVIDER_ERROR
            return GmailTokenResult(status=status, error_detail=f"HTTP {exc.code}: {_read_body(exc)}")
        except Exception as exc:  # noqa: BLE001 - classified below; never leaks raw
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = GmailOutcomeStatus.TIMEOUT if is_timeout else GmailOutcomeStatus.TRANSPORT_ERROR
            return GmailTokenResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            refresh_token = payload.get("refresh_token")
            if require_refresh_token and not refresh_token:
                return GmailTokenResult(
                    status=GmailOutcomeStatus.MALFORMED_RESPONSE,
                    error_detail=(
                        "Gmail token exchange returned no refresh_token — access_type=offline + "
                        "prompt=consent should always yield one"
                    ),
                )
            tokens = GmailTokenBundle(
                access_token=payload["access_token"],
                refresh_token=refresh_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"])),
                scope=payload.get("scope", OAUTH_SCOPE),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return GmailTokenResult(
                status=GmailOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Gmail token response shape: {exc}",
            )
        return GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=tokens)

    def exchange_code(self, *, code: str, redirect_uri: str) -> GmailTokenResult:
        return self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            require_refresh_token=True,
        )

    def refresh(self, *, refresh_token: str) -> GmailTokenResult:
        return self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}, require_refresh_token=False
        )


class GmailClient:
    """The real adapter for the Gmail API v1 read-only endpoints (labels/
    messages.list/messages.get) — see module docstring's "Read-only
    discipline" section: no mutating method exists anywhere on this
    class."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def _get_json(self, url: str, access_token: str):
        request = urllib.request.Request(
            url, method="GET", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_profile(self, *, access_token: str) -> GmailIdentityResult:
        """`GET /gmail/v1/users/me/profile` — the ONE identity-
        verification code path (see module docstring's "Endpoints"
        section for why this replaced Google's generic userinfo
        endpoint). A plain authenticated GET, same pattern/error
        handling as every other read call on this class."""
        try:
            payload = self._get_json(GMAIL_PROFILE_URL, access_token)
        except urllib.error.HTTPError as exc:
            return GmailIdentityResult(status=_status_for_http_error(exc), error_detail=f"HTTP {exc.code}: {_read_body(exc)}")
        except Exception as exc:  # noqa: BLE001
            return GmailIdentityResult(status=_status_for_transport_error(exc), error_detail=str(exc)[:500])

        try:
            if not isinstance(payload, Mapping):
                raise TypeError(f"users.getProfile response was not a JSON object: {payload!r}")
            email = payload.get("emailAddress")
            if email is not None and not isinstance(email, str):
                raise TypeError(f"emailAddress was not a string: {email!r}")
            identity = GmailIdentity(email=email)
        except (AttributeError, TypeError) as exc:
            return GmailIdentityResult(
                status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not parse users.getProfile response: {exc}"
            )
        return GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=identity)

    def list_labels(self, *, access_token: str) -> GmailLabelListResult:
        try:
            payload = self._get_json(GMAIL_LABELS_URL, access_token)
        except urllib.error.HTTPError as exc:
            return GmailLabelListResult(status=_status_for_http_error(exc), error_detail=f"HTTP {exc.code}: {_read_body(exc)}")
        except Exception as exc:  # noqa: BLE001
            return GmailLabelListResult(status=_status_for_transport_error(exc), error_detail=str(exc)[:500])

        try:
            labels = tuple(
                GmailLabel(label_id=item["id"], name=item.get("name", item["id"]), label_type=item.get("type", "user"))
                for item in payload.get("labels", [])
            )
        except (KeyError, TypeError) as exc:
            return GmailLabelListResult(
                status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not parse labels response: {exc}"
            )
        return GmailLabelListResult(status=GmailOutcomeStatus.OK, labels=labels)

    def list_messages(
        self,
        *,
        access_token: str,
        label_id: str,
        query: Optional[str] = None,
        page_token: Optional[str] = None,
        max_results: int = DEFAULT_PAGE_SIZE,
    ) -> GmailMessageListPageResult:
        # A SINGLE `labelIds` value only — Gmail's own `labelIds`
        # parameter is AND-semantics across multiple values ("messages
        # with labels that match ALL specified label ids"), never OR —
        # so this method deliberately queries ONE label at a time; see
        # `gmail_adapter.py`'s own module docstring for how the adapter
        # composes one "folder" per monitored label rather than a single
        # multi-label query.
        params: dict[str, str] = {"labelIds": label_id, "maxResults": str(max_results)}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        url = f"{GMAIL_MESSAGES_LIST_URL}?{urllib.parse.urlencode(params)}"

        try:
            payload = self._get_json(url, access_token)
        except urllib.error.HTTPError as exc:
            return GmailMessageListPageResult(
                status=_status_for_http_error(exc),
                retry_after_seconds=_retry_after_seconds(exc),
                error_detail=f"HTTP {exc.code}: {_read_body(exc)}",
            )
        except Exception as exc:  # noqa: BLE001
            return GmailMessageListPageResult(status=_status_for_transport_error(exc), error_detail=str(exc)[:500])

        try:
            message_ids = tuple(item["id"] for item in payload.get("messages", []) or ())
        except (KeyError, TypeError) as exc:
            return GmailMessageListPageResult(
                status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not parse messages.list response: {exc}"
            )
        return GmailMessageListPageResult(
            status=GmailOutcomeStatus.OK, message_ids=message_ids, next_page_token=payload.get("nextPageToken")
        )

    def fetch_message_metadata(
        self, *, access_token: str, message_id: str, metadata_headers: Sequence[str] = DEFAULT_METADATA_HEADERS
    ) -> GmailMessageMetadataResult:
        params = [("format", "metadata")] + [("metadataHeaders", h) for h in metadata_headers]
        url = f"{GMAIL_MESSAGE_GET_URL_TEMPLATE.format(message_id=message_id)}?{urllib.parse.urlencode(params)}"

        try:
            payload = self._get_json(url, access_token)
        except urllib.error.HTTPError as exc:
            return GmailMessageMetadataResult(
                status=_status_for_http_error(exc),
                retry_after_seconds=_retry_after_seconds(exc),
                error_detail=f"HTTP {exc.code}: {_read_body(exc)}",
            )
        except Exception as exc:  # noqa: BLE001
            return GmailMessageMetadataResult(status=_status_for_transport_error(exc), error_detail=str(exc)[:500])

        try:
            internal_date_raw = payload.get("internalDate")
            internal_date = (
                datetime.fromtimestamp(int(internal_date_raw) / 1000.0, tz=timezone.utc) if internal_date_raw else None
            )
            metadata = GmailMessageMetadata(
                message_id=payload["id"],
                raw_headers=_parse_raw_headers((payload.get("payload") or {}).get("headers")),
                label_ids=tuple(payload.get("labelIds", []) or ()),
                internal_date=internal_date,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return GmailMessageMetadataResult(
                status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not parse message metadata response: {exc}"
            )
        return GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=metadata)

    def fetch_message_raw(self, *, access_token: str, message_id: str) -> GmailMessageRawResult:
        url = f"{GMAIL_MESSAGE_GET_URL_TEMPLATE.format(message_id=message_id)}?format=raw"
        try:
            payload = self._get_json(url, access_token)
        except urllib.error.HTTPError as exc:
            return GmailMessageRawResult(
                status=_status_for_http_error(exc),
                retry_after_seconds=_retry_after_seconds(exc),
                error_detail=f"HTTP {exc.code}: {_read_body(exc)}",
            )
        except Exception as exc:  # noqa: BLE001
            return GmailMessageRawResult(status=_status_for_transport_error(exc), error_detail=str(exc)[:500])

        try:
            content = _decode_base64url(payload["raw"])
        except (KeyError, TypeError, ValueError) as exc:
            return GmailMessageRawResult(
                status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"could not decode message raw content: {exc}"
            )
        return GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=content)


def _status_for_http_error(exc: urllib.error.HTTPError) -> GmailOutcomeStatus:
    if exc.code == 401:
        return GmailOutcomeStatus.AUTH_ERROR
    if exc.code == 403:
        return GmailOutcomeStatus.PERMISSION_ERROR
    if exc.code == 404:
        return GmailOutcomeStatus.NOT_FOUND
    if exc.code == 429:
        return GmailOutcomeStatus.RATE_LIMITED
    return GmailOutcomeStatus.PROVIDER_ERROR


def _status_for_transport_error(exc: Exception) -> GmailOutcomeStatus:
    is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
    return GmailOutcomeStatus.TIMEOUT if is_timeout else GmailOutcomeStatus.TRANSPORT_ERROR
