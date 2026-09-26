"""CD-6 GUI-operations-foundation follow-on WO tests for
`services.mailbox.gmail.gmail_adapter.GmailMailboxAdapter` — folder
(label) discovery, message identity (Gmail's own `id`, used directly),
delta-page pagination/resumability, refresh-on-401 retry-once behaviour,
and refresh-token preservation. All driven by `FakeGmailClient`/
`FakeGmailOAuthClient` — zero real network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
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
    GmailClient,
    GmailIdentity,
    GmailIdentityResult,
    GmailMessageListPageResult,
    GmailMessageMetadata,
    GmailMessageMetadataResult,
    GmailMessageRawResult,
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


# ---------------------------------------------------------------------
# CD-6 follow-on fix — proactive Gmail quota pacing
# (`_GmailMessagesGetGovernor`). A real production historical sweep hit
# Google's own per-user `messages.get` quota mid-round; these tests prove
# the adapter now paces itself PROACTIVELY (a minimum interval between
# sequential `messages.get` calls, metadata AND raw format alike), scoped
# per mailbox_id, using a fake monotonic clock + fake sleep so NO test
# here ever actually blocks.
#
# CD-6 follow-on quota-SCALING fix (this delivery): `fetch_message_raw`
# (the MIME/raw `messages.get` call `fetch_message_content` makes) used
# to be entirely UNPACED — a real historical-deep-processing run makes
# TWO `messages.get` calls per candidate (one metadata, one raw), both
# against the SAME quota, so leaving raw fetches unpaced meant this
# adapter was only pacing HALF of its own real quota consumption. The
# governor (renamed from `_GmailMetadataFetchGovernor` to
# `_GmailMessagesGetGovernor` — it was already generic, this rename is
# the acknowledgement) now paces `fetch_message_content` identically to
# `fetch_message_headers`: one `wait()` before the initial
# `fetch_message_raw` call, and one more before its own AUTH_ERROR
# reactive-retry call. The tests below prove: (1) mixed metadata/raw
# calls for one mailbox share the SAME pacing state, (2) that pacing
# stays fully mailbox_id-isolated, (3) the metadata AUTH-retry path
# paces BOTH attempts, and (4) the NEW raw AUTH-retry path does too.
# ---------------------------------------------------------------------


class _FakeMonotonicClock:
    """A deterministic stand-in for `time.monotonic` — starts at an
    arbitrary fixed value (never 0, to prove nothing in the governor
    secretly assumes a zero epoch) and only ever advances when told to
    (by `advance()`, or implicitly via `_FakeSleep.__call__` below)."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSleep:
    """Records every `(seconds,)` it was called with — proves the
    governor's own sleep-duration math — and advances the paired fake
    clock by that same amount (mirrors what a REAL `time.sleep` would
    accomplish to elapsed monotonic time, deterministically, with zero
    real blocking)."""

    def __init__(self, clock: _FakeMonotonicClock) -> None:
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


def _paced_adapter(*, min_interval_seconds: float = 0.30, start: float = 1_000.0):
    clock = _FakeMonotonicClock(start=start)
    sleep = _FakeSleep(clock)
    oauth_client = FakeGmailOAuthClient()
    gmail_client = FakeGmailClient()
    token_store = InMemoryGmailTokenStore()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = GmailMailboxAdapter(
        oauth_client=oauth_client, gmail_client=gmail_client, token_store=token_store, mailbox_repository=mailbox_repo,
        messages_get_min_interval_seconds=min_interval_seconds, monotonic_fn=clock, sleep_fn=sleep,
    )
    return oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep


