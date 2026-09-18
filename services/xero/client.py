"""``XeroOAuthClient``/``XeroAccountingClient`` — the real adapters that
speak to Xero's identity and Accounting API (CD-6 Slice 2, PID §102.1,
architect spec §3/§4).

Mirrors ``ai/providers/litellm/client.py``'s own conventions exactly
(the closest, most recent precedent in this codebase for "one real
adapter + one deterministic ``Fake*`` substitute behind a shared
``Protocol``", and this WI's own explicit instruction to follow it):
stdlib ``urllib.request`` only (no new third-party HTTP dependency —
this codebase's established `requirements.txt` doctrine, see that
file's own header notes), never raises for a transport/HTTP-level
failure (the full outcome space is returned as data via
:class:`XeroOutcomeStatus`), and no eager I/O at construction time.

Endpoints (PID §102.1's own researched, authoritative contract —
re-verify against https://developer.xero.com if this module is ever
extended beyond `GET /Accounts`)
------------------------------------------------------------------------
* Authorization:  ``https://login.xero.com/identity/connect/authorize``
* Token exchange/refresh: ``https://identity.xero.com/connect/token``
* Tenant discovery: ``GET https://api.xero.com/connections``
* Chart of Accounts: ``GET https://api.xero.com/api.xro/2.0/Accounts``
  with header ``Xero-Tenant-Id: <tenant_id>`` — ``tenant_id`` is ALWAYS
  a value BAGMAN itself resolved server-side from a prior
  ``GET /connections`` call, NEVER taken from browser/client input
  (architect spec §3's explicit anti-tenant-substitution instruction;
  ``app/api/routers/xero.py`` never accepts a `tenant_id` as a request
  parameter for this reason).
* Contacts: ``GET https://api.xero.com/api.xro/2.0/Contacts`` — CD-6
  supplier-domain-correlation addition (see below).
* Purchase (bill-side, ``ACCPAY``) Invoices:
  ``GET https://api.xero.com/api.xro/2.0/Invoices?where=Type%3D%3D%22ACCPAY%22``
  — same addition; the server-side ``where`` filter is how this method
  itself enforces "never treat a sales-side (``ACCREC``) customer
  invoice as purchase history" (see :meth:`XeroAccountingClient
  .list_purchase_invoices`'s own docstring).
* Scopes: ``openid profile email offline_access accounting.settings.read``
  (PID §102.1 — confirmed current/non-deprecated for a read-only Chart-
  of-Accounts sync; see that section for the devblog citation).

Scope extension (architect-authorized, bounded Xero-assisted supplier-
domain correlation ahead of any bulk mailbox-domain approval)
------------------------------------------------------------------------
:data:`OAUTH_SCOPES` gained two further READ-ONLY scopes,
``accounting.contacts.read`` and ``accounting.invoices.read`` — the
minimum necessary additional read scopes for
:mod:`services.xero.supplier_correlation` to read Contacts/Invoices and
correlate them against still-OPEN ``MAILBOX_DOMAIN_REVIEW`` Needs You
items. No write-capable scope (no ``.write`` token of any kind) was
added, and no broader accounting-transaction write authority was
requested — this component remains READ/REFERENCE-ONLY (see this
module's own docstring and ``services/xero/component.yaml``'s
``prohibited: xero_write_endpoints``). Changing this constant only
changes what scope string gets REQUESTED the next time an operator
goes through the existing connect/reconnect OAuth flow (real,
operator-driven, browser-based, explicitly deferred) — it does not
retroactively grant anything against the currently-connected Infosecurs
tenant (which today only actually holds ``accounting.settings.read``),
and it never itself triggers or simulates any OAuth call.

No new persisted canonical domain model for Contacts/Invoices
------------------------------------------------------------------------
Unlike :class:`RawXeroAccount`/:class:`XeroAccount` (`services.xero
.account`), the new :class:`RawXeroContact`/:class:`RawXeroPurchaseInvoice`
below have no canonical, persisted, ``entity_id``-scoped counterpart —
Xero remains the system of record for its own Contacts/Invoices; BAGMAN
reads them transiently, correlates, and discards them (see
``services.xero.supplier_correlation``'s own module docstring). This
codebase's own established precedent (``contracts/xero/`` carries a
schema for the CANONICAL, persisted ``XeroAccount`` only — never for
the transient ``RawXeroAccount`` the client layer returns) is followed
here deliberately: no ``contracts/xero/bagman.xero_contact.v1.schema.json``
or ``bagman.xero_purchase_invoice.v1.schema.json`` exists, since nothing
here crosses a persisted/API-boundary contract — a documented judgment
call, not an oversight.

No client_id/client_secret exist yet (PID §102.1's own stated
constraint for this dispatch) — this module's constructors accept them
as plain strings (read by the caller via
``services.xero.secrets.read_xero_app_credentials``, which is
missing-file-tolerant and returns ``None`` rather than raising) so this
module itself never touches the filesystem; a caller with no real
credentials yet gets an honest ``CONFIG_ERROR`` outcome the moment a
real call is attempted, never a crash.
"""
from __future__ import annotations

