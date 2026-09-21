"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.gmail_adapter.GmailMailboxAdapter` — folder
(label) discovery, message identity (Gmail's own `id`, used directly),
delta-page pagination/resumability, refresh-on-401 retry-once behaviour,
and refresh-token preservation. All driven by `FakeGmailClient`/
`FakeGmailOAuthClient` — zero real network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest

from services.mailbox.gmail.fake_gmail_client import FakeGmailClient, FakeGmailOAuthClient, fake_token_bundle
from services.mailbox.gmail.gmail_adapter import (
    GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME,
    GMAIL_ALL_RECEIVED_STREAM_ID,
    GmailMailboxAdapter,
    _decode_delta_link,
)
from services.mailbox.gmail.gmail_client import (
    GmailIdentity,
    GmailIdentityResult,
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


def _headers_for(*, subject="Invoice", sender="billing@vendor.com", message_id="msg-abc", content_type=None, content_disposition=None, content_type_header_name="Content-Type", content_disposition_header_name="Content-Disposition"):
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": f"Vendor <{sender}>"},
        {"name": "Message-ID", "value": f"<{message_id}@vendor.com>"},
        {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
    ]
    if content_type:
        headers.append({"name": content_type_header_name, "value": content_type})
    if content_disposition:
        headers.append({"name": content_disposition_header_name, "value": content_disposition})
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


# -- Defect A: non-UTC `Date`-header offsets must normalize to UTC ------
#
# Root cause (PL-reproduced against real production data via disposable
# Postgres, not a guess): `parsedate_to_datetime` correctly parses a
# real-world non-UTC `Date` header into a timezone-AWARE datetime whose
# `tzinfo` is already set — to that NON-UTC offset. The old code took
# the `parsed if parsed.tzinfo else ...` branch and used it AS-IS,
# never normalizing to UTC, producing a non-None, non-UTC `received_at`
# that later crashed `core.timestamps.ensure_utc` deep inside
# `MailboxMessage.to_dict()`. The fix (`.astimezone(timezone.utc)`)
# completes the already-present normalization step losslessly.


def test_non_utc_date_header_offset_normalizes_to_utc_same_instant():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    date_header = "Mon, 21 Sep 2026 16:10:52 -0400 (EDT)"
    # The shared `_headers_for` helper always uses a UTC `+0000` Date —
    # this test needs a non-UTC offset, so swap it out explicitly.
    headers = [h for h in _headers_for() if h["name"] != "Date"]
    headers.append({"name": "Date", "value": date_header})
    metadata = GmailMessageMetadata(message_id="m1", raw_headers=headers, label_ids=(), internal_date=datetime(2026, 9, 21, tzinfo=timezone.utc))

    summary = _to_message_summary(metadata)

    assert summary.received_at is not None
    assert summary.received_at.utcoffset() is not None
    assert summary.received_at.utcoffset().total_seconds() == 0
    expected = parsedate_to_datetime(date_header).astimezone(timezone.utc)
    assert summary.received_at == expected
    # Correctness of the CONVERSION, not merely "some UTC value" — the
    # exact same instant in time, expressed in UTC.
    assert summary.received_at == datetime(2026, 9, 21, 20, 10, 52, tzinfo=timezone.utc)


def test_utc_date_header_is_unchanged_regression():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    date_header = "Mon, 21 Sep 2026 16:10:52 +0000"
    headers = [h for h in _headers_for() if h["name"] != "Date"]
    headers.append({"name": "Date", "value": date_header})
    metadata = GmailMessageMetadata(message_id="m1", raw_headers=headers, label_ids=(), internal_date=datetime(2026, 9, 21, tzinfo=timezone.utc))

    summary = _to_message_summary(metadata)

    assert summary.received_at.utcoffset().total_seconds() == 0
    assert summary.received_at == parsedate_to_datetime(date_header).astimezone(timezone.utc)
    assert summary.received_at == datetime(2026, 9, 21, 16, 10, 52, tzinfo=timezone.utc)


def test_missing_date_header_still_falls_back_to_internal_date_regression():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    internal_date = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)
    headers = [h for h in _headers_for() if h["name"] != "Date"]
    metadata = GmailMessageMetadata(message_id="m1", raw_headers=headers, label_ids=(), internal_date=internal_date)

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


# -- `has_attachments` derivation broadening (CD-6 discovery-signal fix) --
#
# `_derive_has_attachments` extends the original `multipart/mixed`-only
# check with two more bounded, metadata-only signals: an exact/prefix
# match on an attachment-shaped top-level `Content-Type`, and a
# top-level `Content-Disposition: attachment`. See that function's own
# docstring for the full, honest scope of what this is (and is not).


def test_has_attachments_true_for_top_level_pdf_content_type_no_multipart():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(content_type="application/pdf"), label_ids=(), internal_date=None)
    assert _to_message_summary(metadata).has_attachments is True


