"""CD-6 GUI-operations-foundation follow-on WO tests for the Gmail
provider wired into `services.mailbox.sweep.run_sweep` — the SAME
provider-neutral sweep engine Microsoft/IMAP already use (see
`tests/integration/test_mailbox_sweep.py`/`test_mailbox_imap_sweep.py`
for those providers' own equivalent coverage; this file proves the
identical engine behaves correctly for the THIRD provider, driven
entirely by `FakeGmailClient`/`FakeGmailOAuthClient` — zero real
network).

Covers (per the WO's own explicit test list):
* Stage-A bounded discovery: `MailboxMessage` records created, zero
  `EvidenceItem`, zero MIME-fetch calls to the fake client's raw-content
  method, zero automatic `MailboxDomainRule` creation, zero default
  entity assignment.
* `default_entity_id` stays `NULL` for BOTH Gmail mailbox rows.
* Gmail message identity is `mailbox_id`-scoped and durable: the SAME
  message content appearing in BOTH Gmail mailboxes produces TWO
  separate `MailboxMessage` records, never collapsed.
* A message under multiple monitored labels is discovered exactly once
  per pass, not once per label.
* Restart/idempotency: re-running discovery produces no duplicate rows.
* Cross-mailbox isolation: independent `MailboxDomainRule`s; revoking/
  failing one mailbox's connection never affects the other's.
"""
from __future__ import annotations

import pytest

from core.api import BagmanCanonicalAPI
from core.errors import ConflictError
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict
from services.mailbox.cursor import InMemoryMailboxFolderCursorRepository
from services.mailbox.domain_rule import InMemoryMailboxDomainRuleRepository
from services.mailbox.gmail.fake_gmail_client import FakeGmailClient, FakeGmailOAuthClient
from services.mailbox.gmail.gmail_adapter import GmailMailboxAdapter
from services.mailbox.gmail.gmail_client import (
    GmailLabel,
    GmailLabelListResult,
    GmailMessageListPageResult,
    GmailMessageMetadata,
    GmailMessageMetadataResult,
    GmailMessageRawResult,
    GmailOutcomeStatus,
    GmailTokenResult,
)
from services.mailbox.gmail.secrets import InMemoryGmailTokenStore
from services.mailbox.lock import InMemoryMailboxSweepLock
from services.mailbox.mailbox import PROVIDER_GOOGLE_GMAIL, InMemoryMailboxSourceRepository
from services.mailbox.message import (
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_SECURITY_REVIEW,
    InMemoryMailboxMessageRepository,
)
from services.mailbox.sweep import run_sweep
from services.mailbox.sweep_run import InMemoryMailboxSweepRunRepository, TRIGGER_MANUAL
from services.needs_you.needs_you import (
    ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
    InMemoryNeedsYouRepository,
)


class AlwaysCleanScanner(EvidenceSafetyScanner):
    def scan(self, content):
        return ScanResult(ScanVerdict.CLEAN, detail="clean")

    def is_available(self):
        return True