import enum
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol

from core.errors import ValidationError
from services.xero.account import RawXeroAccount

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
ACCOUNTS_URL = "https://api.xero.com/api.xro/2.0/Accounts"
CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"
INVOICES_URL = "https://api.xero.com/api.xro/2.0/Invoices"

#: Xero's own server-side `where` filter, applied ONLY to
#: `list_purchase_invoices` — restricts the returned set to bill-side
#: (`ACCPAY`) invoices, never sales-side (`ACCREC`) customer invoices
#: (architect's own explicit instruction: "Do not treat customers... as
#: suppliers merely because they exist in Xero" — enforced here, at the
#: query itself, not left for a later caller to remember).
_ACCPAY_ONLY_QUERY = urllib.parse.urlencode({"where": 'Type=="ACCPAY"'})

#: PID §102.1 — the exact, confirmed-current scope set for this slice's
#: read-only Chart-of-Accounts sync, EXTENDED (architect-authorized —
#: see module docstring's "Scope extension" section) with two further
#: READ-ONLY scopes for the bounded Xero-assisted supplier-domain
#: correlation capability: `accounting.contacts.read` and
#: `accounting.invoices.read`. No write-capable scope. `offline_access`
#: is what makes a `refresh_token` exist at all (never re-prompting an
#: operator for ordinary token refresh, architect spec §3).
OAUTH_SCOPES = (
    "openid profile email offline_access accounting.settings.read "
    "accounting.contacts.read accounting.invoices.read"
)

DEFAULT_TIMEOUT_SECONDS = 15.0


