"""Tests for the REAL, network-speaking ``services.xero.client
.XeroAccountingClient`` — the CD-6 supplier-domain-correlation
additions, ``list_contacts``/``list_purchase_invoices``/
``list_bank_transactions`` (bounded Xero-assisted correlation ahead of
any bulk mailbox-domain approval; see ``services/xero/supplier_correlation
.py``'s own module docstring for the feature this client work
supports).

No real network I/O anywhere in this module (PID §61) — ``urllib
.request.urlopen`` is monkeypatched to a deterministic fake per test,
proving the real adapter's own request construction (GET-only, exact
URL/headers), response parsing, and full error-status mapping WITHOUT
ever touching a socket. This is a new test file because no existing
test in this repository drives ``XeroAccountingClient``'s real HTTP
path at all — every other Xero/Microsoft-Graph test in this codebase
exercises the `Fake*` substitutes exclusively (see
`services/xero/fake_client.py`'s own module docstring); this module
establishes the missing "prove the real adapter's own HTTP-shape
discipline" coverage the WO explicitly asks for (GET-only, correct
`where` filter, correct parsing, full error matrix), the same shape
`tests/integration/test_litellm_client.py` already proves for
`ai.providers.litellm.client.LiteLLMClient` (loopback-port style,
extended here to full JSON-body mocking since the error matrix this WO
asks for needs response bodies/headers a bare unreachable socket can't
produce).
"""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from email.message import Message

import pytest

from core.errors import ValidationError
from services.xero.client import (
    BANK_TRANSACTIONS_URL,
    CONTACTS_URL,
    INVOICES_URL,
    OAUTH_SCOPES,
    XeroAccountingClient,
    XeroOutcomeStatus,
)