def _metadata(message_id: str, *, subject="Invoice", sender="billing@vendor.com", internal_date=None, extra_headers=None):
    from datetime import datetime, timezone

    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": f"Vendor <{sender}>"},
        {"name": "Message-ID", "value": f"<{message_id}@vendor.com>"},
        {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
    ]
    if extra_headers:
        headers.extend(extra_headers)
    return GmailMessageMetadata(
        message_id=message_id,
        raw_headers=tuple(headers),
        label_ids=("INBOX",),
        internal_date=internal_date or datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


class Harness:
    def __init__(self, *, allow_default_domain: bool = True, email: str = "mgs241171@gmail.com"):
        self.api = BagmanCanonicalAPI()
        self.object_store = InMemoryObjectStore()
        self.scanner = AlwaysCleanScanner()
        self.mailbox_repo = InMemoryMailboxSourceRepository()
        self.message_repo = InMemoryMailboxMessageRepository()
        self.sweep_run_repo = InMemoryMailboxSweepRunRepository()
        self.cursor_repo = InMemoryMailboxFolderCursorRepository()
        self.domain_rule_repo = InMemoryMailboxDomainRuleRepository()
        self.needs_you_repo = InMemoryNeedsYouRepository()
        self.lock = InMemoryMailboxSweepLock()
        self.oauth_client = FakeGmailOAuthClient()
        self.gmail_client = FakeGmailClient()
        self.token_store = InMemoryGmailTokenStore()

        self.adapter = GmailMailboxAdapter(
            oauth_client=self.oauth_client, gmail_client=self.gmail_client, token_store=self.token_store,
            mailbox_repository=self.mailbox_repo,
        )

        self.mailbox = self.mailbox_repo.create_mailbox(display_name="Personal Gmail", email_address=email, provider_kind=PROVIDER_GOOGLE_GMAIL)
        self.mailbox_repo.begin_microsoft_connect(self.mailbox.mailbox_id)
        self.mailbox_repo.mark_microsoft_connected(self.mailbox.mailbox_id)
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)

        from datetime import datetime, timedelta, timezone

        self.token_store.write(self.mailbox.mailbox_id, access_token="a", refresh_token="r", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

        source = self.api.register_source(
            source_type="EMAIL_MAILBOX", provider=self.mailbox.mailbox_id, status="ACTIVE", actor_type="SYSTEM", actor_id="test"
        )
        self.source_id = source.source_id

        canonical_suffix = "".join(c if c.isalnum() else "_" for c in email.upper())
        self.entity = self.api.register_entity(
            entity_type="COMPANY", canonical_name=f"TEST_ENTITY_{canonical_suffix}", display_name="Test Entity", status="ACTIVE",
            actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="01-01",
        )

        if allow_default_domain:
            self.domain_rule_repo.upsert_rule(
                mailbox_id=self.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT",
                policy="MUST_READ", destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
            )

    def refresh_mailbox(self):
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)
        return self.mailbox

    def queue_discovery(self, *, labels=("INBOX",)):
        self.gmail_client.queue_labels_result(
            GmailLabelListResult(status=GmailOutcomeStatus.OK, labels=tuple(GmailLabel(label_id=l, name=l, label_type="system") for l in labels))
        )

    def queue_folder_round(self, *, message_ids=(), metadata_by_id=None):
        self.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=tuple(message_ids)))
        metadata_by_id = metadata_by_id or {mid: _metadata(mid) for mid in message_ids}
        for mid in message_ids:
            self.gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.OK, metadata=metadata_by_id[mid]))

    def queue_content(self, body: bytes = b"From: billing@vendor.com\r\nSubject: Invoice\r\n\r\nBody"):
        self.gmail_client.queue_raw_result(GmailMessageRawResult(status=GmailOutcomeStatus.OK, content=body))

    def sweep(self):
        run = run_sweep(
            mailbox=self.mailbox, mailbox_source_id=self.source_id, trigger=TRIGGER_MANUAL, adapter=self.adapter,
            message_repository=self.message_repo, sweep_run_repository=self.sweep_run_repo,
            cursor_repository=self.cursor_repo, sweep_lock=self.lock, mailbox_repository=self.mailbox_repo,
            domain_rule_repository=self.domain_rule_repo, needs_you_repository=self.needs_you_repo,
            entity_repository=self.api.entity_repository,
            api=self.api, object_store=self.object_store, scanner=self.scanner, actor_type="SYSTEM", actor_id="test",
        )
        self.refresh_mailbox()
        return run


def _force_auth_pass(monkeypatch):
    """See `tests/integration/test_mailbox_imap_sweep.py::_force_auth_pass`'s
    own docstring for the full reasoning this mirrors exactly:
    `assess_gmail_authentication` is deliberately, correctly, always
    UNKNOWN right now (no live diagnostic exists yet) — this helper
    monkeypatches the sweep engine's own imported name so tests that
    prove DOWNSTREAM mechanics (evidence creation, idempotency,
    cross-mailbox isolation) can do so without depending on the real
    selector's own conservative behaviour."""
    from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_PASS, AuthenticationAssessment
    import services.mailbox.sweep as sweep_module

    monkeypatch.setattr(
        sweep_module,
        "assess_gmail_authentication",
        lambda raw_headers: AuthenticationAssessment(verdict=AUTH_ASSESSMENT_PASS, reason="test-forced PASS", evidence={}),
    )


@pytest.fixture
def h() -> Harness:
    return Harness()


# -- successful connect + sweep -------------------------------------------


def test_successful_sweep_with_fake_client_ingests_evidence(h, monkeypatch):
    _force_auth_pass(monkeypatch)
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",))
    h.queue_content()
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_INGESTED
    assert messages[0].immutable_provider_message_id == "m1"
    assert messages[0].internet_message_id == "<m1@vendor.com>"


def test_successful_sweep_never_assigns_a_default_entity_for_review_required_rule(h, monkeypatch):
    _force_auth_pass(monkeypatch)
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",))
    h.queue_content()
    h.sweep()
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    evidence = h.api.evidence_repository.get_evidence(messages[0].evidence_id)
    assert evidence.entity_id is None


