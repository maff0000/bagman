"""``FakeMicrosoftOAuthClient``/``FakeMicrosoftGraphClient`` — deterministic,
no-I/O substitutes for ``services.mailbox.microsoft.graph_client``'s
real adapters (CD-6 Slice 4), mirroring ``services.xero.fake_client``'s
established pattern exactly: fully scripted by the test/caller,
satisfies the same ``Protocol`` the real adapter does, never mistaken
for the real thing.

**This is what this entire slice is built and tested against** — no
real Microsoft Entra app/credentials exist yet (this delivery's own
hard constraint), so every OAuth-flow/sweep/adapter test in this
delivery drives these fakes, never the real network-speaking classes in
``graph_client.py``.

Read-only by construction (architect spec — "build your
FakeMicrosoftGraphClient to only ever expose read operations, matching
FakeXeroAccountingClient's own discipline"): every method here reads a
pre-scripted queue and returns it; none of them can be called to
simulate a write against a real mailbox, because no write-shaped method
exists on either this class or the ``Protocol`` it satisfies.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from services.mailbox.microsoft.graph_client import (
    GraphDeltaPageResult,
    GraphMessageContentResult,
    GraphOutcomeStatus,
    MicrosoftIdentityResult,
    MicrosoftTokenBundle,
    MicrosoftTokenResult,
)


@dataclass(frozen=True)
class RecordedTokenCall:
    kind: str  # "exchange_code" | "refresh"
    detail: str


class FakeMicrosoftOAuthClient:
    """Deterministic `MicrosoftOAuthClientProtocol` implementation.
    Script outcomes with `queue_exchange_result`/`queue_refresh_result`/
    `queue_me_result`; each call pops the next queued outcome for that
    method, or raises `AssertionError` if nothing was queued (mirrors
    `services.xero.fake_client.FakeXeroOAuthClient` exactly)."""

    def __init__(self, *, configured: bool = True) -> None:
        self._exchange_queue: Deque[MicrosoftTokenResult] = deque()
        self._refresh_queue: Deque[MicrosoftTokenResult] = deque()
        self._me_queue: Deque[MicrosoftIdentityResult] = deque()
        self.token_calls: list[RecordedTokenCall] = []
        self.authorize_urls: list[str] = []
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured

    def queue_exchange_result(self, result: MicrosoftTokenResult) -> None:
        self._exchange_queue.append(result)

    def queue_refresh_result(self, result: MicrosoftTokenResult) -> None:
        self._refresh_queue.append(result)

    def queue_me_result(self, result: MicrosoftIdentityResult) -> None:
        self._me_queue.append(result)

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        url = f"https://login.microsoftonline.com/fake-tenant/oauth2/v2.0/authorize?state={state}&redirect_uri={redirect_uri}"
        self.authorize_urls.append(url)
        return url

    def exchange_code(self, *, code: str, redirect_uri: str) -> MicrosoftTokenResult:
        self.token_calls.append(RecordedTokenCall(kind="exchange_code", detail=code))
        if not self._exchange_queue:
            raise AssertionError("FakeMicrosoftOAuthClient.exchange_code() called with nothing queued")
        return self._exchange_queue.popleft()

    def refresh(self, *, refresh_token: str) -> MicrosoftTokenResult:
        self.token_calls.append(RecordedTokenCall(kind="refresh", detail=refresh_token))
        if not self._refresh_queue:
            raise AssertionError("FakeMicrosoftOAuthClient.refresh() called with nothing queued")
        return self._refresh_queue.popleft()

    def get_me(self, *, access_token: str) -> MicrosoftIdentityResult:
        if not self._me_queue:
            raise AssertionError("FakeMicrosoftOAuthClient.get_me() called with nothing queued")
        return self._me_queue.popleft()


class FakeMicrosoftGraphClient:
    """Deterministic `MicrosoftGraphClientProtocol` implementation.
    Script outcomes with `queue_delta_result`/`queue_content_result`;
    each call pops the next queued outcome, or raises `AssertionError`
    if nothing was queued."""

    def __init__(self) -> None:
        self._delta_queue: Deque[GraphDeltaPageResult] = deque()
        self._content_queue: Deque[GraphMessageContentResult] = deque()
        self.delta_calls: list[dict] = []
        self.content_calls: list[str] = []

    def queue_delta_result(self, result: GraphDeltaPageResult) -> None:
        self._delta_queue.append(result)

    def queue_content_result(self, result: GraphMessageContentResult) -> None:
        self._content_queue.append(result)

    def fetch_delta(
        self,
        *,
        access_token: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp=None,
    ) -> GraphDeltaPageResult:
        self.delta_calls.append(
            {
                "folder": folder,
                "delta_link": delta_link,
                "next_link": next_link,
                "bootstrap_timestamp": bootstrap_timestamp,
            }
        )
        if not self._delta_queue:
            raise AssertionError("FakeMicrosoftGraphClient.fetch_delta() called with nothing queued")
        return self._delta_queue.popleft()

    def fetch_message_content(self, *, access_token: str, immutable_message_id: str) -> GraphMessageContentResult:
        self.content_calls.append(immutable_message_id)
        if not self._content_queue:
            raise AssertionError("FakeMicrosoftGraphClient.fetch_message_content() called with nothing queued")
        return self._content_queue.popleft()


def fake_token_bundle(*, access_token: str = "fake-ms-access-token", expires_in_seconds: float = 3600.0) -> MicrosoftTokenBundle:
    """Convenience builder for a plausible, clearly-fake
    :class:`MicrosoftTokenBundle`."""
    from datetime import datetime, timedelta, timezone

    return MicrosoftTokenBundle(
        access_token=access_token,
        refresh_token="fake-ms-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
    )