class _FakeHTTPResponse:
    """Minimal stand-in for `http.client.HTTPResponse` used as a
    context manager — only `.read()` is ever called on it by
    `XeroAccountingClient`."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _ok(payload: dict) -> _FakeHTTPResponse:
    return _FakeHTTPResponse(json.dumps(payload).encode("utf-8"))


def _http_error(code: int, *, body: bytes = b"", retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url="https://api.xero.com/x", code=code, msg="error", hdrs=headers, fp=io.BytesIO(body))


def _patch_urlopen(monkeypatch, *, returns=None, raises=None):
    """Patch the real `urllib.request.urlopen` (module-global — see
    `services.xero.client`'s own `import urllib.request`) to either
    return `returns` or raise `raises`, and CAPTURE the `Request` object
    it was called with so a test can assert on method/URL/headers.
    Returns the mutable `captured` dict; `captured["request"]` is set
    the moment the fake is invoked."""
    captured: dict = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - test double
        captured["request"] = request
        captured["timeout"] = timeout
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def _patch_urlopen_sequence(monkeypatch, *, responses):
    """Like `_patch_urlopen`, but for `list_bank_transactions`'s own
    real multi-page/bounded-retry shape: `responses` is an ordered list
    where each entry is EITHER a `_FakeHTTPResponse` (returned) or an
    `Exception` instance (raised) — one entry consumed per real
    `urlopen()` call, in call order. Every `Request` object seen is
    captured in order (`captured["requests"]`), so a test can assert on
    each page's own `page=N`/`where=` query string. Calling `urlopen()`
    more times than scripted raises `AssertionError` — an un-scripted
    call is a test bug, never a silently fabricated response (same
    discipline every `Fake*` substitute in this codebase already
    documents)."""
    captured: dict = {"requests": []}
    queue = list(responses)

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - test double
        captured["requests"].append(request)
        if not queue:
            raise AssertionError("urlopen() called more times than this test scripted")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


CONTACTS_PAYLOAD = {
    "Contacts": [
        {
            "ContactID": "c-1",
            "Name": "Acme Supplies Ltd",
            "EmailAddress": "billing@acme-supplies.example",
            "IsCustomer": False,
            "IsSupplier": True,
            "ContactStatus": "ACTIVE",
        },
        {
            "ContactID": "c-2",
            "Name": "No Email Ltd",
            "IsCustomer": False,
            "IsSupplier": False,
            "ContactStatus": "ACTIVE",
        },
    ]
}

INVOICES_PAYLOAD = {
    "Invoices": [
        {
            "InvoiceID": "i-1",
            "Type": "ACCPAY",
            "Contact": {"ContactID": "c-1"},
            "Date": "2026-01-15T00:00:00",
            "Status": "AUTHORISED",
        },
        {
            "InvoiceID": "i-2",
            "Type": "ACCPAY",
            "Contact": {"ContactID": "c-1"},
            "Date": "2026-02-20T00:00:00",
            "Status": "PAID",
        },
    ]
}


# ---------------------------------------------------------------------
# list_contacts
# ---------------------------------------------------------------------


def test_list_contacts_requires_a_real_tenant_id():
    client = XeroAccountingClient()
    with pytest.raises(ValidationError):
        client.list_contacts(tenant_id="", access_token="token")


def test_list_contacts_is_get_only_and_hits_the_contacts_url(monkeypatch):
    captured = _patch_urlopen(monkeypatch, returns=_ok(CONTACTS_PAYLOAD))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")

    assert result.status == XeroOutcomeStatus.OK
    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.full_url == CONTACTS_URL
    assert request.headers["Xero-tenant-id"] == "tenant-1"
    assert request.headers["Authorization"] == "Bearer access-1"


def test_list_contacts_parses_a_realistic_response(monkeypatch):
    _patch_urlopen(monkeypatch, returns=_ok(CONTACTS_PAYLOAD))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")

    assert result.status == XeroOutcomeStatus.OK
    assert len(result.contacts) == 2
    first = result.contacts[0]
    assert first.contact_id == "c-1"
    assert first.name == "Acme Supplies Ltd"
    assert first.email_address == "billing@acme-supplies.example"
    assert first.is_customer is False
    assert first.is_supplier is True
    assert first.contact_status == "ACTIVE"
    # A blank/absent EmailAddress is handled gracefully — never a crash,
    # never fabricated — as `None`.
    second = result.contacts[1]
    assert second.email_address is None


@pytest.mark.parametrize("code", [401, 403])
def test_list_contacts_auth_error_statuses(monkeypatch, code):
    _patch_urlopen(monkeypatch, raises=_http_error(code, body=b"nope"))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.AUTH_ERROR


def test_list_contacts_rate_limited_carries_retry_after(monkeypatch):
    _patch_urlopen(monkeypatch, raises=_http_error(429, retry_after="12"))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.RATE_LIMITED
    assert result.retry_after_seconds == 12.0


def test_list_contacts_other_http_error_is_provider_error(monkeypatch):
    _patch_urlopen(monkeypatch, raises=_http_error(500, body=b"boom"))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.PROVIDER_ERROR


def test_list_contacts_malformed_json_is_malformed_response(monkeypatch):
    _patch_urlopen(monkeypatch, returns=_FakeHTTPResponse(b"not json at all"))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.MALFORMED_RESPONSE


def test_list_contacts_missing_expected_key_is_malformed_response(monkeypatch):
    _patch_urlopen(monkeypatch, returns=_ok({"NotContacts": []}))
    client = XeroAccountingClient()
    result = client.list_contacts(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.MALFORMED_RESPONSE


# ---------------------------------------------------------------------
# list_purchase_invoices
# ---------------------------------------------------------------------


def test_list_purchase_invoices_requires_a_real_tenant_id():
    client = XeroAccountingClient()
    with pytest.raises(ValidationError):
        client.list_purchase_invoices(tenant_id="", access_token="token")


def test_list_purchase_invoices_is_get_only_with_the_accpay_where_filter(monkeypatch):
    captured = _patch_urlopen(monkeypatch, returns=_ok(INVOICES_PAYLOAD))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")

    assert result.status == XeroOutcomeStatus.OK
    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.full_url == f'{INVOICES_URL}?where=Type%3D%3D%22ACCPAY%22'
    # Never the sales-side filter, and never unfiltered.
    assert "ACCREC" not in request.full_url
    assert request.headers["Xero-tenant-id"] == "tenant-1"


def test_list_purchase_invoices_parses_a_realistic_response(monkeypatch):
    _patch_urlopen(monkeypatch, returns=_ok(INVOICES_PAYLOAD))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")

    assert result.status == XeroOutcomeStatus.OK
    assert len(result.invoices) == 2
    first = result.invoices[0]
    assert first.invoice_id == "i-1"
    assert first.contact_id == "c-1"
    assert first.invoice_type == "ACCPAY"
    assert first.status == "AUTHORISED"
    # `_parse_xero_wire_datetime` mirrors the pre-existing account-date
    # parsing exactly: a bare ISO-8601 string with no offset parses to
    # a naive `datetime` (never a fabricated tzinfo) — this fixture's
    # `"2026-01-15T00:00:00"` has no offset, matching Xero's real
    # `Date` field shape for the modern JSON Accounting API.
    assert first.invoice_date == datetime(2026, 1, 15, 0, 0, 0)


def test_list_purchase_invoices_defends_against_a_non_accpay_row(monkeypatch):
    """The WO's own explicit instruction: never silently trust the
    server-side `where` filter alone worked — a stray ACCREC row (or
    any other Type) makes the whole response MALFORMED_RESPONSE, never
    silently accepted as purchase history."""
    payload = {
        "Invoices": [
            {"InvoiceID": "i-bad", "Type": "ACCREC", "Contact": {"ContactID": "c-1"}, "Date": None, "Status": "AUTHORISED"}
        ]
    }
    _patch_urlopen(monkeypatch, returns=_ok(payload))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.MALFORMED_RESPONSE


@pytest.mark.parametrize("code", [401, 403])
def test_list_purchase_invoices_auth_error_statuses(monkeypatch, code):
    _patch_urlopen(monkeypatch, raises=_http_error(code))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.AUTH_ERROR


def test_list_purchase_invoices_rate_limited_carries_retry_after(monkeypatch):
    _patch_urlopen(monkeypatch, raises=_http_error(429, retry_after="5"))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.RATE_LIMITED
    assert result.retry_after_seconds == 5.0


def test_list_purchase_invoices_other_http_error_is_provider_error(monkeypatch):
    _patch_urlopen(monkeypatch, raises=_http_error(503))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.PROVIDER_ERROR


def test_list_purchase_invoices_malformed_json_is_malformed_response(monkeypatch):
    _patch_urlopen(monkeypatch, returns=_FakeHTTPResponse(b"{not json"))
    client = XeroAccountingClient()
    result = client.list_purchase_invoices(tenant_id="tenant-1", access_token="access-1")
    assert result.status == XeroOutcomeStatus.MALFORMED_RESPONSE


# ---------------------------------------------------------------------
# list_bank_transactions — CD-6 second-correlation-source WO. The ONE
# client method that really paginates/really retries — see
# services/xero/client.py's own module docstring.
# ---------------------------------------------------------------------

_SINCE = datetime(2024, 11, 1, tzinfo=timezone.utc)


def _bank_transaction_row(
    *,
    bank_transaction_id="bt-1",
    transaction_type="SPEND",
    status="AUTHORISED",
    contact_id="c-1",
    date="2026-01-15T00:00:00",
    total=123.45,
    currency_code="GBP",
    is_reconciled=True,
):
    return {
        "BankTransactionID": bank_transaction_id,
        "Type": transaction_type,
        "Status": status,
        "Contact": {"ContactID": contact_id} if contact_id is not None else {},
        "Date": date,
        "Reference": "INV-REF-1",
        "Total": total,
        "CurrencyCode": currency_code,
        "IsReconciled": is_reconciled,
    }


def test_list_bank_transactions_requires_a_real_tenant_id():
    client = XeroAccountingClient()
    with pytest.raises(ValidationError):
        client.list_bank_transactions(tenant_id="", access_token="token", since=_SINCE)


def test_list_bank_transactions_is_get_only_with_exact_spend_where_and_page(monkeypatch):
    captured = _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok({"BankTransactions": [_bank_transaction_row()]}),
            _ok({"BankTransactions": []}),  # page 2 — empty, stops pagination
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.OK
    assert len(captured["requests"]) == 2
    first_request = captured["requests"][0]
    assert first_request.get_method() == "GET"
    assert first_request.full_url.startswith(BANK_TRANSACTIONS_URL)
    assert "where=Type%3D%3D%22SPEND%22%26%26Date%3E%3DDateTime%282024%2C11%2C01%29" in first_request.full_url
    assert "page=1" in first_request.full_url
    assert first_request.headers["Xero-tenant-id"] == "tenant-1"
    assert first_request.headers["Authorization"] == "Bearer access-1"
    assert "page=2" in captured["requests"][1].full_url


def test_list_bank_transactions_parses_a_realistic_response(monkeypatch):
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok({"BankTransactions": [_bank_transaction_row()]}),
            _ok({"BankTransactions": []}),
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.OK
    assert len(result.bank_transactions) == 1
    row = result.bank_transactions[0]
    assert row.bank_transaction_id == "bt-1"
    assert row.transaction_type == "SPEND"
    assert row.status == "AUTHORISED"
    assert row.contact_id == "c-1"
    assert row.reference == "INV-REF-1"
    assert row.total == 123.45
    assert row.currency_code == "GBP"
    assert row.is_reconciled is True
    assert row.date == datetime(2026, 1, 15, 0, 0, 0)


def test_list_bank_transactions_paginates_across_multiple_real_pages(monkeypatch):
    captured = _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-1")]}),
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-2")]}),
            _ok({"BankTransactions": []}),  # Xero's own documented stop condition
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.OK
    assert {row.bank_transaction_id for row in result.bank_transactions} == {"bt-1", "bt-2"}
    assert len(captured["requests"]) == 3
    assert [r for req in captured["requests"] for r in [req.full_url] if "page=1" in r]
    assert "page=3" in captured["requests"][2].full_url


def test_list_bank_transactions_deduplicates_by_bank_transaction_id_across_pages(monkeypatch):
    """A `BankTransactionID` re-seen on a later page (e.g. a legitimate
    re-fetch after a bounded retry) is never counted twice — the client
    method itself is responsible for this (see module docstring's
    "Deduplication" section)."""
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-dup")]}),
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-dup")]}),
            _ok({"BankTransactions": []}),
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.OK
    assert len(result.bank_transactions) == 1


def test_list_bank_transactions_defensively_discards_a_non_spend_row(monkeypatch):
    """The WO's own explicit instruction: never blindly trust the
    server-side `where=Type=="SPEND"` filter alone — a stray non-SPEND
    row is silently discarded (NOT raised — the deliberately different
    judgment call from `list_purchase_invoices`'s own sibling choice,
    see `_parse_raw_bank_transaction`'s own docstring)."""
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok(
                {
                    "BankTransactions": [
                        _bank_transaction_row(bank_transaction_id="bt-receive", transaction_type="RECEIVE"),
                        _bank_transaction_row(bank_transaction_id="bt-spend", transaction_type="SPEND"),
                    ]
                }
            ),
            _ok({"BankTransactions": []}),
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.OK
    assert [row.bank_transaction_id for row in result.bank_transactions] == ["bt-spend"]


def test_list_bank_transactions_defensively_discards_a_row_before_since(monkeypatch):
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok(
                {
                    "BankTransactions": [
                        _bank_transaction_row(bank_transaction_id="bt-too-old", date="2024-01-01T00:00:00"),
                        _bank_transaction_row(bank_transaction_id="bt-in-window", date="2025-01-01T00:00:00"),
                    ]
                }
            ),
            _ok({"BankTransactions": []}),
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.OK
    assert [row.bank_transaction_id for row in result.bank_transactions] == ["bt-in-window"]


def test_list_bank_transactions_rate_limited_retries_the_same_page_exactly_once_then_succeeds(monkeypatch):
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _http_error(429, retry_after="7"),
            _ok({"BankTransactions": [_bank_transaction_row()]}),
            _ok({"BankTransactions": []}),
        ],
    )
    slept = []
    client = XeroAccountingClient()
    result = client.list_bank_transactions(
        tenant_id="tenant-1", access_token="access-1", since=_SINCE, sleep_fn=slept.append
    )

    assert result.status == XeroOutcomeStatus.OK
    assert len(result.bank_transactions) == 1
    assert slept == [7.0]  # bounded backoff honoured Retry-After, exactly one sleep