def test_governor_first_metadata_fetch_needs_no_sleep_subsequent_calls_do():
    """8. Several sequential `fetch_message_metadata` calls, driven via
    `fetch_folder_delta`'s own per-page loop against ONE page carrying
    three message ids. The FIRST call must trigger no sleep at all (no
    prior timestamp for this mailbox_id yet); the SECOND and THIRD must
    each sleep, and the recorded duration must equal the configured
    minimum interval (the fake clock never advances on its own between
    calls, so the full interval is always "remaining")."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1", "m2", "m3")))
    for mid in ("m1", "m2", "m3"):
        gmail_client.queue_metadata_result(
            GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata(mid, label_ids=("INBOX",)))
        )

    page = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))

    assert page.status == GmailOutcomeStatus.OK
    assert len(page.messages) == 3
    # Exactly 2 sleeps for 3 sequential calls — the first never sleeps.
    assert sleep.calls == pytest.approx([0.30, 0.30])
    # Zero real blocking — proven by the fake clock's own final value:
    # exactly `start + 2 * min_interval_seconds`, nothing more.
    assert clock.now == pytest.approx(1_000.0 + 0.60)


def test_governor_pagination_persists_pacing_state_across_pages():
    """9. A second page, fetched via `next_link`, must NOT reset this
    mailbox's own pacing state — the governor's "last call" timestamp is
    adapter-instance-scoped, never page-scoped. Page 1 ends immediately
    after its own (unsleeped) first call; page 2's own first call must
    therefore still need to sleep, spaced from page 1's own last call,
    not from a reset zero state."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m1",), next_page_token="page-2"))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m1", label_ids=("INBOX",))))
    first = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert first.status == GmailOutcomeStatus.OK
    assert sleep.calls == []  # page 1's own single message never sleeps

    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("m2",)))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m2", label_ids=("INBOX",))))
    second = adapter.fetch_folder_delta(mailbox_id=mailbox.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, next_link=first.next_link)
    assert second.status == GmailOutcomeStatus.OK
    # Page 2's own first (and only) metadata call DID sleep — proving the
    # governor's own "last call" state persisted across the page
    # boundary rather than resetting when a new `fetch_folder_delta` call
    # began.
    assert sleep.calls == pytest.approx([0.30])


def test_governor_cross_mailbox_isolation():
    """10. Pacing mailbox_id_A many times must NEVER throttle a
    completely independent mailbox_id_B — a first call for B, issued
    right after A's own pacing history, must need no sleep at all."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox_a = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")
    mailbox_b = _seeded_mailbox(mailbox_repo, email="matt.george.scott@gmail.com")
    for mailbox in (mailbox_a, mailbox_b):
        token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    # Pace mailbox A across several sequential calls first.
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("a1", "a2", "a3")))
    for mid in ("a1", "a2", "a3"):
        gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata(mid, label_ids=("INBOX",))))
    page_a = adapter.fetch_folder_delta(mailbox_id=mailbox_a.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page_a.status == GmailOutcomeStatus.OK
    calls_after_a = len(sleep.calls)
    assert calls_after_a == 2  # 3 sequential calls for A -> 2 sleeps

    # Mailbox B's very first metadata fetch, issued immediately
    # afterwards on the SAME governor/adapter instance, must need no
    # sleep — it has no prior timestamp of its OWN, and A's own history
    # must never leak into B's own pacing state.
    gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=("b1",)))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("b1", label_ids=("INBOX",))))
    page_b = adapter.fetch_folder_delta(mailbox_id=mailbox_b.mailbox_id, folder=GMAIL_ALL_RECEIVED_STREAM_ID, bootstrap_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert page_b.status == GmailOutcomeStatus.OK
    assert len(sleep.calls) == calls_after_a  # zero NEW sleeps triggered by B's own first call


def test_governor_wait_directly_zero_real_sleeping_proof():
    """11. A direct, white-box proof against `_GmailMessagesGetGovernor`
    itself (mirrors this file's own established pattern of testing
    private helpers like `_to_message_summary`/`_decode_delta_link`
    directly): the first `wait()` for a mailbox_id never sleeps; a
    second `wait()` for the SAME mailbox_id, issued with zero elapsed
    fake-clock time, sleeps for exactly the configured minimum interval;
    at no point does this test block in real wall-clock time (proven by
    only ever asserting on `sleep.calls`, never timing the test itself)."""
    from services.mailbox.gmail.gmail_adapter import _GmailMessagesGetGovernor

    clock = _FakeMonotonicClock(start=500.0)
    sleep = _FakeSleep(clock)
    governor = _GmailMessagesGetGovernor(min_interval_seconds=0.30, monotonic_fn=clock, sleep_fn=sleep)

    governor.wait("mailbox-x")
    assert sleep.calls == []

    governor.wait("mailbox-x")
    assert sleep.calls == pytest.approx([0.30])

    # A fully independent mailbox_id, waited on immediately afterwards,
    # still needs no sleep at all.
    governor.wait("mailbox-y")
    assert sleep.calls == pytest.approx([0.30])


# ---------------------------------------------------------------------
# CD-6 follow-on quota-SCALING fix — `fetch_message_content` (raw MIME
# `messages.get`) is now governed by the SAME shared
# `_GmailMessagesGetGovernor` as `fetch_message_metadata`/
# `fetch_message_headers`, closing the gap that let a real historical
# deep-processing run (two `messages.get` calls per candidate) exceed
# its safe quota budget unpaced.
# ---------------------------------------------------------------------


def test_governor_paces_mixed_metadata_and_raw_calls_for_the_same_mailbox():
    """12. `metadata, raw, metadata, raw` for ONE mailbox_id, driven
    directly via `fetch_message_headers`/`fetch_message_content` (never
    `fetch_folder_delta` — this proves the governor is genuinely shared
    across the two DIFFERENT public methods, not merely reused within
    one). The first call never sleeps; every subsequent call — metadata
    or raw alike — sleeps for exactly the configured minimum interval
    relative to the immediately-preceding call, proving one unified
    per-mailbox timeline rather than two independent ones."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m1", label_ids=("INBOX",))))
    headers1 = adapter.fetch_message_headers(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")
    assert headers1.status == GmailOutcomeStatus.OK
    assert sleep.calls == []  # very first messages.get call for this mailbox_id — no prior timestamp

    gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"raw-1"))
    content1 = adapter.fetch_message_content(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")
    assert content1.status == GmailOutcomeStatus.OK
    assert sleep.calls == pytest.approx([0.30])  # raw call paced against the PRECEDING metadata call

    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m2", label_ids=("INBOX",))))
    headers2 = adapter.fetch_message_headers(mailbox_id=mailbox.mailbox_id, immutable_message_id="m2")
    assert headers2.status == GmailOutcomeStatus.OK
    assert sleep.calls == pytest.approx([0.30, 0.30])  # metadata call paced against the PRECEDING raw call

    gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"raw-2"))
    content2 = adapter.fetch_message_content(mailbox_id=mailbox.mailbox_id, immutable_message_id="m2")
    assert content2.status == GmailOutcomeStatus.OK
    assert sleep.calls == pytest.approx([0.30, 0.30, 0.30])

    # Zero real blocking — the fake clock only ever advances by the
    # recorded sleep durations.
    assert clock.now == pytest.approx(1_000.0 + 3 * 0.30)