class XeroOutcomeStatus(str, enum.Enum):
    """The full outcome space for one Xero HTTP call — never raised as
    an exception; always returned as data (mirrors
    `ai.providers.litellm.client.LiteLLMOutcomeStatus` exactly, see
    module docstring)."""

    OK = "OK"
    #: No HTTP response was ever received (connection refused, DNS
    #: failure, connect-phase timeout).
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    #: 401/403 — the access token was rejected or access was revoked.
    AUTH_ERROR = "AUTH_ERROR"
    #: 429 — rate limited; `retry_after_seconds` carries Xero's own
    #: `Retry-After` header value when present.
    RATE_LIMITED = "RATE_LIMITED"
    #: Any other non-2xx status.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    #: A 2xx response whose body could not be parsed as the expected
    #: shape.
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    #: BAGMAN's own local configuration is broken (no client_id/secret
    #: on disk yet, or similar) — never even reaches the network.
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass(frozen=True)
class XeroTokenBundle:
    """A normalised OAuth token result — NEVER logged, NEVER placed in
    an audit-event payload, NEVER returned from an HTTP handler to the
    browser (architect spec §3/§24). Only
    ``services.xero.secrets.write_connection_tokens`` and this module's
    own callers ever hold one in memory, for the duration of one
    request."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str = OAUTH_SCOPES


@dataclass(frozen=True)
class XeroTokenResult:
    status: XeroOutcomeStatus
    tokens: Optional[XeroTokenBundle] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class XeroConnectionInfo:
    """One entry from `GET /connections` — tenant discovery (architect
    spec §3)."""

    connection_id: str
    tenant_id: str
    tenant_name: Optional[str]
    tenant_type: Optional[str]


@dataclass(frozen=True)
class XeroConnectionsResult:
    status: XeroOutcomeStatus
    connections: tuple[XeroConnectionInfo, ...] = ()
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class XeroAccountsResult:
    status: XeroOutcomeStatus
    accounts: tuple[RawXeroAccount, ...] = ()
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class RawXeroContact:
    """The plain, provider-shaped fields one `GET /Contacts` entry
    yields — transient, never persisted (see module docstring's "No new
    persisted canonical domain model" section). `is_customer`/
    `is_supplier` are Xero's OWN self-reported flags, captured verbatim
    but NEVER conflated with "has real purchase-invoice history" (the
    stronger signal `services.xero.supplier_correlation` actually
    weights — see that module's own docstring)."""

    contact_id: str
    name: str
    email_address: Optional[str]
    is_customer: bool
    is_supplier: bool
    contact_status: Optional[str]


@dataclass(frozen=True)
class XeroContactsResult:
    status: XeroOutcomeStatus
    contacts: tuple[RawXeroContact, ...] = ()
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class RawXeroPurchaseInvoice:
    """The plain, provider-shaped fields one `GET /Invoices` entry
    (server-side filtered to `Type=="ACCPAY"`) yields — transient,
    never persisted. `invoice_type` is captured and defensively
    re-validated (see `_parse_raw_purchase_invoice`) rather than
    silently trusting the server-side filter alone worked. `status`
    (`AUTHORISED`/`PAID`/`VOIDED`/`DRAFT`/...) is captured verbatim —
    this client layer never decides which statuses count as "real"
    purchase history; that weighting judgment belongs to
    `services.xero.supplier_correlation` alone."""

    invoice_id: str
    contact_id: Optional[str]
    invoice_type: str
    invoice_date: Optional[datetime]
    status: Optional[str]


@dataclass(frozen=True)
class XeroInvoicesResult:
    status: XeroOutcomeStatus
    invoices: tuple[RawXeroPurchaseInvoice, ...] = ()
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


class XeroOAuthClientProtocol(Protocol):
    def is_configured(self) -> bool: ...

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> XeroTokenResult: ...

    def refresh(self, *, refresh_token: str) -> XeroTokenResult: ...

    def list_connections(self, *, access_token: str) -> XeroConnectionsResult: ...


class XeroAccountingClientProtocol(Protocol):
    def list_accounts(self, *, tenant_id: str, access_token: str) -> XeroAccountsResult: ...

    def list_contacts(self, *, tenant_id: str, access_token: str) -> XeroContactsResult: ...

    def list_purchase_invoices(self, *, tenant_id: str, access_token: str) -> XeroInvoicesResult: ...


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


class XeroOAuthClient:
    """The real adapter for Xero's identity endpoints (PID §102.1).

    ``credentials_provider`` is called FRESH on every request that
    needs a credential (never cached beyond one call's stack frame) —
    mirrors `ai.providers.litellm.client.LiteLLMClient
    ._authorization_header`'s own identical "read the secret fresh off
    disk so a rotated/newly-placed credential takes effect without a
    process restart" discipline. Defaults to
    `services.xero.secrets.read_xero_app_credentials`, which is itself
    missing-file-tolerant (returns `None`, never raises) — exactly the
    honest "Xero not configured" state this dispatch requires, since no
    real Xero Developer App exists yet."""

    def __init__(
        self,
        *,
        credentials_provider=None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if credentials_provider is None:
            from services.xero.secrets import read_xero_app_credentials

            credentials_provider = read_xero_app_credentials
        self._credentials_provider = credentials_provider
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        """True if a real Xero Developer App credential is currently
        readable — the router's own pre-flight check
        (`app/api/routers/xero.py::connect`) BEFORE it ever mints an
        `OAuthState`/transitions a connection into `PENDING`, so
        attempting to connect while Xero is not configured yet fails
        immediately and honestly rather than leaving a dangling
        `PENDING` row nothing can ever complete."""
        return self._credentials_provider() is not None

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Build the full `login.xero.com` authorize URL — never
        performs I/O (pure string construction); this is what
        `POST /internal/xero/connect` returns for the browser to
        navigate to (architect spec §3: 'the browser may initiate a
        connect flow' but owns nothing about the token exchange
        itself). `client_id` is rendered empty when Xero is not yet
        configured — the caller (`app/api/routers/xero.py`) checks
        configuration explicitly BEFORE ever calling this, so an empty
        `client_id` here in practice only ever appears in this
        function's own unit tests, never in a real response."""
        credentials = self._credentials_provider()
        params = {
            "response_type": "code",
            "client_id": credentials.client_id if credentials else "",
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPES,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _token_request(self, form: Mapping[str, str]) -> XeroTokenResult:
        credentials = self._credentials_provider()
        if credentials is None:
            return XeroTokenResult(
                status=XeroOutcomeStatus.CONFIG_ERROR,
                error_detail="Xero not configured: no client_id/client_secret on disk yet",
            )

        body = urllib.parse.urlencode(form).encode("utf-8")
        basic_auth = _basic_auth_header(credentials.client_id, credentials.client_secret)
        request = urllib.request.Request(
            TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": basic_auth,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = XeroOutcomeStatus.AUTH_ERROR if exc.code in (400, 401) else XeroOutcomeStatus.PROVIDER_ERROR
            return XeroTokenResult(status=status, error_detail=f"HTTP {exc.code}: {_read_body(exc)}")
        except Exception as exc:  # noqa: BLE001 - classified below; never leaks raw
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
            return XeroTokenResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            expires_in = int(payload["expires_in"])
            tokens = XeroTokenBundle(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc),
                scope=payload.get("scope", OAUTH_SCOPES),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return XeroTokenResult(
                status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Xero token response shape: {exc}",
            )
        return XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=tokens)

    def exchange_code(self, *, code: str, redirect_uri: str) -> XeroTokenResult:
        return self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    def refresh(self, *, refresh_token: str) -> XeroTokenResult:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def list_connections(self, *, access_token: str) -> XeroConnectionsResult:
        # Authenticated purely via the bearer `access_token` — unlike
        # `_token_request` (which needs the app's own client_id/secret
        # for HTTP Basic auth against the token endpoint), `/connections`
        # needs no app-level credential at all, so there is nothing to
        # check for "configured" here.
        request = urllib.request.Request(
            CONNECTIONS_URL, method="GET", headers={"Authorization": f"Bearer {access_token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return XeroConnectionsResult(
                    status=XeroOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                )
            return XeroConnectionsResult(
                status=XeroOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
            return XeroConnectionsResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            connections = tuple(
                XeroConnectionInfo(
                    connection_id=str(item.get("id", "")),
                    tenant_id=item["tenantId"],
                    tenant_name=item.get("tenantName"),
                    tenant_type=item.get("tenantType"),
                )
                for item in payload
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return XeroConnectionsResult(
                status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Xero /connections response shape: {exc}",
            )
        return XeroConnectionsResult(status=XeroOutcomeStatus.OK, connections=connections)


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    import base64

    token = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _parse_xero_wire_datetime(value: Any) -> Optional[datetime]:
    """The ONE place every caller in this module parses a Xero-supplied
    date/timestamp string — shared by `_parse_raw_account`'s own
    `UpdatedDateUTC` parsing and `_parse_raw_purchase_invoice`'s `Date`
    parsing (the WO's own explicit instruction: reuse this exact same
    helper, do not reinvent it for invoices).

    Xero's own wire format is "/Date(1700000000000+0000)/" for some
    endpoints, but the modern JSON Accounting API returns plain
    ISO-8601 for both `UpdatedDateUTC` and `Date` — handled generically
    here rather than special-cased, since either form parses via
    `fromisoformat` once normalised, and a value this module cannot
    parse is simply left `None` (never fabricated) rather than crashing
    the whole call over one cosmetic field."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_raw_account(item: Mapping[str, Any]) -> RawXeroAccount:
    parsed_updated = _parse_xero_wire_datetime(item.get("UpdatedDateUTC"))

    return RawXeroAccount(
        account_id=item["AccountID"],
        code=item.get("Code"),
        name=item["Name"],
        type=item["Type"],
        account_class=item.get("Class"),
        tax_type=item.get("TaxType"),
        status=item.get("Status"),
        show_in_expense_claims=item.get("ShowInExpenseClaims"),
        reporting_code=item.get("ReportingCode"),
        reporting_code_name=item.get("ReportingCodeName"),
        updated_date_utc=parsed_updated,
    )


def _parse_raw_contact(item: Mapping[str, Any]) -> RawXeroContact:
    email = item.get("EmailAddress")
    return RawXeroContact(
        contact_id=item["ContactID"],
        name=item["Name"],
        email_address=email if isinstance(email, str) and email.strip() else None,
        is_customer=bool(item.get("IsCustomer", False)),
        is_supplier=bool(item.get("IsSupplier", False)),
        contact_status=item.get("ContactStatus"),
    )


def _parse_raw_purchase_invoice(item: Mapping[str, Any]) -> RawXeroPurchaseInvoice:
    invoice_type = item["Type"]
    if invoice_type != "ACCPAY":
        # Defensive re-validation (WO's own explicit instruction) — the
        # server-side `where=Type=="ACCPAY"` filter is trusted, but
        # never BLINDLY trusted: a response that somehow contains a
        # non-ACCPAY row is treated as a malformed/unexpected response
        # shape (caught by list_purchase_invoices's own
        # ValueError-inclusive except clause), never silently accepted
        # as if it were a legitimate purchase invoice.
        raise ValueError(
            f"expected every /Invoices row to be Type=='ACCPAY' (the where-filter's own contract), "
            f"got {invoice_type!r} for InvoiceID={item.get('InvoiceID')!r}"
        )
    contact = item.get("Contact") or {}
    return RawXeroPurchaseInvoice(
        invoice_id=item["InvoiceID"],
        contact_id=contact.get("ContactID"),
        invoice_type=invoice_type,
        invoice_date=_parse_xero_wire_datetime(item.get("Date")),
        status=item.get("Status"),
    )


class XeroAccountingClient:
    """The real adapter for `GET /api.xro/2.0/Accounts` (PID §102.1)."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def list_accounts(self, *, tenant_id: str, access_token: str) -> XeroAccountsResult:
        if not tenant_id:
            raise ValidationError(
                "list_accounts requires a real, server-resolved tenant_id — never call this "
                "with an empty/browser-supplied value (architect spec §3)"
            )

        request = urllib.request.Request(
            ACCOUNTS_URL,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-Tenant-Id": tenant_id,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return XeroAccountsResult(
                    status=XeroOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                )
            if exc.code == 429:
                return XeroAccountsResult(
                    status=XeroOutcomeStatus.RATE_LIMITED,
                    retry_after_seconds=_retry_after_seconds(exc),
                    error_detail=f"HTTP 429: {_read_body(exc)}",
                )
            return XeroAccountsResult(
                status=XeroOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
            return XeroAccountsResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            accounts = tuple(_parse_raw_account(item) for item in payload["Accounts"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return XeroAccountsResult(
                status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Xero /Accounts response shape: {exc}",
            )
        return XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=accounts)

    def list_contacts(self, *, tenant_id: str, access_token: str) -> XeroContactsResult:
        """`GET /Contacts` (CD-6 supplier-domain-correlation addition —
        see module docstring). Same GET-only/never-raises-for-transport-
        failure/`Xero-Tenant-Id` discipline as :meth:`list_accounts`
        exactly."""
        if not tenant_id:
            raise ValidationError(
                "list_contacts requires a real, server-resolved tenant_id — never call this "
                "with an empty/browser-supplied value (architect spec §3)"
            )

        request = urllib.request.Request(
            CONTACTS_URL,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-Tenant-Id": tenant_id,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return XeroContactsResult(
                    status=XeroOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                )
            if exc.code == 429:
                return XeroContactsResult(
                    status=XeroOutcomeStatus.RATE_LIMITED,
                    retry_after_seconds=_retry_after_seconds(exc),
                    error_detail=f"HTTP 429: {_read_body(exc)}",
                )
            return XeroContactsResult(
                status=XeroOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
            return XeroContactsResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            contacts = tuple(_parse_raw_contact(item) for item in payload["Contacts"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return XeroContactsResult(
                status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Xero /Contacts response shape: {exc}",
            )
        return XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=contacts)

    def list_purchase_invoices(self, *, tenant_id: str, access_token: str) -> XeroInvoicesResult:
        """`GET /Invoices?where=Type%3D%3D%22ACCPAY%22` — bill-side
        (purchase) invoices ONLY, server-side filtered (see module
        docstring's endpoint list, and `_ACCPAY_ONLY_QUERY`'s own
        comment for why this is enforced at the query itself, not left
        to a later caller). Same GET-only/never-raises-for-transport-
        failure/`Xero-Tenant-Id` discipline as :meth:`list_accounts`
        exactly."""
        if not tenant_id:
            raise ValidationError(
                "list_purchase_invoices requires a real, server-resolved tenant_id — never call this "
                "with an empty/browser-supplied value (architect spec §3)"
            )

        request = urllib.request.Request(
            f"{INVOICES_URL}?{_ACCPAY_ONLY_QUERY}",
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-Tenant-Id": tenant_id,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return XeroInvoicesResult(
                    status=XeroOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                )
            if exc.code == 429:
                return XeroInvoicesResult(
                    status=XeroOutcomeStatus.RATE_LIMITED,
                    retry_after_seconds=_retry_after_seconds(exc),
                    error_detail=f"HTTP 429: {_read_body(exc)}",
                )
            return XeroInvoicesResult(
                status=XeroOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
            )
        except Exception as exc:  # noqa: BLE001
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
            return XeroInvoicesResult(status=status, error_detail=str(exc)[:500])

        try:
            payload = json.loads(raw.decode("utf-8"))
            invoices = tuple(_parse_raw_purchase_invoice(item) for item in payload["Invoices"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return XeroInvoicesResult(
                status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"could not parse Xero /Invoices response shape: {exc}",
            )
        return XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=invoices)