def test_list_bank_transactions_rate_limited_after_one_retry_is_an_honest_failure(monkeypatch):
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _http_error(429, retry_after="3"),
            _http_error(429, retry_after="3"),
        ],
    )
    slept = []
    client = XeroAccountingClient()
    result = client.list_bank_transactions(
        tenant_id="tenant-1", access_token="access-1", since=_SINCE, sleep_fn=slept.append
    )

    assert result.status == XeroOutcomeStatus.RATE_LIMITED
    assert len(slept) == 1  # exactly one bounded retry, never unbounded


@pytest.mark.parametrize("code", [401, 403])
def test_list_bank_transactions_auth_error_statuses(monkeypatch, code):
    _patch_urlopen_sequence(monkeypatch, responses=[_http_error(code, body=b"nope")])
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)
    assert result.status == XeroOutcomeStatus.AUTH_ERROR


def test_list_bank_transactions_other_http_error_is_provider_error(monkeypatch):
    _patch_urlopen_sequence(monkeypatch, responses=[_http_error(500, body=b"boom")])
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)
    assert result.status == XeroOutcomeStatus.PROVIDER_ERROR


def test_list_bank_transactions_malformed_json_is_malformed_response(monkeypatch):
    _patch_urlopen_sequence(monkeypatch, responses=[_FakeHTTPResponse(b"not json at all")])
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)
    assert result.status == XeroOutcomeStatus.MALFORMED_RESPONSE


