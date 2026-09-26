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
    #: 403 whose body carries no recognized transient-quota `reason` (see
    #: `_classify_403_body`/`_status_from_code`'s own decision table) — a genuine
    #: permission/scope/policy error this app's own grant lacks —
    #: distinct from AUTH_ERROR: a token refresh will never fix this.
    #: NEVER produced for a 403 whose structured
    #: `error.errors[].reason` is `rateLimitExceeded`/
    #: `userRateLimitExceeded` — that is `RATE_LIMITED` (see below), a
    #: CD-6 fix: a real production sweep once hit Google's own per-user
    #: quota mid-round, which this module misclassified as
    #: `PERMISSION_ERROR` and caused `services/mailbox/sweep.py` to mark
    #: the mailbox `connection_state -> ERROR` over nothing more than
    #: transient quota exhaustion.
    PERMISSION_ERROR = "PERMISSION_ERROR"
    #: 404 — the specific message no longer exists (vanished between
    #: list and get).
    NOT_FOUND = "NOT_FOUND"
    #: 429, OR a 403 whose structured `error.errors[].reason` is
    #: `rateLimitExceeded`/`userRateLimitExceeded` (Google's own real
    #: shape for per-user/per-project quota exhaustion — see
    #: `_classify_403_body`/`_status_from_code`'s own decision table);
    #: `retry_after_seconds` carries Gmail's own `Retry-After` header
    #: when present (either shape).
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
        self, *, access_token: str, label_id: Optional[str] = None, query: Optional[str] = None,
        page_token: Optional[str] = None, max_results: int = DEFAULT_PAGE_SIZE, include_spam_trash: bool = False,
    ) -> GmailMessageListPageResult: ...

    def fetch_message_metadata(
        self, *, access_token: str, message_id: str, metadata_headers: Sequence[str] = DEFAULT_METADATA_HEADERS
    ) -> GmailMessageMetadataResult: ...

    def fetch_message_raw(self, *, access_token: str, message_id: str) -> GmailMessageRawResult: ...


def _read_body(exc: urllib.error.HTTPError) -> str:
    """A bounded, human-eyeball-only diagnostic excerpt
    (<=`_DIAGNOSTIC_EXCERPT_CHARS` chars) of an `HTTPError`'s body —
    used ONLY by call sites that never need to `json.loads` the result
    (today: `GmailOAuthClient._token_request`'s own 400/401 diagnostic,
    and every non-403 status in `_classify_http_error`). NEVER use this
    for a body that will also be fed to `json.loads` for structured
    classification — see :func:`_read_body_bounded`'s own docstring for
    the real, proven production incident (a Gmail 403 quota-exceeded
    body over 500 characters, truncated mid-structure BEFORE parsing,
    silently misclassified as `PERMISSION_ERROR`) that resulted from
    exactly that conflation, and why 403 classification now uses a
    separate, genuinely-bounded-for-parsing read instead of this one."""
    try:
        return exc.read().decode("utf-8", errors="replace")[:_DIAGNOSTIC_EXCERPT_CHARS]
    except Exception:  # noqa: BLE001 - best-effort diagnostic only
        return ""


#: Hard maximum, in bytes, this module will EVER read from a 403
#: response body for STRUCTURED (`json.loads`) classification — see
#: :func:`_read_body_bounded`'s own docstring. 64 KiB is deliberately
#: generous headroom: Google's own real structured `error.errors[]`
#: bodies are typically well under a few KB (including a verbose,
#: human-readable `message` field), so this bound is never expected to
#: be hit by a genuine Google response — its purpose is purely a hard
#: ceiling against an oversized/malformed/hostile body consuming
#: unbounded memory, NOT a realistic day-to-day limit.
_MAX_ERROR_BODY_BYTES = 64 * 1024

