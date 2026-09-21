"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.gmail_adapter.GmailMailboxAdapter` — folder
(label) discovery, message identity (Gmail's own `id`, used directly),
delta-page pagination/resumability, refresh-on-401 retry-once behaviour,
and refresh-token preservation. All driven by `FakeGmailClient`/
`FakeGmailOAuthClient` — zero real network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.mailbox.gmail.fake_gmail_client import FakeGmailClient, FakeGmailOAuthClient, fake_token_bundle
from services.mailbox.gmail.gmail_adapter import GmailMailboxAdapter, compute_monitored_labels
from services.mailbox.gmail.gmail_client import (
    GmailIdentity,
    GmailIdentityResult,
    GmailLabel,
    GmailLabelListResult,
    GmailMessageListPageResult,
    GmailMessageMetadata,
    GmailMessageMetadataResult,
    GmailOutcomeStatus,
    GmailTokenBundle,
    GmailTokenResult,
)
from services.mailbox.gmail.secrets import InMemoryGmailTokenStore
from services.mailbox.mailbox import InMemoryMailboxSourceRepository, PROVIDER_GOOGLE_GMAIL


def _adapter():
    oauth_client = FakeGmailOAuthClient()
    gmail_client = FakeGmailClient()
    token_store = InMemoryGmailTokenStore()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = GmailMailboxAdapter(oauth_client=oauth_client, gmail_client=gmail_client, token_store=token_store, mailbox_repository=mailbox_repo)
    return oauth_client, gmail_client, token_store, mailbox_repo, adapter


def _seeded_mailbox(mailbox_repo, *, email="mgs241171@gmail.com"):
    return mailbox_repo.create_mailbox(display_name="Personal Gmail", email_address=email, provider_kind=PROVIDER_GOOGLE_GMAIL)


def _fresh_tokens(*, refresh_token="refresh-1"):
    return GmailTokenBundle(access_token="access-1", refresh_token=refresh_token, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))


def _headers_for(*, subject="Invoice", sender="billing@vendor.com", message_id="msg-abc", content_type=None):
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": f"Vendor <{sender}>"},
        {"name": "Message-ID", "value": f"<{message_id}@vendor.com>"},
        {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
    ]
    if content_type:
        headers.append({"name": "Content-Type", "value": content_type})
    return headers


# -- message identity — Gmail's own id, used directly --------------------


def test_message_identity_is_gmails_own_message_id_directly():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(
        message_id="18abcdef1234",
        raw_headers=_headers_for(),
        label_ids=("INBOX",),
        internal_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    summary = _to_message_summary(metadata)
    assert summary.immutable_id == "18abcdef1234"
    assert summary.internet_message_id == "<msg-abc@vendor.com>"
    assert summary.sender_address == "billing@vendor.com"
    assert summary.subject == "Invoice"


def test_message_identity_falls_back_to_internal_date_when_no_usable_date_header():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    internal_date = datetime(2024, 3, 5, 9, 30, tzinfo=timezone.utc)
    metadata = GmailMessageMetadata(message_id="m1", raw_headers=[{"name": "Subject", "value": "No date header"}], label_ids=(), internal_date=internal_date)
    summary = _to_message_summary(metadata)
    assert summary.received_at == internal_date


def test_has_attachments_detected_from_content_type_header():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(
        message_id="m1", raw_headers=_headers_for(content_type="multipart/mixed; boundary=xyz"), label_ids=(), internal_date=None
    )
    assert _to_message_summary(metadata).has_attachments is True


def test_has_attachments_false_without_multipart_mixed():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(content_type="text/plain"), label_ids=(), internal_date=None)
    assert _to_message_summary(metadata).has_attachments is False


# -- folder (label) discovery --------------------------------------------


def test_compute_monitored_labels_selects_only_system_inbox_spam_trash():
    labels = (
        GmailLabel(label_id="INBOX", name="INBOX", label_type="system"),
        GmailLabel(label_id="SPAM", name="SPAM", label_type="system"),
        GmailLabel(label_id="TRASH", name="TRASH", label_type="system"),
        GmailLabel(label_id="SENT", name="SENT", label_type="system"),
        GmailLabel(label_id="DRAFT", name="DRAFT", label_type="system"),
        GmailLabel(label_id="Label_1", name="My Custom Label", label_type="user"),
        GmailLabel(label_id="CATEGORY_PROMOTIONS", name="CATEGORY_PROMOTIONS", label_type="system"),
    )
    monitored = compute_monitored_labels(labels)
    ids = {f.folder_id for f in monitored}
    assert ids == {"INBOX", "SPAM", "TRASH"}
    # Canonical, stable order.
    assert [f.folder_id for f in monitored] == ["INBOX", "SPAM", "TRASH"]


