"""``FakeGmailOAuthClient``/``FakeGmailClient`` — deterministic, no-I/O
substitutes for ``services.mailbox.gmail.gmail_client``'s real adapters
(CD-6 GUI-operations-foundation follow-on WO — third mailbox provider),
mirroring ``services.mailbox.microsoft.fake_client``'s established
pattern exactly: fully scripted by the test/caller, satisfies the same
``Protocol`` the real adapter does, never mistaken for the real thing.

**This is what this entire delivery is built and tested against** — no
real Google Cloud OAuth app/credentials exist yet (this delivery's own
hard constraint), so every OAuth-flow/sweep/adapter test drives these
fakes, never the real network-speaking classes in ``gmail_client.py``.

Read-only by construction (mirrors `FakeMicrosoftGraphClient`'s own
doctrine): every method here reads a pre-scripted queue and returns it;
none of them can be called to simulate a write against a real mailbox,
because no write-shaped method exists on either this class or the
``GmailClientProtocol`` it satisfies.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Sequence

from services.mailbox.gmail.gmail_client import (
    DEFAULT_METADATA_HEADERS,
    DEFAULT_PAGE_SIZE,
    GmailIdentityResult,
    GmailLabelListResult,
    GmailMessageListPageResult,
    GmailMessageMetadataResult,
    GmailMessageRawResult,
    GmailTokenBundle,
    GmailTokenResult,
)


@dataclass(frozen=True)
class RecordedTokenCall:
    kind: str  # "exchange_code" | "refresh"
    detail: str


class FakeGmailOAuthClient:
    """Deterministic `GmailOAuthClientProtocol` implementation. Script
    outcomes with `queue_exchange_result`/`queue_refresh_result`/
    `queue_identity_result`; each call pops the next queued outcome for
    that method, or raises `AssertionError` if nothing was queued
    (mirrors `services.mailbox.microsoft.fake_client
    .FakeMicrosoftOAuthClient` exactly)."""

    def __init__(self, *, configured: bool = True) -> None:
        self._exchange_queue: Deque[GmailTokenResult] = deque()
        self._refresh_queue: Deque[GmailTokenResult] = deque()
        self._identity_queue: Deque[GmailIdentityResult] = deque()
        self.token_calls: list[RecordedTokenCall] = []
        self.authorize_urls: list[str] = []
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured

    def queue_exchange_result(self, result: GmailTokenResult) -> None:
        self._exchange_queue.append(result)

    def queue_refresh_result(self, result: GmailTokenResult) -> None:
        self._refresh_queue.append(result)

    def queue_identity_result(self, result: GmailIdentityResult) -> None:
        self._identity_queue.append(result)

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            f"state={state}&redirect_uri={redirect_uri}&access_type=offline&prompt=consent"
        )
        self.authorize_urls.append(url)
        return url

    def exchange_code(self, *, code: str, redirect_uri: str) -> GmailTokenResult:
        self.token_calls.append(RecordedTokenCall(kind="exchange_code", detail=code))
        if not self._exchange_queue:
            raise AssertionError("FakeGmailOAuthClient.exchange_code() called with nothing queued")
        return self._exchange_queue.popleft()

    def refresh(self, *, refresh_token: str) -> GmailTokenResult:
        self.token_calls.append(RecordedTokenCall(kind="refresh", detail=refresh_token))
        if not self._refresh_queue:
            raise AssertionError("FakeGmailOAuthClient.refresh() called with nothing queued")
        return self._refresh_queue.popleft()

    def get_identity(self, *, access_token: str) -> GmailIdentityResult:
        if not self._identity_queue:
            raise AssertionError("FakeGmailOAuthClient.get_identity() called with nothing queued")
        return self._identity_queue.popleft()


@dataclass(frozen=True)
class RecordedListMessagesCall:
    label_id: str
    query: Optional[str]
    page_token: Optional[str]
    max_results: int


class FakeGmailClient:
    """Deterministic `GmailClientProtocol` implementation. Script
    outcomes with `queue_labels_result`/`queue_list_messages_result`/
    `queue_metadata_result`/`queue_raw_result`; each call pops the next
    queued outcome for that method, or raises `AssertionError` if
    nothing was queued."""

    def __init__(self) -> None:
        self._labels_queue: Deque[GmailLabelListResult] = deque()
        self._list_messages_queue: Deque[GmailMessageListPageResult] = deque()
        self._metadata_queue: Deque[GmailMessageMetadataResult] = deque()
        self._raw_queue: Deque[GmailMessageRawResult] = deque()

        self.labels_calls: int = 0
        self.list_messages_calls: list[RecordedListMessagesCall] = []
        self.metadata_calls: list[str] = []
        self.raw_calls: list[str] = []

    def queue_labels_result(self, result: GmailLabelListResult) -> None:
        self._labels_queue.append(result)

    def queue_list_messages_result(self, result: GmailMessageListPageResult) -> None:
        self._list_messages_queue.append(result)

    def queue_metadata_result(self, result: GmailMessageMetadataResult) -> None:
        self._metadata_queue.append(result)

    def queue_raw_result(self, result: GmailMessageRawResult) -> None:
        self._raw_queue.append(result)

    def list_labels(self, *, access_token: str) -> GmailLabelListResult:
        self.labels_calls += 1
        if not self._labels_queue:
            raise AssertionError("FakeGmailClient.list_labels() called with nothing queued")
        return self._labels_queue.popleft()

    def list_messages(
        self,
        *,
        access_token: str,
        label_id: str,
        query: Optional[str] = None,
        page_token: Optional[str] = None,
        max_results: int = DEFAULT_PAGE_SIZE,
    ) -> GmailMessageListPageResult:
        self.list_messages_calls.append(
            RecordedListMessagesCall(label_id=label_id, query=query, page_token=page_token, max_results=max_results)
        )
        if not self._list_messages_queue:
            raise AssertionError("FakeGmailClient.list_messages() called with nothing queued")
        return self._list_messages_queue.popleft()

    def fetch_message_metadata(
        self, *, access_token: str, message_id: str, metadata_headers: Sequence[str] = DEFAULT_METADATA_HEADERS
    ) -> GmailMessageMetadataResult:
        self.metadata_calls.append(message_id)
        if not self._metadata_queue:
            raise AssertionError("FakeGmailClient.fetch_message_metadata() called with nothing queued")
        return self._metadata_queue.popleft()

    def fetch_message_raw(self, *, access_token: str, message_id: str) -> GmailMessageRawResult:
        self.raw_calls.append(message_id)
        if not self._raw_queue:
            raise AssertionError("FakeGmailClient.fetch_message_raw() called with nothing queued")
        return self._raw_queue.popleft()


def fake_token_bundle(*, access_token: str = "fake-gmail-access-token", expires_in_seconds: float = 3600.0) -> GmailTokenBundle:
    """Convenience builder for a plausible, clearly-fake
    :class:`GmailTokenBundle`."""
    from datetime import datetime, timedelta, timezone

    return GmailTokenBundle(
        access_token=access_token,
        refresh_token="fake-gmail-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
    )