def test_list_bank_transactions_exceeding_the_bounded_page_limit_is_an_honest_failure_never_a_silent_truncation(
    monkeypatch,
):
    """Hitting `_MAX_BANK_TRANSACTION_PAGES` without Xero ever returning
    an empty page is a genuine `MALFORMED_RESPONSE`-class failure —
    never a silently truncated 'complete' result (see the method's own
    docstring). Monkeypatches the bound down to 3 pages so this test
    does not need 50 real scripted responses."""
    import services.xero.client as client_module

    monkeypatch.setattr(client_module, "_MAX_BANK_TRANSACTION_PAGES", 3)
    _patch_urlopen_sequence(
        monkeypatch,
        responses=[
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-1")]}),
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-2")]}),
            _ok({"BankTransactions": [_bank_transaction_row(bank_transaction_id="bt-3")]}),
            # A 4th page would exist in reality, but the bound is hit
            # before it is ever fetched.
        ],
    )
    client = XeroAccountingClient()
    result = client.list_bank_transactions(tenant_id="tenant-1", access_token="access-1", since=_SINCE)

    assert result.status == XeroOutcomeStatus.MALFORMED_RESPONSE
    assert "bounded page limit" in result.error_detail


# ---------------------------------------------------------------------
# OAUTH_SCOPES — CD-6 supplier-domain-correlation scope extension
# ---------------------------------------------------------------------