def test_compute_monitored_labels_omits_missing_ones_without_crashing():
    labels = (GmailLabel(label_id="INBOX", name="INBOX", label_type="system"),)
    monitored = compute_monitored_labels(labels)
    assert [f.folder_id for f in monitored] == ["INBOX"]


def test_discovery_config_error_when_credentials_absent():
    oauth_client = FakeGmailOAuthClient(configured=False)
    gmail_client = FakeGmailClient()
    token_store = InMemoryGmailTokenStore()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = GmailMailboxAdapter(oauth_client=oauth_client, gmail_client=gmail_client, token_store=token_store, mailbox_repository=mailbox_repo)
    mailbox = _seeded_mailbox(mailbox_repo)
    result = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert result.status == GmailOutcomeStatus.AUTH_ERROR
    assert gmail_client.labels_calls == 0


def test_discovery_ok_with_real_labels():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    gmail_client.queue_labels_result(
        GmailLabelListResult(
            status=GmailOutcomeStatus.OK,
            labels=(
                GmailLabel(label_id="INBOX", name="INBOX", label_type="system"),
                GmailLabel(label_id="SPAM", name="SPAM", label_type="system"),
                GmailLabel(label_id="TRASH", name="TRASH", label_type="system"),
            ),
        )
    )
    result = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert result.status == GmailOutcomeStatus.OK
    assert [f.folder_id for f in result.folders] == ["INBOX", "SPAM", "TRASH"]


# -- delta pagination / resumability --------------------------------------


def test_bootstrap_round_uses_after_query_and_sets_delta_link():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1", "m2")))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(message_id="a"), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m2", raw_headers=_headers_for(message_id="b"), internal_date=datetime(2024, 1, 3, tzinfo=timezone.utc)))
    )

    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page.status == GmailOutcomeStatus.OK
    assert len(page.messages) == 2
    assert page.messages[0].immutable_id == "m1"
    assert page.delta_link is not None
    assert page.next_link is None
    # Query used `after:` with the bootstrap epoch.
    assert gmail_client.list_messages_calls[0].query.startswith("after:")
    assert gmail_client.list_messages_calls[0].label_id == "INBOX"


def test_pagination_follows_next_page_token_and_threads_running_max():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1",), next_page_token="page-2"))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    first = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert first.status == GmailOutcomeStatus.OK
    assert first.next_link is not None
    assert first.delta_link is None

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m2",)))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m2", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 3, tzinfo=timezone.utc)))
    )
    second = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", next_link=first.next_link)
    assert second.status == GmailOutcomeStatus.OK
    assert len(second.messages) == 1
    assert second.next_link is None
    assert second.delta_link is not None
    # The second page's own list call carried the resumed page_token.
    assert gmail_client.list_messages_calls[-1].page_token == "page-2"


def test_next_round_uses_delta_link_as_new_lower_bound():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1",)))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    first = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))
    second = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", delta_link=first.delta_link)
    assert second.status == GmailOutcomeStatus.OK
    assert second.messages == ()
    # The round-2 query's `after:` epoch derives from round 1's own delta_link — never a bare re-bootstrap from scratch.
    assert gmail_client.list_messages_calls[-1].query is not None


def test_message_vanishing_between_list_and_get_is_skipped_not_a_page_failure():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("gone", "m2")))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.NOT_FOUND, error_detail="vanished"))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m2", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page.status == GmailOutcomeStatus.OK
    assert [m.immutable_id for m in page.messages] == ["m2"]


def test_duplicate_message_ids_within_one_page_are_deduped_before_metadata_fetch():
    """See module docstring's 'Not processing the same message twice per
    pass' section — even if `messages.list` somehow returned the same id
    twice within one page, this adapter fetches metadata for it only
    ONCE."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1", "m1", "m2")))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m2", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page.status == GmailOutcomeStatus.OK
    assert len(gmail_client.metadata_calls) == 2  # never 3 — the duplicate m1 was never re-fetched
    assert gmail_client.metadata_calls == ["m1", "m2"]


def test_fetch_folder_delta_never_calls_fetch_message_raw():
    """Stage-A discovery is headers-only — proves the adapter's own
    `fetch_folder_delta` never touches the fake client's raw-content
    method at all."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1",)))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert gmail_client.raw_calls == []


def test_attachment_metadata_is_always_empty_for_gmail_metadata_only_discovery():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1",)))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(), internal_date=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    )
    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page.messages[0].attachment_metadata == ()


# -- refresh-on-401 retry-once behaviour ----------------------------------


def test_fetch_folder_delta_reactive_refreshes_once_on_401_then_succeeds():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="stale-token", refresh_token="refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=_fresh_tokens()))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))

    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page.status == GmailOutcomeStatus.OK
    assert len(gmail_client.list_messages_calls) == 2  # exactly one retry, never more
    assert len(oauth_client.token_calls) == 1
    assert oauth_client.token_calls[0].kind == "refresh"