def test_has_attachments_true_for_pdf_content_type_with_trailing_parameters():
    """Proves the exact/prefix match tolerates trailing `; name=...`-
    style parameters rather than requiring an exact full-string match."""
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(
        message_id="m1", raw_headers=_headers_for(content_type='application/pdf; name="invoice.pdf"'), label_ids=(), internal_date=None
    )
    assert _to_message_summary(metadata).has_attachments is True


def test_has_attachments_true_for_content_disposition_attachment():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(
        message_id="m1",
        raw_headers=_headers_for(content_type="text/plain", content_disposition='attachment; filename="x.pdf"'),
        label_ids=(),
        internal_date=None,
    )
    assert _to_message_summary(metadata).has_attachments is True


def test_has_attachments_false_for_plain_text_no_disposition():
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(message_id="m1", raw_headers=_headers_for(content_type="text/plain"), label_ids=(), internal_date=None)
    assert _to_message_summary(metadata).has_attachments is False


def test_has_attachments_derivation_is_case_insensitive():
    """Mixed-case header NAME and VALUE for both `Content-Type` and
    `Content-Disposition` must still be recognised."""
    from services.mailbox.gmail.gmail_adapter import _to_message_summary

    metadata = GmailMessageMetadata(
        message_id="m1",
        raw_headers=_headers_for(
            content_type="MULTIPART/MIXED; boundary=xyz",
            content_type_header_name="content-type",
            content_disposition="ATTACHMENT",
            content_disposition_header_name="Content-Disposition",
        ),
        label_ids=(),
        internal_date=None,
    )
    assert _to_message_summary(metadata).has_attachments is True


# -- folder discovery: single ALL_RECEIVED synthetic stream --------------


def test_discovery_returns_exactly_one_all_received_stream():
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    result = adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert result.status == GmailOutcomeStatus.OK
    assert [f.folder_id for f in result.folders] == [GMAIL_ALL_RECEIVED_STREAM_ID]
    assert [f.display_name for f in result.folders] == [GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME]
    assert GMAIL_ALL_RECEIVED_STREAM_ID == "ALL_RECEIVED"
    assert GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME == "All received mail"


def test_discovery_never_calls_list_labels():
    """See module docstring's 'Label/folder normalisation' section —
    the monitored-folder list is now a fixed constant; there is nothing
    left to discover FROM real Gmail labels for this purpose, so
    `list_labels()` must never be called by discovery any more."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    adapter.discover_monitored_folders(mailbox_id=mailbox.mailbox_id)
    assert gmail_client.labels_calls == 0


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
    # `folder` ("INBOX" here) is NEVER forwarded to `list_messages` as a
    # real `labelIds` value — the ALL_RECEIVED stream always queries with
    # no label restriction and `include_spam_trash=True` (see module
    # docstring's "Label/folder normalisation" section).
    assert gmail_client.list_messages_calls[0].label_id is None
    assert gmail_client.list_messages_calls[0].include_spam_trash is True


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


# ---------------------------------------------------------------------
# ALL_RECEIVED stream — inclusion/exclusion by real Gmail `labelIds`,
# and the critical watermark-then-exclude ordering (architect ruling,
# module docstring's "Label/folder normalisation" section, points 4/5).
# ---------------------------------------------------------------------


def _msg_metadata(message_id: str, *, label_ids=(), internal_date=None) -> GmailMessageMetadata:
    return GmailMessageMetadata(
        message_id=message_id,
        raw_headers=_headers_for(message_id=message_id),
        label_ids=tuple(label_ids),
        internal_date=internal_date or datetime(2024, 1, 2, tzinfo=timezone.utc),
    )


def _single_message_page(gmail_client, adapter, mailbox, *, message_id="m1", label_ids=()):
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=(message_id,)))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata(message_id, label_ids=label_ids)))
    return adapter.fetch_folder_delta(
        mailbox_id=mailbox.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )


@pytest.mark.parametrize(
    "label_ids",
    [
        ("INBOX",),  # 1. Inbox received message.
        (),  # 2. Archived (no labels at all is also covered by test 11, but this is the "no monitored labels" archived case).
        ("UNREAD",),  # 2b. Archived, carrying only a non-outbound, non-system label.
        ("Label_123",),  # 3. User-labelled archived message.
        ("SPAM",),  # 4. Spam.
        ("TRASH",),  # 5. Trash.
        ("CATEGORY_UPDATES",),  # 10. Category-labelled inbound message.
    ],
)
def test_all_received_stream_includes_every_non_outbound_disposition(label_ids):
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    page = _single_message_page(gmail_client, adapter, mailbox, label_ids=label_ids)
    assert page.status == GmailOutcomeStatus.OK
    assert [m.immutable_id for m in page.messages] == ["m1"]


def test_all_received_stream_includes_message_with_no_labels_at_all():
    """11. `label_ids = ()` — no explicit outbound-exclusion signal
    present -> included."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    page = _single_message_page(gmail_client, adapter, mailbox, label_ids=())
    assert page.status == GmailOutcomeStatus.OK
    assert [m.immutable_id for m in page.messages] == ["m1"]