def test_oauth_scopes_is_extended_with_exactly_the_three_new_read_only_scopes():
    assert OAUTH_SCOPES == (
        "openid profile email offline_access accounting.settings.read "
        "accounting.contacts.read accounting.invoices.read accounting.banktransactions.read"
    )
    # No write-capable scope token anywhere in the string.
    assert ".write" not in OAUTH_SCOPES
    for token in OAUTH_SCOPES.split(" "):
        assert not token.endswith(".write")


# ---------------------------------------------------------------------
# GET-only proof across the whole module (grep-confirmable statically —
# this test proves it dynamically too): the only `method="POST"` call
# site in this file remains the OAuth token exchange.
# ---------------------------------------------------------------------


def test_the_only_post_call_site_in_client_py_is_the_oauth_token_exchange():
    import inspect

    import services.xero.client as client_module

    source = inspect.getsource(client_module)
    assert source.count('method="POST"') == 1
    # list_connections, list_accounts, list_contacts,
    # list_purchase_invoices, and _bank_transactions_page_request (the
    # ONE source-level GET-request builder list_bank_transactions calls
    # — once for the initial page fetch, once again for its own single
    # bounded rate-limit retry, both at runtime, but only ONE literal
    # `method="GET"` call site in the source).
    assert source.count('method="GET"') == 5
