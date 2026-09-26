"""``FakeImapClient`` — a deterministic, no-I/O substitute for
``services.mailbox.imap.imap_client.ImapClient`` (CD-6 GUI-operations-
foundation follow-on WO — second mailbox provider), mirroring
``services.mailbox.microsoft.fake_client``'s established pattern
exactly: fully scripted by the test/caller, satisfies the SAME
``ImapClientProtocol`` the real adapter does, never mistaken for the
real thing.

**This is what the entire IMAP adapter/sweep test suite is built and
run against** — no real network/TLS/`mail.noust.ai` connection exists
in this delivery's own test suite (no credentials exist yet — see
``services/mailbox/imap/secrets.py``'s own module docstring); every
adapter/sweep test drives this fake instead of the real
network-speaking ``ImapClient``.

Read-only by construction (mirrors `FakeMicrosoftGraphClient`'s own
doctrine): every method here reads a pre-scripted queue and returns it;
none of them can be called to simulate a write against a real mailbox,
because no write-shaped method exists on either this class or the
`ImapClientProtocol` it satisfies — see `imap_client.py`'s own module
docstring for why that absence is deliberate and permanent.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Sequence

from services.mailbox.imap.imap_client import (
    ImapCapabilityResult,
    ImapConnectResult,
    ImapFetchContentResult,
    ImapFetchHeadersResult,
    ImapFolderListResult,
    ImapOutcomeStatus,
    ImapSearchResult,
    ImapSession,
)


@dataclass(frozen=True)
class RecordedFetchHeadersCall:
    folder: str
    uids: tuple


@dataclass(frozen=True)
class RecordedFetchContentCall:
    folder: str
    uid: int


class FakeImapSessionToken:
    """A trivial, opaque stand-in for a real socket — `FakeImapClient`
    never opens anything real, so this exists purely to satisfy
    `ImapSession`'s own `connection` field shape without lying about
    holding a genuine `imaplib.IMAP4_SSL` instance."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<FakeImapSessionToken>"


class FakeImapClient:
    """Deterministic `ImapClientProtocol` implementation. Script
    outcomes with `queue_connect_result`/`queue_capability_result`/
    `queue_list_folders_result`/`queue_search_result`/
    `queue_fetch_headers_result`/`queue_fetch_content_result`; each call
    pops the next queued outcome for that method, or raises
    `AssertionError` if nothing was queued (mirrors
    `services.xero.fake_client`/`FakeMicrosoftGraphClient` exactly).

    Test-observability recorders (mirrors `FakeMicrosoftGraphClient
    .delta_calls`/`.content_calls`) — used by the security/read-only
    proof tests to assert this client's own `uid_fetch_headers`/
    `uid_fetch_content` methods were called with the RIGHT arguments,
    and that no mutating method was ever called (trivially true here:
    no such method exists to call at all).
    """

    def __init__(self) -> None:
        self._connect_queue: Deque[ImapConnectResult] = deque()
        self._capability_queue: Deque[ImapCapabilityResult] = deque()
        self._list_folders_queue: Deque[ImapFolderListResult] = deque()
        self._search_queue: Deque[ImapSearchResult] = deque()
        self._fetch_headers_queue: Deque[ImapFetchHeadersResult] = deque()
        self._fetch_content_queue: Deque[ImapFetchContentResult] = deque()

        self.connect_calls: list[dict] = []
        self.capability_calls: int = 0
        self.list_folders_calls: int = 0
        self.search_calls: list[dict] = []
        self.fetch_headers_calls: list[RecordedFetchHeadersCall] = []
        self.fetch_content_calls: list[RecordedFetchContentCall] = []
        self.logout_calls: int = 0

    # -- scripting ---------------------------------------------------

    def queue_connect_result(self, result: ImapConnectResult) -> None:
        self._connect_queue.append(result)

    def queue_capability_result(self, result: ImapCapabilityResult) -> None:
        self._capability_queue.append(result)

    def queue_list_folders_result(self, result: ImapFolderListResult) -> None:
        self._list_folders_queue.append(result)

    def queue_search_result(self, result: ImapSearchResult) -> None:
        self._search_queue.append(result)

    def queue_fetch_headers_result(self, result: ImapFetchHeadersResult) -> None:
        self._fetch_headers_queue.append(result)

    def queue_fetch_content_result(self, result: ImapFetchContentResult) -> None:
        self._fetch_content_queue.append(result)

    # -- ImapClientProtocol -------------------------------------------

    def connect_and_login(self, *, host: str, port: int, username: str, password: str, timeout_seconds: float = 15.0):
        self.connect_calls.append({"host": host, "port": port, "username": username})
        if not self._connect_queue:
            raise AssertionError("FakeImapClient.connect_and_login() called with nothing queued")
        result = self._connect_queue.popleft()
        if result.status == ImapOutcomeStatus.OK and result.session is None:
            # Convenience: a test that queues a bare OK without wiring
            # its own session token still gets a real, distinct
            # ImapSession — mirrors how a real successful connect always
            # carries one.
            result = ImapConnectResult(
                status=ImapOutcomeStatus.OK,
                session=ImapSession(connection=FakeImapSessionToken()),  # type: ignore[arg-type]
                tls_version=result.tls_version,
                tls_cipher=result.tls_cipher,
                peer_cert_subject=result.peer_cert_subject,
                peer_cert_issuer=result.peer_cert_issuer,
            )
        return result

    def capability(self, session: ImapSession) -> ImapCapabilityResult:
        self.capability_calls += 1
        if not self._capability_queue:
            raise AssertionError("FakeImapClient.capability() called with nothing queued")
        return self._capability_queue.popleft()

    def list_folders(self, session: ImapSession) -> ImapFolderListResult:
        self.list_folders_calls += 1
        if not self._list_folders_queue:
            raise AssertionError("FakeImapClient.list_folders() called with nothing queued")
        return self._list_folders_queue.popleft()

    def uid_search(self, session: ImapSession, *, folder: str, criteria: str) -> ImapSearchResult:
        self.search_calls.append({"folder": folder, "criteria": criteria})
        if not self._search_queue:
            raise AssertionError("FakeImapClient.uid_search() called with nothing queued")
        return self._search_queue.popleft()

    def uid_fetch_headers(self, session: ImapSession, *, folder: str, uids: Sequence[int]) -> ImapFetchHeadersResult:
        self.fetch_headers_calls.append(RecordedFetchHeadersCall(folder=folder, uids=tuple(uids)))
        if not self._fetch_headers_queue:
            raise AssertionError("FakeImapClient.uid_fetch_headers() called with nothing queued")
        return self._fetch_headers_queue.popleft()

    def uid_fetch_content(self, session: ImapSession, *, folder: str, uid: int) -> ImapFetchContentResult:
        self.fetch_content_calls.append(RecordedFetchContentCall(folder=folder, uid=uid))
        if not self._fetch_content_queue:
            raise AssertionError("FakeImapClient.uid_fetch_content() called with nothing queued")
        return self._fetch_content_queue.popleft()

    def logout(self, session: ImapSession) -> None:
        self.logout_calls += 1

    # `examine_folder` is deliberately NOT part of `ImapClientProtocol`'s
    # own narrow surface as consumed by `imap_adapter.py` (the adapter
    # only ever calls `uid_search`/`uid_fetch_headers`/
    # `uid_fetch_content`, each of which internally EXAMINEs on the real
    # client — see `imap_client.py`). This fake therefore has no
    # `examine_folder` method either; `uid_search`'s own queued
    # `ImapSearchResult.uidvalidity` is how a test controls the
    # UIDVALIDITY the adapter observes.
