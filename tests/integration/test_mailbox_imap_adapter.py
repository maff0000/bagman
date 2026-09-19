"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.imap.imap_adapter.ImapMailboxAdapter` — folder
discovery (with/without SPECIAL-USE), message identity
compose/decompose, and delta-page pagination/UIDVALIDITY-epoch
behaviour. All driven by `FakeImapClient` — zero real network/TLS.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services.mailbox.imap.fake_imap_client import FakeImapClient
from services.mailbox.imap.imap_adapter import (
    ImapMailboxAdapter,
    compose_immutable_message_id,
    decompose_immutable_message_id,
)
from services.mailbox.imap.imap_client import (
    ImapCapabilityResult,
    ImapConnectResult,
    ImapFetchHeadersResult,
    ImapFolderInfo,
    ImapFolderListResult,
    ImapMessageHeaders,
    ImapOutcomeStatus,
    ImapSearchResult,
)
from services.mailbox.imap.secrets import NoustAIImapCredentials
from services.mailbox.mailbox import InMemoryMailboxSourceRepository


def _adapter():
    client = FakeImapClient()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = ImapMailboxAdapter(
        client=client,
        mailbox_repository=mailbox_repo,
        credentials_provider=lambda: NoustAIImapCredentials(username="matt@noust.ai", password="pw"),
    )
    return client, adapter


def _queue_connect_ok(client: FakeImapClient) -> None:
    client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))


# -- message identity -------------------------------------------------


def test_compose_and_decompose_round_trip():
    composed = compose_immutable_message_id(folder="INBOX", uidvalidity=1001, uid=42)
    decomposed = decompose_immutable_message_id(composed)
    assert decomposed is not None
    assert decomposed.folder == "INBOX"
    assert decomposed.uidvalidity == 1001
    assert decomposed.uid == 42


def test_decompose_returns_none_for_a_foreign_id():
    assert decompose_immutable_message_id("some-graph-immutable-id") is None


def test_two_different_uidvalidity_epochs_never_collide_for_the_same_uid():
    id_epoch_1 = compose_immutable_message_id(folder="INBOX", uidvalidity=1, uid=5)
    id_epoch_2 = compose_immutable_message_id(folder="INBOX", uidvalidity=2, uid=5)
    assert id_epoch_1 != id_epoch_2


# -- folder discovery ---------------------------------------------------


def test_discovery_config_error_when_credentials_absent():
    client = FakeImapClient()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = ImapMailboxAdapter(client=client, mailbox_repository=mailbox_repo, credentials_provider=lambda: None)
    result = adapter.discover_monitored_folders(mailbox_id="mb-1")
    assert result.status == ImapOutcomeStatus.CONFIG_ERROR
    assert client.connect_calls == []


def test_discovery_with_special_use_advertised_monitors_junk_and_trash_excludes_sent_drafts():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_capability_result(ImapCapabilityResult(status=ImapOutcomeStatus.OK, capabilities=("IMAP4rev1", "SPECIAL-USE")))
    client.queue_list_folders_result(
        ImapFolderListResult(
            status=ImapOutcomeStatus.OK,
            folders=(
                ImapFolderInfo(name="INBOX", flags=("\\HasNoChildren",)),
                ImapFolderInfo(name="Junk", flags=("\\HasNoChildren", "\\Junk")),
                ImapFolderInfo(name="Trash", flags=("\\HasNoChildren", "\\Trash")),
                ImapFolderInfo(name="Sent", flags=("\\HasNoChildren", "\\Sent")),
                ImapFolderInfo(name="Drafts", flags=("\\HasNoChildren", "\\Drafts")),
                ImapFolderInfo(name="Archive", flags=("\\HasNoChildren", "\\Archive")),
                ImapFolderInfo(name="[Gmail]", flags=("\\Noselect", "\\HasChildren")),
            ),
        )
    )
    result = adapter.discover_monitored_folders(mailbox_id="mb-1")
    assert result.status == ImapOutcomeStatus.OK
    names = {f.folder_id for f in result.folders}
    assert names == {"INBOX", "Junk", "Trash"}
    assert client.logout_calls == 1


def test_discovery_without_special_use_falls_back_to_conservative_name_match():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_capability_result(ImapCapabilityResult(status=ImapOutcomeStatus.OK, capabilities=("IMAP4rev1",)))
    client.queue_list_folders_result(
        ImapFolderListResult(
            status=ImapOutcomeStatus.OK,
            folders=(
                ImapFolderInfo(name="INBOX"),
                ImapFolderInfo(name="Spam"),
                ImapFolderInfo(name="Deleted Items"),
                ImapFolderInfo(name="Sent Items"),
                ImapFolderInfo(name="Archive"),
                ImapFolderInfo(name="Trash Talk"),  # must NOT fuzzy-match "Trash"
            ),
        )
    )
    result = adapter.discover_monitored_folders(mailbox_id="mb-1")
    assert result.status == ImapOutcomeStatus.OK
    names = {f.folder_id for f in result.folders}
    assert names == {"INBOX", "Spam", "Deleted Items"}


def test_discovery_capability_failure_is_a_whole_call_failure():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_capability_result(ImapCapabilityResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail="boom"))
    result = adapter.discover_monitored_folders(mailbox_id="mb-1")
    assert result.status == ImapOutcomeStatus.PROVIDER_ERROR
    assert client.logout_calls == 1  # still cleans up the connection