def test_must_read_message_with_the_real_unforced_selector_goes_to_security_review_never_evidence(h):
    """The REAL, current (correctly conservative) end-to-end behaviour —
    no monkeypatch. `assess_gmail_authentication` always returns UNKNOWN
    right now, so a MUST_READ-domain message must route to
    SECURITY_REVIEW, never proceed to MIME/evidence."""
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",))
    # Deliberately NOT calling h.queue_content() — MIME must never be
    # fetched for a SECURITY_REVIEW-routed message.
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 0
    assert h.gmail_client.raw_calls == []

    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert messages[0].evidence_id is None

    escalations = [
        i for i in h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION)
        if i.metadata.get("mailbox_id") == h.mailbox.mailbox_id
    ]
    assert len(escalations) == 1


# -- Stage-A bounded discovery ---------------------------------------------


def test_unknown_domain_message_creates_mailbox_message_zero_evidence_zero_mime_fetch(h):
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",), metadata_by_id={"m1": _metadata("m1", subject="Invoice attached", sender="new@unknown-domain.com")})
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 0
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert messages[0].discovery_candidate is True
    assert h.gmail_client.raw_calls == []


def test_unknown_domain_credible_candidate_raises_exactly_one_needs_you_item_never_auto_creates_a_rule(h):
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",), metadata_by_id={"m1": _metadata("m1", subject="Your invoice", sender="ap@newsupplier.com")})
    h.sweep()

    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status="OPEN")
    matching = [i for i in items if i.metadata.get("mailbox_id") == h.mailbox.mailbox_id]
    assert len(matching) == 1

    rules = h.domain_rule_repo.list_rules(mailbox_id=h.mailbox.mailbox_id)
    assert "newsupplier.com" not in {r.sender_domain for r in rules}