def test_fetch_folder_delta_never_retries_more_than_once():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="stale-token", refresh_token="refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=_fresh_tokens()))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="still 401"))

    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page.status == GmailOutcomeStatus.AUTH_ERROR
    assert len(gmail_client.list_messages_calls) == 2
    assert len(oauth_client.token_calls) == 1  # never a second refresh attempt


def test_reactive_refresh_failure_marks_mailbox_auth_required():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="stale", refresh_token="refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    mailbox_repo.begin_microsoft_connect(mailbox.mailbox_id)
    mailbox_repo.mark_microsoft_connected(mailbox.mailbox_id)

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.AUTH_ERROR))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="revoked"))

    adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    updated = mailbox_repo.get_mailbox(mailbox.mailbox_id)
    assert updated.connection_state == "AUTH_REQUIRED"


def test_preemptive_refresh_happens_before_first_request_when_near_expiry():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="near-expiry", refresh_token="refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(seconds=10))

    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=_fresh_tokens(refresh_token="rotated-refresh")))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))

    adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert len(oauth_client.token_calls) == 1
    assert oauth_client.token_calls[0].kind == "refresh"


# -- refresh-token preservation (Google's own real difference) -----------


def test_refresh_response_omitting_refresh_token_preserves_the_stored_one():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="near-expiry", refresh_token="original-refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(seconds=10))

    # Google's own real refresh response usually carries NO refresh_token.
    refreshed_bundle = GmailTokenBundle(access_token="new-access", refresh_token=None, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=refreshed_bundle))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))

    adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))

    stored = token_store.read(mailbox.mailbox_id)
    assert stored.access_token == "new-access"
    assert stored.refresh_token == "original-refresh-token"  # NEVER overwritten with None


def test_refresh_response_carrying_a_new_refresh_token_rotates_it():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="near-expiry", refresh_token="old-refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(seconds=10))

    refreshed_bundle = GmailTokenBundle(access_token="new-access", refresh_token="brand-new-refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=refreshed_bundle))
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))

    adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stored = token_store.read(mailbox.mailbox_id)
    assert stored.refresh_token == "brand-new-refresh-token"


# -- cross-mailbox isolation ------------------------------------------------


def test_two_gmail_mailboxes_have_fully_independent_token_state():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox_a = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")
    mailbox_b = _seeded_mailbox(mailbox_repo, email="matt.george.scott@gmail.com")
    token_store.write(mailbox_a.mailbox_id, access_token="a-access", refresh_token="a-refresh", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    token_store.write(mailbox_b.mailbox_id, access_token="b-access", refresh_token="b-refresh", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    assert token_store.read(mailbox_a.mailbox_id).access_token == "a-access"
    assert token_store.read(mailbox_b.mailbox_id).access_token == "b-access"

    # Revoking/failing mailbox A's connection never touches mailbox B's.
    mailbox_repo.begin_microsoft_connect(mailbox_a.mailbox_id)
    mailbox_repo.mark_microsoft_connected(mailbox_a.mailbox_id)
    mailbox_repo.begin_microsoft_connect(mailbox_b.mailbox_id)
    mailbox_repo.mark_microsoft_connected(mailbox_b.mailbox_id)

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.AUTH_ERROR))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="revoked"))
    adapter.fetch_folder_delta(mailbox_id=mailbox_a.mailbox_id, folder="INBOX", bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))

    assert mailbox_repo.get_mailbox(mailbox_a.mailbox_id).connection_state == "AUTH_REQUIRED"
    assert mailbox_repo.get_mailbox(mailbox_b.mailbox_id).connection_state == "CONNECTED"


# -- OAuth callback identity verification ---------------------------------


def test_exchange_code_and_verify_identity_succeeds_and_persists_tokens():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")
    mailbox_repo.begin_microsoft_connect(mailbox.mailbox_id)  # NOT_CONFIGURED -> AUTH_REQUIRED (mirrors the real connect endpoint)

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="mgs241171@gmail.com")))

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is True
    assert token_store.read(mailbox.mailbox_id) is not None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state == "CONNECTED"


