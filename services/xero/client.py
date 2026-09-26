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
* Bank Transactions (``SPEND``-type only): ``GET
  https://api.xero.com/api.xro/2.0/BankTransactions?where=Type%3D%3D%22SPEND%22%26%26Date%3E%3DDateTime(...)&page=N``
  — CD-6 second-correlation-source addition (see
  :meth:`XeroAccountingClient.list_bank_transactions`'s own docstring):
  a bounded, deduplicated, real-paginated read of SPEND-type
  BankTransactions, correlated by ``ContactID`` to the same sender
  domains as the existing invoice-based path, for the real, live
  finding that Infosecurs records purchase expenditure as direct
  ``SPEND`` BankTransactions rather than ACCPAY bills (zero ACCPAY
  invoices were returned by the first real correlation run against the
  live Infosecurs Xero organisation).
* Scopes: ``openid profile email offline_access accounting.settings.read``
  (PID §102.1 — confirmed current/non-deprecated for a read-only Chart-
  of-Accounts sync; see that section for the devblog citation).

Scope extension (architect-authorized, bounded Xero-assisted supplier-
domain correlation ahead of any bulk mailbox-domain approval)
------------------------------------------------------------------------
:data:`OAUTH_SCOPES` gained three further READ-ONLY scopes,
``accounting.contacts.read``, ``accounting.invoices.read``, and
``accounting.banktransactions.read`` — the minimum necessary additional
read scopes for :mod:`services.xero.supplier_correlation` to read
Contacts/Invoices/BankTransactions and correlate them against still-
OPEN ``MAILBOX_DOMAIN_REVIEW`` Needs You items. No write-capable scope
(no ``.write`` token of any kind) was added, and no broader accounting-
transaction write authority was requested — this component remains
READ/REFERENCE-ONLY (see this module's own docstring and
``services/xero/component.yaml``'s ``prohibited: xero_write_endpoints``).
Changing this constant only changes what scope string gets REQUESTED
the next time an operator goes through the existing connect/reconnect
OAuth flow (real, operator-driven, browser-based, explicitly deferred)
— it does not retroactively grant anything against the currently-
connected Infosecurs tenant (which today only actually holds
``accounting.settings.read``), and it never itself triggers or
simulates any OAuth call. Building ``list_bank_transactions`` and
adding its scope here is explicitly CODE-ONLY (WO constraint) — no live
OAuth scope expansion is requested, triggered, or simulated by this
change; the real re-consent against the live Infosecurs tenant is a
separate, later, human-driven action.

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
from typing import Any, Callable, Mapping, Optional, Protocol

from core.errors import ValidationError
from services.xero.account import RawXeroAccount

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
ACCOUNTS_URL = "https://api.xero.com/api.xro/2.0/Accounts"
CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"
INVOICES_URL = "https://api.xero.com/api.xro/2.0/Invoices"
BANK_TRANSACTIONS_URL = "https://api.xero.com/api.xro/2.0/BankTransactions"

#: Xero's own server-side `where` filter, applied ONLY to
#: `list_purchase_invoices` — restricts the returned set to bill-side
#: (`ACCPAY`) invoices, never sales-side (`ACCREC`) customer invoices
#: (architect's own explicit instruction: "Do not treat customers... as
#: suppliers merely because they exist in Xero" — enforced here, at the
#: query itself, not left for a later caller to remember).
_ACCPAY_ONLY_QUERY = urllib.parse.urlencode({"where": 'Type=="ACCPAY"'})

#: `list_bank_transactions`'s own bounded pagination/retry budget (see
#: that method's own docstring's "Real pagination required" section).
#: Hitting either bound is an honest `MALFORMED_RESPONSE`-class failure
#: — never a silently truncated "complete" result. 50 pages at Xero's
#: own real, fixed 100-row page size for this endpoint (not caller-
#: configurable) is a genuinely generous ceiling for one entity's
#: real ~10-month historical window, while still preventing any
#: conceivable infinite-loop/retry-storm.
_MAX_BANK_TRANSACTION_PAGES = 50
_BANK_TRANSACTIONS_PAGE_SIZE = 100
#: Same bounded-backoff budget/discipline as `services.xero.sync
#: ._MAX_RATE_LIMIT_BACKOFF_SECONDS` / `services.mailbox.sweep
#: ._MAX_RATE_LIMIT_BACKOFF_SECONDS` — this module's own two
#: established precedents for "bound the backoff, retry exactly once,
#: then an honest failure" — deliberately the SAME 30.0s ceiling, not a
#: new value invented for this one method.
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 30.0

#: PID §102.1 — the exact, confirmed-current scope set for this slice's
#: read-only Chart-of-Accounts sync, EXTENDED (architect-authorized —
#: see module docstring's "Scope extension" section) with three further
#: READ-ONLY scopes for the bounded Xero-assisted supplier-domain
#: correlation capability: `accounting.contacts.read`,
#: `accounting.invoices.read`, and `accounting.banktransactions.read`.
#: No write-capable scope. `offline_access` is what makes a
#: `refresh_token` exist at all (never re-prompting an operator for
#: ordinary token refresh, architect spec §3).
OAUTH_SCOPES = (
    "openid profile email offline_access accounting.settings.read "
    "accounting.contacts.read accounting.invoices.read accounting.banktransactions.read"
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


@dataclass(frozen=True)
class RawXeroBankTransaction:
    """The plain, provider-shaped fields one `GET /BankTransactions` row
    (server-side filtered to `Type=="SPEND"&&Date>=DateTime(...)`, then
    defensively re-validated client-side — see
    `_parse_raw_bank_transaction`'s own docstring for why this differs
    from `RawXeroPurchaseInvoice`'s sibling RAISE-on-unexpected-type
    choice) yields — transient, never persisted (see module docstring's
    "No new persisted canonical domain model" section).

    `transaction_type` is Xero's own `Type` (captured verbatim — always
    `"SPEND"` by the time a row reaches this dataclass, since
    `_parse_raw_bank_transaction` already discarded anything else; this
    client layer never decides supplier-relevance weighting, exactly
    like `RawXeroPurchaseInvoice.status`'s own existing doctrine).
    `status` is Xero's own `Status` (`AUTHORISED`/`DELETED`/...,
    captured verbatim — a `DELETED` transaction must NEVER count as
    positive supplier evidence, per the architect's own explicit
    instruction, but that weighting judgment belongs to
    `services.xero.supplier_correlation` alone, not here). `total` is
    Xero's own JSON-native numeric `Total` field, captured as a plain
    `float` — this codebase has NO pre-existing amount-handling
    convention anywhere (a real check: zero `Decimal` usage and zero
    other amount-shaped fields exist in this repository today), so this
    is genuinely the first; `float` was chosen over `Decimal` because
    (a) there is nothing to match, and (b) every other Raw* field in
    this module is already captured "as Xero's JSON returns it" with no
    extra precision-conversion layer, and a `Decimal` would need one
    more conversion step at every JSON boundary this value crosses
    (this dataclass's own construction, and later
    `NeedsYouItem.metadata`, which is a plain JSON-serialisable dict —
    `Decimal` is not JSON-serialisable without a custom encoder this
    codebase does not have). `is_reconciled` is Xero's own
    `IsReconciled` boolean — the closest available "reconciliation
    state" signal; Xero's public Accounting API exposes no richer
    reconciliation-state breakdown than this one boolean at this
    endpoint, so this is the honest ceiling of what is available here,
    never invented beyond it."""

    bank_transaction_id: str
    transaction_type: str
    status: Optional[str]
    date: Optional[datetime]
    contact_id: Optional[str]
    reference: Optional[str]
    total: Optional[float]
    currency_code: Optional[str]
    is_reconciled: bool


@dataclass(frozen=True)
class XeroBankTransactionsResult:
    status: XeroOutcomeStatus
    bank_transactions: tuple[RawXeroBankTransaction, ...] = ()
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

    def list_bank_transactions(
        self, *, tenant_id: str, access_token: str, since: datetime
    ) -> XeroBankTransactionsResult: ...


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


def _bank_transactions_spend_where_clause(since: datetime) -> str:
    """The exact, documented Xero `where`-clause date-literal syntax —
    `DateTime(yyyy,MM,dd)` — combined with an exact `Type=="SPEND"`
    match (WO's own real, confirmed-documented Xero API quirk, not
    invented). Server-side is trusted to narrow the result, but NEVER
    blindly trusted alone — `_parse_raw_bank_transaction` re-validates
    both conditions client-side on every row (see that function's own
    docstring for why this differs from `_parse_raw_purchase_invoice`'s
    own choice to RAISE on an unexpected type)."""
    return urllib.parse.urlencode(
        {"where": f'Type=="SPEND"&&Date>=DateTime({since.year},{since.month:02d},{since.day:02d})'}
    )


def _parse_raw_bank_transaction(item: Mapping[str, Any], *, since: datetime) -> Optional[RawXeroBankTransaction]:
    """Parse one `/BankTransactions` row, defensively re-validating the
    server-side `where=Type=="SPEND"&&Date>=DateTime(...)` filter's own
    contract — returns `None` (silently DISCARDED, never raised) for a
    row that fails either check, or whose `Date` could not be parsed at
    all (never counted as satisfying `since` when the date itself could
    not even be established — the safe direction for a FILTER, never
    the safe direction for stored data; a row that DOES pass still has
    its `date` field captured verbatim, naive-or-aware exactly as
    parsed, unlike this transient comparison).

    This is a DELIBERATELY different judgment call from
    `_parse_raw_purchase_invoice`'s own choice to RAISE on an
    unexpected `Type` (see that function's docstring). BankTransactions
    realistically returns much higher volume with many more genuine
    Xero `Type` variants (`SPEND`, `RECEIVE`, `SPEND-OVERPAYMENT`,
    `SPEND-PREPAYMENT`, `SPEND-TRANSFER`, `RECEIVE-OVERPAYMENT`,
    `RECEIVE-PREPAYMENT`, `RECEIVE-TRANSFER` are all real Xero values),
    and is fetched over MANY real pages for a realistic ~10-month
    window (unlike the single-page, low-volume
    `/Invoices?where=Type=="ACCPAY"` call) — an all-or-nothing raise
    over one stray row deep into a multi-page pull would throw away an
    otherwise-good result over what is far more likely to be an
    ordinary `where`-clause/pagination edge case than genuine
    corruption. A `where` clause that only asks for exact
    `Type=="SPEND"` may still legitimately need this defensive
    narrowing rather than an all-or-nothing raise, given the
    realistically higher chance of a partial/edge-case response over
    many pages — so this method silently narrows instead, and documents
    the choice here rather than leaving it an unexplained asymmetry
    with its sibling."""
    transaction_type = item.get("Type")
    if transaction_type != "SPEND":
        return None

    raw_date = _parse_xero_wire_datetime(item.get("Date"))
    if raw_date is None:
        return None
    # `since` is always canonical UTC-aware (every real caller derives
    # it via `services.mailbox.bootstrap_policy
    # .compute_entity_historical_bootstrap`); `raw_date` may legitimately
    # be naive (the SAME pre-existing Xero wire-format quirk
    # `_parse_xero_wire_datetime` already documents for invoice dates).
    # Coerced to UTC ONLY for this transient comparison — never for the
    # value actually stored on `RawXeroBankTransaction.date` below,
    # which keeps `raw_date` exactly as parsed.
    comparable_date = raw_date if raw_date.tzinfo is not None else raw_date.replace(tzinfo=timezone.utc)
    if comparable_date < since:
        return None

    contact = item.get("Contact") or {}
    return RawXeroBankTransaction(
        bank_transaction_id=item["BankTransactionID"],
        transaction_type=transaction_type,
        status=item.get("Status"),
        date=raw_date,
        contact_id=contact.get("ContactID"),
        reference=item.get("Reference"),
        total=item.get("Total"),
        currency_code=item.get("CurrencyCode"),
        is_reconciled=bool(item.get("IsReconciled", False)),
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

    def _bank_transactions_page_request(
        self, *, tenant_id: str, access_token: str, where_query: str, page: int
    ) -> urllib.request.Request:
        """The ONE place this method's GET request is built (called
        once for the initial fetch of a page, once again for its single
        bounded rate-limit retry — see :meth:`list_bank_transactions`)
        — kept as a single source-level GET-request call site
        deliberately, mirroring this class's existing GET-only
        transport discipline."""
        return urllib.request.Request(
            f"{BANK_TRANSACTIONS_URL}?{where_query}&page={page}",
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Xero-Tenant-Id": tenant_id,
                "Accept": "application/json",
            },
        )

    def list_bank_transactions(
        self,
        *,
        tenant_id: str,
        access_token: str,
        since: datetime,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> XeroBankTransactionsResult:
        """`GET /BankTransactions?where=Type%3D%3D%22SPEND%22%26%26Date...&page=N`
        — CD-6 second-correlation-source addition (see module
        docstring's "Bank Transactions" endpoint entry and
        `services/xero/supplier_correlation.py`'s own docstring for
        why: the real, live Infosecurs Xero organisation returned zero
        ACCPAY purchase invoices, so purchase expenditure there is
        hypothesised to be recorded as direct `SPEND` BankTransactions
        instead).

        Unlike every other method on this class, this one REALLY
        PAGINATES (a genuinely new capability — `list_accounts`/
        `list_contacts`/`list_purchase_invoices` never need to, see
        those methods' own single-page-is-enough shape): Xero's own
        `/BankTransactions` endpoint returns at most
        `_BANK_TRANSACTIONS_PAGE_SIZE` (100) rows per `page`, and a
        realistic ~10-month window for a real operating company can
        plausibly exceed that. Pages are fetched `page=1,2,3,...` until
        Xero returns an empty `BankTransactions` array (Xero's own
        documented stop condition), bounded by
        `_MAX_BANK_TRANSACTION_PAGES` — hitting that bound returns an
        honest `MALFORMED_RESPONSE` (never a silently truncated
        "complete" result presented as if every row were seen).

        Rate-limiting is handled per-page with the SAME bounded-
        backoff-then-retry-exactly-once discipline this module already
        establishes twice (`services.xero.sync`/`services.mailbox.sweep`
        own `_MAX_RATE_LIMIT_BACKOFF_SECONDS`): a `429` on any one page
        sleeps once (bounded) and retries that SAME page exactly once;
        a `429` again aborts the WHOLE call as `RATE_LIMITED` — a
        partial page haul is never silently presented as a complete
        result.

        Deduplication by `BankTransactionID` happens HERE, across every
        page (and across a page's own bounded retry) — this client
        method is responsible for it, not merely a caller three layers
        up (see `RawXeroBankTransaction`'s own module-docstring
        "Deduplication" note in the WO for the defense-in-depth
        reasoning; `services.xero.supplier_correlation` also dedupes
        independently as a second, defensive layer — see that module's
        own docstring).

        Every returned row already passed `_parse_raw_bank_transaction`'s
        own defensive re-validation (`Type=="SPEND"` and `Date >= since`,
        both re-checked client-side, never blindly trusted from the
        server-side `where` filter alone — see that function's own
        docstring for why this SILENTLY NARROWS rather than raising,
        unlike `list_purchase_invoices`'s sibling choice).
        """
        if not tenant_id:
            raise ValidationError(
                "list_bank_transactions requires a real, server-resolved tenant_id — never call this "
                "with an empty/browser-supplied value (architect spec §3)"
            )

        where_query = _bank_transactions_spend_where_clause(since)
        collected: list[RawXeroBankTransaction] = []
        seen_ids: set[str] = set()
        page = 1

        while True:
            if page > _MAX_BANK_TRANSACTION_PAGES:
                return XeroBankTransactionsResult(
                    status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                    error_detail=(
                        f"/BankTransactions exceeded the bounded page limit "
                        f"({_MAX_BANK_TRANSACTION_PAGES} pages / "
                        f"{_MAX_BANK_TRANSACTION_PAGES * _BANK_TRANSACTIONS_PAGE_SIZE} rows) without Xero "
                        "ever returning an empty page — an honest failure, never a silently truncated "
                        "'complete' result"
                    ),
                )

            request = self._bank_transactions_page_request(
                tenant_id=tenant_id, access_token=access_token, where_query=where_query, page=page
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    return XeroBankTransactionsResult(
                        status=XeroOutcomeStatus.AUTH_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                    )
                if exc.code != 429:
                    return XeroBankTransactionsResult(
                        status=XeroOutcomeStatus.PROVIDER_ERROR, error_detail=f"HTTP {exc.code}: {_read_body(exc)}"
                    )
                # Bounded backoff, retry this SAME page exactly once
                # (mirrors `services.xero.sync.run_sync`'s own
                # identical discipline).
                backoff = min(_retry_after_seconds(exc) or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
                sleep_fn(backoff)
                retry_request = self._bank_transactions_page_request(
                    tenant_id=tenant_id, access_token=access_token, where_query=where_query, page=page
                )
                try:
                    with urllib.request.urlopen(retry_request, timeout=self._timeout_seconds) as response:
                        raw = response.read()
                except urllib.error.HTTPError as retry_exc:
                    if retry_exc.code in (401, 403):
                        return XeroBankTransactionsResult(
                            status=XeroOutcomeStatus.AUTH_ERROR,
                            error_detail=f"HTTP {retry_exc.code}: {_read_body(retry_exc)}",
                        )
                    if retry_exc.code == 429:
                        return XeroBankTransactionsResult(
                            status=XeroOutcomeStatus.RATE_LIMITED,
                            retry_after_seconds=_retry_after_seconds(retry_exc),
                            error_detail=(
                                f"rate limited on page {page} after one bounded retry: "
                                f"HTTP 429: {_read_body(retry_exc)}"
                            ),
                        )
                    return XeroBankTransactionsResult(
                        status=XeroOutcomeStatus.PROVIDER_ERROR,
                        error_detail=f"HTTP {retry_exc.code}: {_read_body(retry_exc)}",
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    is_timeout = isinstance(retry_exc, TimeoutError) or "timed out" in str(retry_exc).lower()
                    status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
                    return XeroBankTransactionsResult(status=status, error_detail=str(retry_exc)[:500])
            except Exception as exc:  # noqa: BLE001
                is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
                status = XeroOutcomeStatus.TIMEOUT if is_timeout else XeroOutcomeStatus.TRANSPORT_ERROR
                return XeroBankTransactionsResult(status=status, error_detail=str(exc)[:500])

            try:
                payload = json.loads(raw.decode("utf-8"))
                page_rows = payload["BankTransactions"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                return XeroBankTransactionsResult(
                    status=XeroOutcomeStatus.MALFORMED_RESPONSE,
                    error_detail=f"could not parse Xero /BankTransactions response shape: {exc}",
                )

            if not page_rows:
                break  # Xero's own documented stop condition: an empty page means "no more results".

            for item in page_rows:
                parsed = _parse_raw_bank_transaction(item, since=since)
                if parsed is None:
                    continue
                if parsed.bank_transaction_id in seen_ids:
                    continue  # defense-in-depth dedup (see method docstring)
                seen_ids.add(parsed.bank_transaction_id)
                collected.append(parsed)

            page += 1

        return XeroBankTransactionsResult(status=XeroOutcomeStatus.OK, bank_transactions=tuple(collected))