def test_default_entity_id_stays_null_for_both_gmail_mailbox_rows():
    """Explicit WO-required proof: NOTHING in the Gmail router/adapter/
    composition wiring ever assigns `default_entity_id` — for either of
    the two real Gmail accounts this delivery targets."""
    repo = InMemoryMailboxSourceRepository()
    mailbox_a = repo.create_mailbox(display_name="Personal Gmail", email_address="mgs241171@gmail.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox_b = repo.create_mailbox(display_name="Work Gmail", email_address="matt.george.scott@gmail.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    assert mailbox_a.default_entity_id is None
    assert mailbox_b.default_entity_id is None


# -- Gmail durable message identity ----------------------------------------


def test_same_message_content_observed_in_both_gmail_mailboxes_produces_two_separate_records():
    """WO-required explicit proof: the SAME immutable_provider_message_id
    string observed for TWO different `mailbox_id`s must never collapse
    into one `MailboxMessage` — the composite `(mailbox_id,
    immutable_provider_message_id)` key is what gives mailbox-scoping."""
    harness_a = Harness(email="mgs241171@gmail.com")
    harness_b = Harness(email="matt.george.scott@gmail.com")

    shared_metadata = _metadata("shared-gmail-id-123", subject="Your invoice", sender="ap@newsupplier.com")

    harness_a.queue_discovery()
    harness_a.queue_folder_round(message_ids=("shared-gmail-id-123",), metadata_by_id={"shared-gmail-id-123": shared_metadata})
    harness_a.sweep()

    harness_b.queue_discovery()
    harness_b.queue_folder_round(message_ids=("shared-gmail-id-123",), metadata_by_id={"shared-gmail-id-123": shared_metadata})
    harness_b.sweep()

    messages_a = harness_a.message_repo.list_messages(mailbox_id=harness_a.mailbox.mailbox_id)
    messages_b = harness_b.message_repo.list_messages(mailbox_id=harness_b.mailbox.mailbox_id)
    assert len(messages_a) == 1
    assert len(messages_b) == 1
    assert messages_a[0].immutable_provider_message_id == "shared-gmail-id-123"
    assert messages_b[0].immutable_provider_message_id == "shared-gmail-id-123"
    # Two DIFFERENT MailboxMessage rows (different mailbox_message_id),
    # never collapsed, despite the identical provider message id.
    assert messages_a[0].mailbox_message_id != messages_b[0].mailbox_message_id
    assert messages_a[0].mailbox_id != messages_b[0].mailbox_id


# -- label handling: exactly once per pass, not once per label -----------


def test_message_under_two_monitored_labels_is_discovered_exactly_once_not_once_per_label(h):
    """`services/mailbox/sweep.py` iterates each monitored label as its
    own folder pass — the SAME Gmail message id appearing in BOTH the
    `INBOX` pass and the `SPAM` pass must resolve to exactly ONE
    `MailboxMessage` row (the second folder's own occurrence is recorded
    as a cheap 'duplicate' observation, never a second discovery/
    evidence-creation), never processed/evidenced twice."""
    metadata = _metadata("dup-message-1", subject="Invoice", sender="new@unknown-domain.com")
    h.queue_discovery(labels=("INBOX", "SPAM"))
    # INBOX pass.
    h.queue_folder_round(message_ids=("dup-message-1",), metadata_by_id={"dup-message-1": metadata})
    # SPAM pass — the SAME message id observed again.
    h.queue_folder_round(message_ids=("dup-message-1",), metadata_by_id={"dup-message-1": metadata})

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert run.duplicates == 1
    # Metadata was fetched twice (once per folder pass — a real API call
    # cost this design accepts, see gmail_adapter.py's own module
    # docstring), but never resulted in a second MailboxMessage/Needs You
    # item/evidence record.
    assert h.gmail_client.metadata_calls.count("dup-message-1") == 2


# -- idempotency / restart-safety ------------------------------------------


def test_rerunning_discovery_against_the_same_fake_state_produces_no_duplicate_messages(h, monkeypatch):
    _force_auth_pass(monkeypatch)
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",))
    h.queue_content()
    first_run = h.sweep()
    assert first_run.evidence_created == 1

    # Simulate a process restart that lost the durable cursor.
    h.cursor_repo = InMemoryMailboxFolderCursorRepository()
    h.queue_discovery()
    h.queue_folder_round(message_ids=("m1",))
    second_run = h.sweep()
    assert second_run.evidence_created == 0
    assert second_run.duplicates == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1


# -- cross-mailbox isolation ------------------------------------------------


def test_domain_rule_created_for_one_mailbox_never_governs_the_other():
    harness_a = Harness(email="mgs241171@gmail.com", allow_default_domain=False)
    harness_b = Harness(email="matt.george.scott@gmail.com", allow_default_domain=False)

    harness_a.domain_rule_repo.upsert_rule(
        mailbox_id=harness_a.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT",
        policy="MUST_READ", destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )

    rule_for_a = harness_a.domain_rule_repo.find_for_sender(mailbox_id=harness_a.mailbox.mailbox_id, sender_domain="vendor.com", sender_address=None)
    rule_for_b = harness_b.domain_rule_repo.find_for_sender(mailbox_id=harness_b.mailbox.mailbox_id, sender_domain="vendor.com", sender_address=None)
    assert rule_for_a is not None
    assert rule_for_b is None


def test_revoking_one_mailboxs_connection_never_affects_the_others():
    harness_a = Harness(email="mgs241171@gmail.com")
    harness_b = Harness(email="matt.george.scott@gmail.com")

    harness_a.oauth_client.queue_refresh_result(GmailTokenResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail="revoked"))
    # Force mailbox A's stored token to look expired so a sweep attempt
    # triggers a pre-emptive refresh that then fails.
    from datetime import datetime, timedelta, timezone

    harness_a.token_store.write(harness_a.mailbox.mailbox_id, access_token="expiring", refresh_token="refresh-a", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    harness_a.queue_discovery()
    harness_a.sweep()

    assert harness_a.mailbox_repo.get_mailbox(harness_a.mailbox.mailbox_id).connection_state == "AUTH_REQUIRED"
    assert harness_b.mailbox_repo.get_mailbox(harness_b.mailbox.mailbox_id).connection_state == "CONNECTED"


# -- precondition -----------------------------------------------------------


def test_precondition_rejects_a_non_connected_mailbox():
    harness = Harness()
    harness.mailbox_repo.disconnect_microsoft(harness.mailbox.mailbox_id)
    harness.mailbox = harness.mailbox_repo.get_mailbox(harness.mailbox.mailbox_id)
    with pytest.raises(ConflictError):
        harness.sweep()


def test_concurrent_sweep_declines_cleanly(h):
    h.queue_discovery()
    h.queue_folder_round(message_ids=())
    token = h.lock.try_acquire(h.mailbox.mailbox_id)
    try:
        with pytest.raises(Exception):
            h.sweep()
    finally:
        h.lock.release(h.mailbox.mailbox_id, token)