def test_discovery_auth_error_on_connect_never_calls_capability_or_list():
    client, adapter = _adapter()
    client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.AUTH_ERROR, error_detail="bad password"))
    result = adapter.discover_monitored_folders(mailbox_id="mb-1")
    assert result.status == ImapOutcomeStatus.AUTH_ERROR
    assert client.capability_calls == 0
    assert client.list_folders_calls == 0


def test_discovery_tls_validation_failure_surfaces_distinctly():
    client, adapter = _adapter()
    client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.TLS_VALIDATION_FAILED, error_detail="cert mismatch"))
    result = adapter.discover_monitored_folders(mailbox_id="mb-1")
    assert result.status == ImapOutcomeStatus.TLS_VALIDATION_FAILED


# -- fetch_folder_delta / pagination / UIDVALIDITY epoch -----------------


def _headers_for(uid: int, *, subject: str = "Invoice", sender: str = "billing@vendor.com") -> ImapMessageHeaders:
    raw = (
        {"name": "Subject", "value": subject},
        {"name": "From", "value": f"Vendor <{sender}>"},
        {"name": "Message-ID", "value": f"<msg-{uid}@vendor.com>"},
        {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
    )
    return ImapMessageHeaders(uid=uid, raw_headers=raw)


def test_bootstrap_round_uses_since_criteria_and_sets_delta_link():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(1, 2), uidvalidity=100))
    client.queue_fetch_headers_result(
        ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_headers_for(1), _headers_for(2)))
    )
    page = adapter.fetch_folder_delta(
        mailbox_id="mb-1", folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    assert page.status == ImapOutcomeStatus.OK
    assert len(page.messages) == 2
    assert page.delta_link is not None
    assert "SINCE" in client.search_calls[0]["criteria"]
    assert page.messages[0].immutable_id == compose_immutable_message_id(folder="INBOX", uidvalidity=100, uid=1)
    assert page.messages[0].internet_message_id == "<msg-1@vendor.com>"
    assert page.messages[0].sender_address == "billing@vendor.com"
    assert page.messages[0].subject == "Invoice"


def test_pagination_splits_a_large_uid_set_across_multiple_pages():
    client, adapter = _adapter()
    uids = tuple(range(1, 61))  # 60 > _PAGE_SIZE (50)
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=uids, uidvalidity=100))
    client.queue_fetch_headers_result(
        ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=tuple(_headers_for(u) for u in uids[:50]))
    )
    first_page = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", bootstrap_timestamp=datetime.now(timezone.utc))
    assert first_page.status == ImapOutcomeStatus.OK
    assert len(first_page.messages) == 50
    assert first_page.next_link is not None
    assert first_page.delta_link is None

    _queue_connect_ok(client)
    client.queue_fetch_headers_result(
        ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=tuple(_headers_for(u) for u in uids[50:]))
    )
    second_page = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", next_link=first_page.next_link)
    assert second_page.status == ImapOutcomeStatus.OK
    assert len(second_page.messages) == 10
    assert second_page.next_link is None
    assert second_page.delta_link is not None


def test_same_epoch_second_round_only_searches_uids_after_last_seen():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(1, 2, 3), uidvalidity=100))
    client.queue_fetch_headers_result(
        ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=tuple(_headers_for(u) for u in (1, 2, 3)))
    )
    first = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", bootstrap_timestamp=datetime.now(timezone.utc))

    _queue_connect_ok(client)
    # probe (ALL) returns same uidvalidity
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(1, 2, 3, 4), uidvalidity=100))
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(4,), uidvalidity=100))
    client.queue_fetch_headers_result(ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_headers_for(4),)))
    second = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", delta_link=first.delta_link)
    assert second.status == ImapOutcomeStatus.OK
    assert len(second.messages) == 1
    assert second.messages[0].immutable_id == compose_immutable_message_id(folder="INBOX", uidvalidity=100, uid=4)
    # The second search call (after the ALL probe) must be UID-bounded.
    assert client.search_calls[-1]["criteria"] == "UID 4:*"


def test_uidvalidity_change_between_sweeps_is_a_distinct_epoch_never_collapsed():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(5,), uidvalidity=100))
    client.queue_fetch_headers_result(ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_headers_for(5),)))
    first = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", bootstrap_timestamp=datetime.now(timezone.utc))
    old_id = first.messages[0].immutable_id

    # UIDVALIDITY changed — the probe (ALL) now reports a NEW epoch.
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(5,), uidvalidity=200))
    client.queue_fetch_headers_result(ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_headers_for(5),)))
    second = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", delta_link=first.delta_link)
    new_id = second.messages[0].immutable_id

    assert old_id != new_id
    assert compose_immutable_message_id(folder="INBOX", uidvalidity=200, uid=5) == new_id


def test_fetch_folder_delta_never_calls_uid_fetch_content():
    """Stage-A discovery is headers-only — see module docstring. Proves
    the adapter's own `fetch_folder_delta` never touches the fake
    client's content-fetch method at all."""
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(1,), uidvalidity=100))
    client.queue_fetch_headers_result(ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_headers_for(1),)))
    adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", bootstrap_timestamp=datetime.now(timezone.utc))
    assert client.fetch_content_calls == []


def test_attachment_metadata_is_always_empty_for_imap_headers_only_discovery():
    client, adapter = _adapter()
    _queue_connect_ok(client)
    client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(1,), uidvalidity=100))
    client.queue_fetch_headers_result(ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_headers_for(1),)))
    page = adapter.fetch_folder_delta(mailbox_id="mb-1", folder="INBOX", bootstrap_timestamp=datetime.now(timezone.utc))
    assert page.messages[0].attachment_metadata == ()