def test_exchange_code_and_verify_identity_calls_profile_then_writes_token_then_connects():
    """Proves the ordering the architect spec requires: the profile
    lookup happens, matches, THEN the token is persisted, THEN the
    mailbox is marked CONNECTED — never any of those before a genuine
    positive identity match. `gmail_client.profile_calls` proves exactly
    one profile lookup happened; the token-store/connection-state
    assertions (only reachable this way given `FakeGmailClient`'s own
    "queued exactly once, popped exactly once" discipline) prove it
    happened before the write/CONNECTED side effects."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")
    mailbox_repo.begin_microsoft_connect(mailbox.mailbox_id)

    assert gmail_client.profile_calls == 0
    assert token_store.read(mailbox.mailbox_id) is None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state != "CONNECTED"

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="mgs241171@gmail.com")))

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is True
    assert gmail_client.profile_calls == 1  # the profile call genuinely happened
    assert token_store.read(mailbox.mailbox_id) is not None  # ... then the token was written
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state == "CONNECTED"  # ... then CONNECTED


def test_exchange_code_and_verify_identity_rejects_a_wrong_account():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="someone.else@gmail.com")))

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is False
    assert outcome.reason == "wrong_account"
    assert token_store.read(mailbox.mailbox_id) is None  # never persisted on a mismatch
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state == "NOT_CONFIGURED"


def test_exchange_code_and_verify_identity_fails_safely_on_profile_401():
    """Gmail's `users.getProfile` returning 401 (AUTH_ERROR) must fail
    exactly like any other identity-lookup failure: no token persisted,
    mailbox never CONNECTED."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="HTTP 401: unauthorized"))

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is False
    assert outcome.reason == "identity_lookup_failed"
    assert outcome.provider_operation == "users.getProfile"
    assert outcome.provider_status == GmailOutcomeStatus.AUTH_ERROR.value
    assert token_store.read(mailbox.mailbox_id) is None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state != "CONNECTED"


def test_exchange_code_and_verify_identity_fails_safely_on_profile_403():
    """A genuine permission error (PERMISSION_ERROR) must fail exactly
    like any other identity-lookup failure — no token persisted, never
    CONNECTED."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.PERMISSION_ERROR, error_detail="HTTP 403: forbidden"))

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is False
    assert outcome.reason == "identity_lookup_failed"
    assert outcome.provider_operation == "users.getProfile"
    assert outcome.provider_status == GmailOutcomeStatus.PERMISSION_ERROR.value
    assert token_store.read(mailbox.mailbox_id) is None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state != "CONNECTED"


def test_exchange_code_and_verify_identity_fails_safely_on_malformed_profile_response():
    """A malformed/unparseable `users.getProfile` body (MALFORMED_RESPONSE)
    must fail exactly like any other identity-lookup failure."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle()))
    gmail_client.queue_profile_result(
        GmailIdentityResult(status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail="could not parse users.getProfile response")
    )

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is False
    assert outcome.reason == "identity_lookup_failed"
    assert outcome.provider_operation == "users.getProfile"
    assert outcome.provider_status == GmailOutcomeStatus.MALFORMED_RESPONSE.value
    assert token_store.read(mailbox.mailbox_id) is None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state != "CONNECTED"


def test_exchange_code_and_verify_identity_token_exchange_failure_carries_bounded_diagnostics():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="HTTP 400: invalid_grant"))

    outcome = adapter.exchange_code_and_verify_identity(
        mailbox_id=mailbox.mailbox_id, expected_email_address=mailbox.email_address, code="abc", redirect_uri="https://localhost:8543/cb"
    )
    assert outcome.ok is False
    assert outcome.reason == "token_exchange_failed"
    assert outcome.provider_operation == "token_exchange"
    assert outcome.provider_status == GmailOutcomeStatus.AUTH_ERROR.value
    assert token_store.read(mailbox.mailbox_id) is None
    assert mailbox_repo.get_mailbox(mailbox.mailbox_id).connection_state != "CONNECTED"
    assert gmail_client.profile_calls == 0  # never reached — token exchange failed first


def test_exchange_code_and_verify_identity_routes_tokens_to_the_right_mailbox_of_two_in_flight():
    """The core WO-named proof at the adapter layer: two independent
    Gmail mailboxes' own callback completions never cross-contaminate
    token storage."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox_a = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")
    mailbox_b = _seeded_mailbox(mailbox_repo, email="matt.george.scott@gmail.com")
    mailbox_repo.begin_microsoft_connect(mailbox_a.mailbox_id)
    mailbox_repo.begin_microsoft_connect(mailbox_b.mailbox_id)

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle(access_token="token-for-a")))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="mgs241171@gmail.com")))
    adapter.exchange_code_and_verify_identity(mailbox_id=mailbox_a.mailbox_id, expected_email_address=mailbox_a.email_address, code="code-a", redirect_uri="https://localhost:8543/cb")

    oauth_client.queue_exchange_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=fake_token_bundle(access_token="token-for-b")))
    gmail_client.queue_profile_result(GmailIdentityResult(status=GmailOutcomeStatus.OK, identity=GmailIdentity(email="matt.george.scott@gmail.com")))
    adapter.exchange_code_and_verify_identity(mailbox_id=mailbox_b.mailbox_id, expected_email_address=mailbox_b.email_address, code="code-b", redirect_uri="https://localhost:8543/cb")

    assert token_store.read(mailbox_a.mailbox_id).access_token == "token-for-a"
    assert token_store.read(mailbox_b.mailbox_id).access_token == "token-for-b"
