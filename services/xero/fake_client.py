"""``FakeXeroOAuthClient``/``FakeXeroAccountingClient`` — deterministic,
no-I/O substitutes for ``services.xero.client``'s real adapters (CD-6
Slice 2), mirroring this codebase's established ``Fake*`` pattern
exactly (``ai/providers/litellm/fake.py``, ``agent/claude_code/fake.py``
— see those modules' own docstrings for the doctrine this follows:
fully scripted by the test/caller, satisfies the same ``Protocol`` the
real adapter does, never mistaken for the real thing).

This is what CD-6 Slice 2's own dispatch requires everything be tested
against: no real Xero Developer App/credentials exist yet, so every
sync/OAuth-flow test in this delivery drives these fakes, never the
real network-speaking classes in ``client.py``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from services.xero.client import (
    XeroAccountsResult,
    XeroConnectionInfo,
    XeroConnectionsResult,
    XeroContactsResult,
    XeroInvoicesResult,
    XeroOutcomeStatus,
    XeroTokenBundle,
    XeroTokenResult,
)


@dataclass(frozen=True)
class RecordedTokenCall:
    kind: str  # "exchange_code" | "refresh"
    detail: str  # the code or refresh_token supplied


class FakeXeroOAuthClient:
    """Deterministic `XeroOAuthClientProtocol` implementation. Script
    outcomes with `queue_exchange_result`/`queue_refresh_result`/
    `queue_connections_result`; each call pops the next queued outcome
    for that method, or raises `AssertionError` if nothing was queued
    (an unscripted call is a test/dev-composition bug, never a silently
    fabricated success — same discipline `FakeLiteLLMClient` documents)."""

    def __init__(self, *, configured: bool = True) -> None:
        self._exchange_queue: Deque[XeroTokenResult] = deque()
        self._refresh_queue: Deque[XeroTokenResult] = deque()
        self._connections_queue: Deque[XeroConnectionsResult] = deque()
        self.token_calls: list[RecordedTokenCall] = []
        self.authorize_urls: list[str] = []
        #: Scripted answer for `is_configured()` — defaults to `True`
        #: (this codebase's tests overwhelmingly want to exercise the
        #: REAL flow, not the "not configured" honest-degradation path)
        #: so a test that specifically wants the "Xero not configured"
        #: state sets this `False` explicitly (or constructs with
        #: `configured=False`) rather than every ordinary test having to
        #: remember to flip it on.
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured

    def queue_exchange_result(self, result: XeroTokenResult) -> None:
        self._exchange_queue.append(result)

    def queue_refresh_result(self, result: XeroTokenResult) -> None:
        self._refresh_queue.append(result)

    def queue_connections_result(self, result: XeroConnectionsResult) -> None:
        self._connections_queue.append(result)

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        url = f"https://login.xero.com/identity/connect/authorize?state={state}&redirect_uri={redirect_uri}"
        self.authorize_urls.append(url)
        return url

    def exchange_code(self, *, code: str, redirect_uri: str) -> XeroTokenResult:
        self.token_calls.append(RecordedTokenCall(kind="exchange_code", detail=code))
        if not self._exchange_queue:
            raise AssertionError("FakeXeroOAuthClient.exchange_code() called with nothing queued")
        return self._exchange_queue.popleft()

    def refresh(self, *, refresh_token: str) -> XeroTokenResult:
        self.token_calls.append(RecordedTokenCall(kind="refresh", detail=refresh_token))
        if not self._refresh_queue:
            raise AssertionError("FakeXeroOAuthClient.refresh() called with nothing queued")
        return self._refresh_queue.popleft()

    def list_connections(self, *, access_token: str) -> XeroConnectionsResult:
        if not self._connections_queue:
            raise AssertionError("FakeXeroOAuthClient.list_connections() called with nothing queued")
        return self._connections_queue.popleft()


class FakeXeroAccountingClient:
    """Deterministic `XeroAccountingClientProtocol` implementation.
    Script outcomes with `queue_accounts_result`/`queue_contacts_result`/
    `queue_invoices_result`; each corresponding `list_*()` call pops the
    next queued outcome for THAT method, or raises `AssertionError` if
    nothing was queued for it (an unscripted call is a test/dev-
    composition bug, never a silently fabricated success — same
    discipline every other `Fake*` in this codebase documents)."""

    def __init__(self) -> None:
        self._queue: Deque[XeroAccountsResult] = deque()
        self._contacts_queue: Deque[XeroContactsResult] = deque()
        self._invoices_queue: Deque[XeroInvoicesResult] = deque()
        self.calls: list[tuple[str, str]] = []  # (tenant_id, access_token)
        self.contact_calls: list[tuple[str, str]] = []
        self.invoice_calls: list[tuple[str, str]] = []

    def queue_accounts_result(self, result: XeroAccountsResult) -> None:
        self._queue.append(result)

    def queue_contacts_result(self, result: XeroContactsResult) -> None:
        self._contacts_queue.append(result)

    def queue_invoices_result(self, result: XeroInvoicesResult) -> None:
        self._invoices_queue.append(result)

    def list_accounts(self, *, tenant_id: str, access_token: str) -> XeroAccountsResult:
        self.calls.append((tenant_id, access_token))
        if not self._queue:
            raise AssertionError("FakeXeroAccountingClient.list_accounts() called with nothing queued")
        return self._queue.popleft()

    def list_contacts(self, *, tenant_id: str, access_token: str) -> XeroContactsResult:
        self.contact_calls.append((tenant_id, access_token))
        if not self._contacts_queue:
            raise AssertionError("FakeXeroAccountingClient.list_contacts() called with nothing queued")
        return self._contacts_queue.popleft()

    def list_purchase_invoices(self, *, tenant_id: str, access_token: str) -> XeroInvoicesResult:
        self.invoice_calls.append((tenant_id, access_token))
        if not self._invoices_queue:
            raise AssertionError("FakeXeroAccountingClient.list_purchase_invoices() called with nothing queued")
        return self._invoices_queue.popleft()


def fake_token_bundle(*, access_token: str = "fake-access-token", expires_in_seconds: float = 1800.0) -> XeroTokenBundle:
    """Convenience builder for a plausible, clearly-fake
    :class:`XeroTokenBundle` — used throughout the test suite so every
    test does not have to hand-construct one."""
    from datetime import datetime, timedelta, timezone

    return XeroTokenBundle(
        access_token=access_token,
        refresh_token="fake-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
    )