#: The short, human-eyeball-only diagnostic excerpt length used for
#: `error_detail` strings derived from a 403 body — deliberately a
#: SEPARATE constant from `_MAX_ERROR_BODY_BYTES` above, and much
#: smaller. Conflating "how much to read for structured parsing" with
#: "how much to show a human in a log line" is the exact class of bug
#: this module's CD-6 fix corrects (see :func:`_read_body_bounded`) — a
#: parsing bound and a diagnostic-excerpt bound must never be the same
#: number, and a diagnostic string built from this excerpt must never
#: itself be re-parsed as machine-trusted structured data.
_DIAGNOSTIC_EXCERPT_CHARS = 500


def _read_body_bounded(exc: urllib.error.HTTPError) -> tuple[str, bool]:
    """The ONE place a 403 `HTTPError`'s body is ever read for
    structured-reason classification (mirrors `_classify_http_error`'s
    own "read exactly once" discipline for the whole exception — see
    that function's docstring).

    Why this exists — the real, proven production incident it fixes
    ------------------------------------------------------------------
    A real 222-candidate Gmail historical backfill hit Google's genuine
    quota-exceeded 403 (`error.errors[0].reason ==
    "rateLimitExceeded"`), which should have classified as
    `GmailOutcomeStatus.RATE_LIMITED`. It classified as
    `PERMISSION_ERROR` instead. Root cause: the OLD code path read the
    full body via `_read_body()`, which truncates to 500 characters
    BEFORE returning — a diagnostic-only bound that was then, by
    accident, ALSO used as the input to `json.loads()` for structured
    403 classification. Google's real quota-exceeded body (a verbose,
    human-readable `message` plus a nested `error.errors[]` array) is
    longer than 500 characters, so the truncated string cut the JSON off
    mid-structure; `json.loads` raised `JSONDecodeError`;
    `_reason_for_403` caught it and returned `None`; classification fell
    through to its documented fail-closed default, `PERMISSION_ERROR`.
    The bug was never a `_reason_for_403` logic defect — it was that a
    500-char DIAGNOSTIC bound was silently doubling as a PARSING bound.

    This function is the fix: a genuinely bounded read, sized for
    parsing (`_MAX_ERROR_BODY_BYTES`, 64 KiB — see that constant's own
    docstring), kept structurally separate from the 500-char
    `_DIAGNOSTIC_EXCERPT_CHARS` bound used for human-readable
    `error_detail` strings. The two bounds must never be merged again.

    Reads AT MOST `_MAX_ERROR_BODY_BYTES + 1` bytes via
    `HTTPError.read(amt)`'s own bounded-read support (`http.client
    .HTTPResponse.read(amt)` under the hood) — NEVER bare `.read()` with
    no argument, which would pull an arbitrarily large body fully into
    memory. The `+1` is deliberate: it is what lets this function tell
    "the real body was <= `_MAX_ERROR_BODY_BYTES`" apart from "the real
    body was longer than that" WITHOUT first reading the whole
    (possibly enormous) body — if exactly `_MAX_ERROR_BODY_BYTES + 1`
    bytes come back, the body was oversized; that extra byte is then
    discarded, never decoded or parsed either way.

    Returns `(body_text, oversized)`. `body_text` is decoded from AT
    MOST `_MAX_ERROR_BODY_BYTES` bytes (`errors="replace"`, matching
    this module's existing best-effort decode discipline elsewhere).
    When `oversized` is `True`, `body_text` is only a PREFIX of the real
    body — callers MUST NOT attempt to `json.loads` it: a prefix of a
    longer JSON document is not itself complete, valid JSON, and
    attempting to parse it risks either an exception or (worse) a
    coincidentally-balanced-but-wrong partial parse. On any read
    failure, returns `("", False)` — the caller's own existing
    fail-closed default (`PERMISSION_ERROR`) applies exactly as it
    always has for an unreadable body."""
    try:
        raw = exc.read(_MAX_ERROR_BODY_BYTES + 1)
    except Exception:  # noqa: BLE001 - best-effort; caller fails closed
        return "", False
    oversized = len(raw) > _MAX_ERROR_BODY_BYTES
    if oversized:
        raw = raw[:_MAX_ERROR_BODY_BYTES]
    try:
        return raw.decode("utf-8", errors="replace"), oversized
    except Exception:  # noqa: BLE001 - best-effort diagnostic only
        return "", oversized


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
            classified = _classify_http_error(exc)
            return GmailIdentityResult(status=classified.status, error_detail=classified.error_detail)
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
            classified = _classify_http_error(exc)
            return GmailLabelListResult(status=classified.status, error_detail=classified.error_detail)
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
        label_id: Optional[str] = None,
        query: Optional[str] = None,
        page_token: Optional[str] = None,
        max_results: int = DEFAULT_PAGE_SIZE,
        include_spam_trash: bool = False,
    ) -> GmailMessageListPageResult:
        # `label_id` is `None` for BAGMAN's own `ALL_RECEIVED` logical
        # stream (see `gmail_adapter.py`'s own module docstring's
        # "Label/folder normalisation" section) — Gmail's `messages.list`
        # simply omits `labelIds` entirely in that case, returning every
        # message regardless of label. `label_id`, when provided, is
        # still a SINGLE value only — Gmail's own `labelIds` parameter is
        # AND-semantics across multiple values ("messages with labels
        # that match ALL specified label ids"), never OR — kept for
        # flexibility/backward compatibility; nothing in this delivery
        # passes it any more.
        #
        # `include_spam_trash=True` is what makes Spam/Trash inclusion
        # structurally guaranteed for the `ALL_RECEIVED` stream — Gmail's
        # `messages.list` otherwise excludes those dispositions from
        # normal results even with no `labelIds` restriction at all.
        params: dict[str, str] = {"maxResults": str(max_results)}
        if label_id:
            params["labelIds"] = label_id
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        url = f"{GMAIL_MESSAGES_LIST_URL}?{urllib.parse.urlencode(params)}"

        try:
            payload = self._get_json(url, access_token)
        except urllib.error.HTTPError as exc:
            classified = _classify_http_error(exc)
            return GmailMessageListPageResult(
                status=classified.status,
                retry_after_seconds=classified.retry_after_seconds,
                error_detail=classified.error_detail,
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
            classified = _classify_http_error(exc)
            return GmailMessageMetadataResult(
                status=classified.status,
                retry_after_seconds=classified.retry_after_seconds,
                error_detail=classified.error_detail,
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
            classified = _classify_http_error(exc)
            return GmailMessageRawResult(
                status=classified.status,
                retry_after_seconds=classified.retry_after_seconds,
                error_detail=classified.error_detail,
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


#: Google's own structured `error.errors[].reason` values that mean
#: "this 403 is transient per-user/per-project QUOTA exhaustion, not a
#: genuine permission/scope/policy failure" — see
#: :func:`_classify_http_error`'s own docstring for the real body shape
#: this is matched against. Matched ONLY against the structured `reason`
#: field, NEVER against the human-readable `message` field (that field's
#: wording is not a stable, documented contract — see this module's own
#: CD-6 quota-classification fix history).
_TRANSIENT_QUOTA_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


def _reason_for_403(body_text: str) -> Optional[str]:
    """Best-effort extraction of Google's own structured
    `error.errors[].reason` from a 403 response body shaped like:
    ``{"error": {"errors": [{"domain": "usageLimits", "reason":
    "rateLimitExceeded", "message": "..."}], "code": 403, "message":
    "..."}}``. Scans every entry in `errors[]`: if ANY entry carries a
    known transient-quota reason (:data:`_TRANSIENT_QUOTA_REASONS`),
    that reason is returned; otherwise the FIRST entry's own `reason` is
    returned (a genuine, non-quota reason, e.g. `insufficientPermissions`).
    Returns `None` when the body is not valid JSON, or does not carry
    the expected `error.errors[]` shape, or `errors[]` is empty/carries
    no string `reason` at all — the caller treats `None` as "no
    recognized structured reason", which fails closed to
    `PERMISSION_ERROR` (never raises on a malformed/unexpected body)."""
    try:
        payload = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    errors = error.get("errors")
    if not isinstance(errors, list):
        return None

    reasons = [entry.get("reason") for entry in errors if isinstance(entry, Mapping) and isinstance(entry.get("reason"), str)]
    if not reasons:
        return None
    for reason in reasons:
        if reason in _TRANSIENT_QUOTA_REASONS:
            return reason
    return reasons[0]


def _status_from_code(code: int) -> GmailOutcomeStatus:
    """HTTP status -> `GmailOutcomeStatus` for every status EXCEPT 403.
    403 is deliberately NOT handled here — it alone needs to inspect the
    response body to distinguish transient quota exhaustion from a
    genuine permission failure (see :func:`_classify_403_body`), and
    that decision is made by `_classify_http_error` before this function
    is ever reached for a 403. Decision table: ``401 -> AUTH_ERROR``;
    ``404 -> NOT_FOUND``; ``429 -> RATE_LIMITED``; anything else
    (including a 403, which never reaches here) -> `PROVIDER_ERROR`."""
    if code == 401:
        return GmailOutcomeStatus.AUTH_ERROR
    if code == 404:
        return GmailOutcomeStatus.NOT_FOUND
    if code == 429:
        return GmailOutcomeStatus.RATE_LIMITED
    return GmailOutcomeStatus.PROVIDER_ERROR


@dataclass(frozen=True)
class _Classified403:
    """The result of classifying one 403 response body — `status` is
    always either `RATE_LIMITED` (a recognized transient-quota
    `reason`) or `PERMISSION_ERROR` (fail-closed default: a genuine
    non-quota `reason`, an oversized body, malformed JSON, or an
    unexpected shape). `diagnostic` is a SHORT, human-eyeball-only
    string — see :func:`_classify_403_body`'s own docstring for its
    exact shapes; it is built from the classification outcome, never
    from a re-parse of the raw body."""

    status: GmailOutcomeStatus
    diagnostic: str


def _classify_403_body(body_text: str, *, oversized: bool) -> _Classified403:
    """The full 403-body -> (`GmailOutcomeStatus`, diagnostic) decision,
    given the bounded `(body_text, oversized)` pair
    :func:`_read_body_bounded` produces.

    Decision table
    ------------------------------------------------------------------
    * `oversized` -> `PERMISSION_ERROR`, fail-closed WITHOUT ever
      attempting `json.loads` on `body_text` (it is only a PREFIX of the
      real body when oversized — see `_read_body_bounded`'s own
      docstring for why parsing a prefix of a longer document is never
      safe). Diagnostic names the bound was exceeded — never dumps the
      (possibly enormous) body itself.
    * Structured `reason` recognized as transient quota
      (`_TRANSIENT_QUOTA_REASONS`) -> `RATE_LIMITED`, diagnostic
      `"HTTP 403; reason=<reason>"`.
    * Structured `reason` present but NOT a recognized transient-quota
      value (e.g. `insufficientPermissions`) -> `PERMISSION_ERROR`,
      diagnostic `"HTTP 403; reason=<reason>"` (still names the real
      reason — this is a genuine, informative permission failure, not
      an unavailable one).
    * No recognized structured `reason` at all (malformed JSON,
      unexpected shape, empty `errors[]`) -> `PERMISSION_ERROR`,
      diagnostic `"HTTP 403; structured reason unavailable"`.

    NEVER inspects `body_text`'s `message` field for classification —
    only `_reason_for_403`'s own structured `error.errors[].reason` scan
    (see that function's docstring; message-text wording is not a
    stable, documented Google contract and must never influence
    classification). NEVER includes the raw body in a diagnostic string
    — these strings can end up in exception messages, audit payloads,
    and logs."""
    if oversized:
        return _Classified403(
            status=GmailOutcomeStatus.PERMISSION_ERROR,
            diagnostic="HTTP 403; structured reason unavailable (body exceeded bounded classification limit)",
        )
    reason = _reason_for_403(body_text)
    if reason is None:
        return _Classified403(status=GmailOutcomeStatus.PERMISSION_ERROR, diagnostic="HTTP 403; structured reason unavailable")
    if reason in _TRANSIENT_QUOTA_REASONS:
        return _Classified403(status=GmailOutcomeStatus.RATE_LIMITED, diagnostic=f"HTTP 403; reason={reason}")
    return _Classified403(status=GmailOutcomeStatus.PERMISSION_ERROR, diagnostic=f"HTTP 403; reason={reason}")


@dataclass(frozen=True)
class _ClassifiedHttpError:
    """The full result of classifying one `urllib.error.HTTPError` —
    produced by reading its body EXACTLY ONCE (see
    :func:`_classify_http_error`'s own docstring for why this dataclass
    exists at all: `HTTPError.read()` consumes a stream and can only be
    read once, so every field a call site needs from one exception must
    be derived in ONE place, never re-read at each call site)."""

    status: GmailOutcomeStatus
    retry_after_seconds: Optional[float]
    error_detail: str


def _classify_http_error(exc: urllib.error.HTTPError) -> _ClassifiedHttpError:
    """The ONE place a Gmail `HTTPError`'s body is ever read — every call
    site below (`get_profile`/`list_labels`/`list_messages`/
    `fetch_message_metadata`/`fetch_message_raw`) calls this exactly
    once per exception and uses its three fields, instead of separately
    calling a status-classifier + `_retry_after_seconds` + a body-read
    helper (which, now that classifying a 403 needs to inspect the
    body, would exhaust `exc`'s stream on the first read and leave any
    later read at that same call site returning an empty string —
    silently breaking `error_detail` for every Gmail HTTP error path in
    this client, not just 403s).

    CD-6 fix — why 403 no longer shares a body-read/bound with every
    other status
    ------------------------------------------------------------------
    A real production incident (see :func:`_read_body_bounded`'s own
    docstring for the full account) was caused by a single 500-char
    diagnostic-only truncation being reused, by accident, as the input
    to `json.loads()` for 403 structured classification — a real Google
    quota-exceeded body longer than 500 characters was cut off
    mid-structure, `json.loads` raised, and classification silently
    fell through to `PERMISSION_ERROR` instead of the correct
    `RATE_LIMITED`. This function now branches on `exc.code` BEFORE
    reading the body at all: a 403 uses :func:`_read_body_bounded` (a
    genuinely large, parsing-sized bound, `_MAX_ERROR_BODY_BYTES`) and
    :func:`_classify_403_body`; every other status uses the original
    `_read_body` (a small, diagnostic-only bound) and
    :func:`_status_from_code`, exactly as before — no body-parsing was
    ever needed for non-403 statuses, and none is added now. Either
    way, `exc`'s body is still read EXACTLY ONCE per exception."""
    retry_after_seconds = _retry_after_seconds(exc)
    code = exc.code

    if code == 403:
        body_text, oversized = _read_body_bounded(exc)
        classified = _classify_403_body(body_text, oversized=oversized)
        return _ClassifiedHttpError(status=classified.status, retry_after_seconds=retry_after_seconds, error_detail=classified.diagnostic)

    body_text = _read_body(exc)
    status = _status_from_code(code)
    return _ClassifiedHttpError(status=status, retry_after_seconds=retry_after_seconds, error_detail=f"HTTP {code}: {body_text}")


def _status_for_transport_error(exc: Exception) -> GmailOutcomeStatus:
    is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
    return GmailOutcomeStatus.TIMEOUT if is_timeout else GmailOutcomeStatus.TRANSPORT_ERROR