@pytest.mark.parametrize(
    "label_ids",
    [
        ("SENT",),  # 6. Sent.
        ("DRAFT",),  # 7. Draft.
        ("SENT", "INBOX"),  # 8. SENT + INBOX together -> excluded (SENT wins even with INBOX present).
        ("SENT", "TRASH"),  # 9. SENT + TRASH together -> excluded.
        ("DRAFT", "INBOX"),
    ],
)
def test_all_received_stream_excludes_outbound_messages_regardless_of_other_labels(label_ids):
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    page = _single_message_page(gmail_client, adapter, mailbox, label_ids=label_ids)
    assert page.status == GmailOutcomeStatus.OK
    assert page.messages == ()


def test_watermark_advances_past_an_excluded_sent_message_even_though_it_is_never_returned():
    """16. The critical watermark test (architect's own explicit ordering
    requirement). A page whose NEWEST message (highest `internalDate`) is
    SENT: (a) it is excluded from the returned summaries; (b) it
    NEVERTHELESS advances `running_max`/the resulting `delta_link`'s
    `since_epoch`; (c) the final cursor genuinely advances beyond that
    excluded message's timestamp, not stuck at the last INCLUDED
    message's earlier timestamp — asserted on the actual decoded cursor
    value, never merely 'some cursor was returned'."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    included_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
    excluded_sent_at = datetime(2024, 1, 5, tzinfo=timezone.utc)  # the NEWEST message in the page.

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m-included", "m-sent")))
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-included", label_ids=("INBOX",), internal_date=included_at))
    )
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-sent", label_ids=("SENT",), internal_date=excluded_sent_at))
    )

    page = adapter.fetch_folder_delta(
        mailbox_id=mailbox.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    assert page.status == GmailOutcomeStatus.OK
    # (a) excluded from the returned summaries.
    assert [m.immutable_id for m in page.messages] == ["m-included"]
    # (b) + (c) the cursor genuinely advanced past the EXCLUDED message's
    # own timestamp (minus the documented one-second safety margin),
    # never merely up to the last INCLUDED message's earlier timestamp.
    assert page.delta_link is not None
    decoded_since_epoch = _decode_delta_link(page.delta_link)
    assert decoded_since_epoch == int(excluded_sent_at.timestamp()) - 1
    assert decoded_since_epoch > int(included_at.timestamp())


def test_mixed_page_only_received_family_messages_returned_within_one_stream():
    """17. One page containing an Inbox-received message, an archived
    message, a Spam message, a Sent message, and a Draft message
    together -> only the three received-family messages appear in the
    returned summaries, within ONE stream/cursor (no duplicate
    enumeration, no separate per-label rounds — only one `list_messages`
    call total)."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    message_ids = ("m-inbox", "m-archived", "m-spam", "m-sent", "m-draft")
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=message_ids))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-inbox", label_ids=("INBOX",))))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-archived", label_ids=())))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-spam", label_ids=("SPAM",))))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-sent", label_ids=("SENT",))))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m-draft", label_ids=("DRAFT",))))

    page = adapter.fetch_folder_delta(
        mailbox_id=mailbox.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc)
    )
    assert page.status == GmailOutcomeStatus.OK
    assert {m.immutable_id for m in page.messages} == {"m-inbox", "m-archived", "m-spam"}
    # Exactly ONE `list_messages` call — one stream/cursor, never a
    # separate per-label round.
    assert len(gmail_client.list_messages_calls) == 1


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


# ---------------------------------------------------------------------
# CD-6 metadata-contract fix — real production-shaped path proof:
# `DEFAULT_METADATA_HEADERS` (now including `Received`) -> real, fake-
# client-backed `fetch_message_headers()` -> provider-neutral
# `evaluate_message_authentication()` -> `assess_gmail_authentication()`.
# `fetch_message_headers()` is the exact call `services/mailbox/sweep
# .py::_reprocess_one_message` (the historical-candidate refresh path)
# uses, and mirrors the metadata-fetch shape every ordinary live sweep
# round's own per-message headers fetch performs — the more direct real
# entry point for this proof than `fetch_folder_delta()`, since it takes
# no `metadata_headers` argument at all: callers can never override it,
# so this exercises the REAL default exactly as production does.
# ---------------------------------------------------------------------