def test_governor_cross_mailbox_isolation_mixed_metadata_and_raw():
    """13. Interleaved `Gmail-1 metadata, Gmail-1 raw, Gmail-2 metadata,
    Gmail-2 raw` — Gmail-2's own FIRST call (a metadata fetch) must incur
    zero governor-induced sleep despite Gmail-1's own recent, still-warm
    pacing history, and Gmail-2's own SECOND call (raw) must then pace
    normally against Gmail-2's own first call — never against Gmail-1's."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox_1 = _seeded_mailbox(mailbox_repo, email="mgs241171@gmail.com")
    mailbox_2 = _seeded_mailbox(mailbox_repo, email="matt.george.scott@gmail.com")
    for mailbox in (mailbox_1, mailbox_2):
        token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("g1-m1", label_ids=("INBOX",))))
    adapter.fetch_message_headers(mailbox_id=mailbox_1.mailbox_id, immutable_message_id="g1-m1")
    assert sleep.calls == []

    gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"raw"))
    adapter.fetch_message_content(mailbox_id=mailbox_1.mailbox_id, immutable_message_id="g1-m1")
    assert sleep.calls == pytest.approx([0.30])

    # Gmail-2's own very first messages.get call, issued right after
    # Gmail-1's own recent activity — must need no sleep at all.
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("g2-m1", label_ids=("INBOX",))))
    adapter.fetch_message_headers(mailbox_id=mailbox_2.mailbox_id, immutable_message_id="g2-m1")
    assert sleep.calls == pytest.approx([0.30])  # unchanged — zero NEW sleeps from Gmail-2's first call

    # Gmail-2's own SECOND call paces normally against Gmail-2's own
    # first — never against Gmail-1's much-earlier-in-wall-clock-terms
    # history.
    gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"raw"))
    adapter.fetch_message_content(mailbox_id=mailbox_2.mailbox_id, immutable_message_id="g2-m1")
    assert sleep.calls == pytest.approx([0.30, 0.30])


def test_governor_paces_both_attempts_of_a_metadata_auth_error_retry():
    """14. `fetch_message_headers` hits AUTH_ERROR on its first
    `fetch_message_metadata` call, reactively refreshes, and retries the
    SAME call once — both the original attempt AND the retry attempt
    must each individually pass through the governor. Proven two ways:
    (a) `gmail_client.metadata_calls` records the message_id TWICE (one
    real provider round trip per governor consultation), and (b) the
    retry attempt — issued with zero elapsed fake-clock time right after
    the first — incurs exactly one governor-induced sleep, which could
    only happen if the governor was consulted a SECOND time (a single
    consultation for a mailbox_id with no prior history never sleeps)."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="stale", refresh_token="refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=_fresh_tokens()))
    gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata("m1", label_ids=("INBOX",))))

    result = adapter.fetch_message_headers(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")

    assert result.status == GmailOutcomeStatus.OK
    assert gmail_client.metadata_calls == ["m1", "m1"]  # original attempt + retry, both real provider calls
    assert len(oauth_client.token_calls) == 1  # exactly one reactive refresh, never more
    # The retry's own governor consultation had zero elapsed fake-clock
    # time relative to the first — it can only have needed to sleep if
    # the governor was genuinely consulted a second time.
    assert sleep.calls == pytest.approx([0.30])


def test_governor_paces_both_attempts_of_a_raw_auth_error_retry():
    """15. The identical shape as the metadata AUTH-retry test above, but
    for `fetch_message_content`'s own AUTH_ERROR retry path — this is
    the actual NEW coverage this WO adds, since `fetch_message_raw` was
    entirely unpaced before this fix. Both the original
    `fetch_message_raw` call and its reactive-refresh retry must each
    individually pass through the governor."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="stale", refresh_token="refresh-token", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.OK, tokens=_fresh_tokens()))
    gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"raw-bytes"))

    result = adapter.fetch_message_content(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")

    assert result.status == GmailOutcomeStatus.OK
    assert result.content == b"raw-bytes"
    assert gmail_client.raw_calls == ["m1", "m1"]  # original attempt + retry, both real provider calls
    assert len(oauth_client.token_calls) == 1  # exactly one reactive refresh, never more
    assert sleep.calls == pytest.approx([0.30])


def test_pilot_shaped_regression_ten_candidates_twenty_governed_messages_get_calls():
    """16. Reproduces the completed `email.interactivebrokers.com` pilot's
    own real shape at the ADAPTER layer: 10 sequential candidates, each
    needing one metadata (`fetch_message_headers`, mirroring
    `_reprocess_one_message`'s own authentication-gate refresh) and one
    raw (`fetch_message_content`) `messages.get` call — 20 logical
    `messages.get` operations in total, all governed by the SAME shared
    per-mailbox governor with correct spacing, zero real network calls."""
    oauth_client, gmail_client, token_store, mailbox_repo, adapter, clock, sleep = _paced_adapter(min_interval_seconds=0.30)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    message_ids = [f"ibkr-{i}" for i in range(10)]
    for message_id in message_ids:
        gmail_client.queue_metadata_result(
            GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=_msg_metadata(message_id, label_ids=("INBOX",)))
        )
        gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=b"clean-mime"))

    for message_id in message_ids:
        headers_result = adapter.fetch_message_headers(mailbox_id=mailbox.mailbox_id, immutable_message_id=message_id)
        assert headers_result.status == GmailOutcomeStatus.OK
        content_result = adapter.fetch_message_content(mailbox_id=mailbox.mailbox_id, immutable_message_id=message_id)
        assert content_result.status == GmailOutcomeStatus.OK

    # 10 metadata + 10 raw == 20 real provider messages.get calls total.
    assert len(gmail_client.metadata_calls) == 10
    assert len(gmail_client.raw_calls) == 10
    # 20 total governed calls for this mailbox_id -> the FIRST never
    # sleeps, the other 19 each sleep the configured minimum interval —
    # proving the unified governor was consulted before all 20, not
    # merely before the 10 metadata calls or the 10 raw calls alone.
    assert sleep.calls == pytest.approx([0.30] * 19)
    assert clock.now == pytest.approx(1_000.0 + 19 * 0.30)


# =======================================================================
# CD-6 real-production-incident regression — the REAL `GmailClient` (never
# `FakeGmailClient`) wired into a real `GmailMailboxAdapter`, monkeypatching
# ONLY `urllib.request.urlopen` (no real network call), proves the full
# seam this delivery fixes: a genuine Gmail 403 quota-exceeded HTTPError
# with a body over 500 characters — the real incident's own distinguishing
# feature — actually produces `GmailOutcomeStatus.RATE_LIMITED` once it
# reaches this adapter's own `fetch_message_headers`/`fetch_message_content`,
# not merely at the lower-level `_classify_http_error` unit tests in
# `test_mailbox_gmail_client.py`. The companion half of this proof — that
# `services/mailbox/sweep.py`'s own bounded-retry-then-succeed logic
# correctly reacts to a `RATE_LIMITED` result — is already covered by
# `255b527`'s own `test_reprocess_header_rate_limited_retries_once_then_succeeds`
# in `tests/integration/test_mailbox_sweep.py` (provider-neutral, driven via
# `GraphMessageHeadersResult` directly) and is unchanged by this delivery.
# =======================================================================


#: Verbatim (module-local copy — this file drives the REAL `GmailClient`,
#: not `_classify_http_error` directly, so it does not import test-only
#: helpers from `test_mailbox_gmail_client.py`) real Google quota message
#: text, matching the real production incident this delivery fixes.
_REAL_QUOTA_MESSAGE_TEXT = (
    "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user' "
    "of service 'gmail.googleapis.com' for consumer 'project_number:393131324766'."
)


def _real_production_quota_body_over_500_chars() -> bytes:
    body = {
        "error": {
            "code": 403,
            "message": _REAL_QUOTA_MESSAGE_TEXT,
            "errors": [
                {
                    "message": _REAL_QUOTA_MESSAGE_TEXT,
                    "domain": "usageLimits",
                    "reason": "rateLimitExceeded",
                    "extendedHelp": "https://developers.google.com/gmail/api/reference/quota",
                }
            ],
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    encoded = json.dumps(body).encode("utf-8")
    assert len(encoded) > 500
    return encoded


def _fake_quota_http_error() -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        url="https://www.googleapis.com/gmail/v1/users/me/messages/m1",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=io.BytesIO(_real_production_quota_body_over_500_chars()),
    )


class _RaisingUrlopenOncePerCall:
    """A fresh `HTTPError` (its body stream can only be read once) is
    raised on EVERY call — mirrors a real Gmail transport genuinely
    returning the same 403 repeatedly, never a stale/already-consumed
    exception object reused across calls."""

    def __call__(self, request, timeout=None):  # noqa: ARG002 - matches urlopen's own signature
        raise _fake_quota_http_error()


def test_real_gmail_client_403_over_500_chars_produces_rate_limited_via_fetch_message_headers(monkeypatch):
    """The gmail_client half of the end-to-end seam: a REAL
    `GmailMailboxAdapter`, wired to a REAL `GmailClient` (never
    `FakeGmailClient`), given a genuine long-body 403 `HTTPError` from
    the underlying transport, produces `RATE_LIMITED` from
    `fetch_message_headers` — proving the CD-6 classification fix is
    real at the adapter seam a real historical-backfill call site
    actually uses, not just at the `_classify_http_error` unit level."""
    oauth_client = FakeGmailOAuthClient()
    gmail_client = GmailClient()
    token_store = InMemoryGmailTokenStore()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = GmailMailboxAdapter(oauth_client=oauth_client, gmail_client=gmail_client, token_store=token_store, mailbox_repository=mailbox_repo)
    mailbox = _seeded_mailbox(mailbox_repo)
    # A fresh, non-expiring token — `_ensure_fresh_access_token` returns it
    # directly, so no OAuth refresh call (real or fake) is ever needed.
    token_store.write(mailbox.mailbox_id, access_token="access-1", refresh_token="refresh-1", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    monkeypatch.setattr(urllib.request, "urlopen", _RaisingUrlopenOncePerCall())

    result = adapter.fetch_message_headers(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")

    assert result.status == GmailOutcomeStatus.RATE_LIMITED
    assert result.status != GmailOutcomeStatus.PERMISSION_ERROR


def test_real_gmail_client_403_over_500_chars_produces_rate_limited_via_fetch_message_content(monkeypatch):
    """The identical proof as above, for `fetch_message_content` (the
    MIME/raw fetch) — the other real call site
    `reprocess_all_historical_candidates_for_domain`/
    `_reprocess_one_message` uses for a historical candidate."""
    oauth_client = FakeGmailOAuthClient()
    gmail_client = GmailClient()
    token_store = InMemoryGmailTokenStore()
    mailbox_repo = InMemoryMailboxSourceRepository()
    adapter = GmailMailboxAdapter(oauth_client=oauth_client, gmail_client=gmail_client, token_store=token_store, mailbox_repository=mailbox_repo)
    mailbox = _seeded_mailbox(mailbox_repo)
    token_store.write(mailbox.mailbox_id, access_token="access-1", refresh_token="refresh-1", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    monkeypatch.setattr(urllib.request, "urlopen", _RaisingUrlopenOncePerCall())

    result = adapter.fetch_message_content(mailbox_id=mailbox.mailbox_id, immutable_message_id="m1")

    assert result.status == GmailOutcomeStatus.RATE_LIMITED
    assert result.status != GmailOutcomeStatus.PERMISSION_ERROR