def _genuine_trusted_gmail_headers(*, domain: str = "trusted-vendor.example", dmarc: str = "pass") -> list[dict]:
    """A sanitized, `.example`-domain/RFC-5737-IP genuine Gmail header
    shape — mirrors `tests/integration/test_mailbox_gmail_authentication
    .py`'s own `_normal_sample_headers()` structural shape (a `Received
    ... by mx.google.com ...` hop immediately preceding a genuine
    `Authentication-Results: mx.google.com; ...` header, plus the
    genuine ARC triple) — never real captured data."""
    return [
        {
            "name": "Received",
            "value": (
                f"from mail.{domain} (mail.{domain}. [203.0.113.10]) "
                "by mx.google.com with ESMTPS id ab1cd23ef456.2026.09.21.00.00.00 "
                "for <mgs241171@example.invalid>; Mon, 21 Sep 2026 00:00:00 -0700 (PDT)"
            ),
        },
        {
            "name": "Authentication-Results",
            "value": (
                f"mx.google.com; dkim=pass header.i=@{domain} header.s=selector1 header.b=redacted; "
                f"spf=pass (google.com: domain of billing@{domain} designates 203.0.113.10 as permitted "
                f"sender) smtp.mailfrom=billing@{domain}; dmarc={dmarc} header.from={domain}"
            ),
        },
        {
            "name": "ARC-Seal",
            "value": "i=1; a=rsa-sha256; t=1758412800; cv=none; d=google.com; s=arc-20160816; b=REDACTEDGENUINESIGNATURE==",
        },
        {
            "name": "ARC-Message-Signature",
            "value": (
                "i=1; a=rsa-sha256; c=relaxed/relaxed; d=google.com; s=arc-20160816; "
                "h=from:to:subject:date:message-id; bh=REDACTED=; b=REDACTEDGENUINE=="
            ),
        },
        {
            "name": "ARC-Authentication-Results",
            "value": (
                f"i=1; mx.google.com; dkim=pass header.i=@{domain} header.s=selector1 header.b=redacted; "
                f"spf=pass smtp.mailfrom=billing@{domain}; dmarc={dmarc} header.from={domain}"
            ),
        },
    ]


def test_default_metadata_headers_production_path_reaches_pass_via_fetch_message_headers():
    """CD-6 metadata-contract fix regression proof: `DEFAULT_METADATA_HEADERS`
    used to omit `Received` entirely, so this exact real production call
    chain (`GmailMailboxAdapter.fetch_message_headers()` -> provider-
    neutral `evaluate_message_authentication()` ->
    `assess_gmail_authentication()`) could never reach a decisive verdict
    for a genuinely authenticated message — it always fell back to
    UNKNOWN, forcing real, legitimate mail into SECURITY_REVIEW. This
    drives the REAL, unmodified call (no `metadata_headers` override —
    the exact shape `_reprocess_one_message`/every live sweep round's own
    per-message headers fetch uses) against a fake Gmail response
    simulating exactly what Gmail's own API returns for that requested
    header set, and proves it now resolves to PASS for a realistic,
    sanitized, genuinely-authenticated message — with header order
    preserved exactly, never sorted/reordered anywhere in this chain."""
    from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_PASS
    from services.mailbox.gmail.gmail_client import DEFAULT_METADATA_HEADERS
    from services.mailbox.sweep import evaluate_message_authentication

    # The metadata-contract fix itself: `Received` is now requested.
    assert "Received" in DEFAULT_METADATA_HEADERS

    oauth_client, gmail_client, token_store, mailbox_repo, adapter = _adapter()
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    metadata_headers = _headers_for(sender="billing@trusted-vendor.example") + _genuine_trusted_gmail_headers()
    gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=GmailMessageMetadata(
                message_id="m1", raw_headers=metadata_headers, internal_date=datetime(2026, 9, 21, tzinfo=timezone.utc)
            ),
        )
    )

    # The real production call — NO `metadata_headers` override, exactly
    # as `_reprocess_one_message`/every live sweep round's per-message
    # headers-only fetch calls it.
    headers_result = adapter.fetch_message_headers(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")
    assert headers_result.status == GmailOutcomeStatus.OK
    # Header ORDER preserved exactly as Gmail returned it — never sorted
    # or reordered anywhere in this call chain (see gmail_client.py's own
    # docstring, and services.mailbox.gmail.authentication's own
    # "Evidence chronology" step 3 for why this order is load-bearing).
    assert list(headers_result.raw_headers) == metadata_headers

    assessment = evaluate_message_authentication(headers_result.raw_headers, provider_kind=PROVIDER_GOOGLE_GMAIL)
    assert assessment.verdict == AUTH_ASSESSMENT_PASS
    assert assessment.evidence["eligible_candidate_count"] == 1
    assert assessment.evidence["selected_header_tokens"]["dmarc"] == "pass"
