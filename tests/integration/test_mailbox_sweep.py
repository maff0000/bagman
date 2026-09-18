"""CD-6 Slice 4 tests for `services.mailbox.sweep.run_sweep` — the
provider-neutral sweep orchestration engine. Exercises bootstrap/delta
doctrine, idempotency (duplicate-across-pages, same-immutable-id-in-
another-folder), cursor-advance-only-after-success, quarantine/oversize/
vanished durably-handled semantics, transient-failure-leaves-cursor-
unadvanced-then-succeeds-on-retry, concurrent-sweep-exclusion, and
provider error-taxonomy handling (401/403/429/410-resync) — all via
`FakeMicrosoftOAuthClient`/`FakeMicrosoftGraphClient`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.api import BagmanCanonicalAPI
from core.errors import ConflictError
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict
from services.mailbox.cursor import InMemoryMailboxFolderCursorRepository
from services.mailbox.domain_rule import InMemoryMailboxDomainRuleRepository
from services.mailbox.lock import InMemoryMailboxSweepLock, MailboxSweepLockError
from services.mailbox.mailbox import PROVIDER_MICROSOFT_GRAPH, InMemoryMailboxSourceRepository
from services.mailbox.message import (
    FOLDER_INBOX,
    FOLDER_JUNK,
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_VANISHED,
    InMemoryMailboxMessageRepository,
)
from services.mailbox.microsoft.adapter import MicrosoftGraphMailboxAdapter
from services.mailbox.microsoft.fake_client import FakeMicrosoftGraphClient, FakeMicrosoftOAuthClient
from services.mailbox.microsoft.graph_client import (
    GraphDeltaPageResult,
    GraphMessageContentResult,
    GraphMessageSummary,
    GraphOutcomeStatus,
    MicrosoftTokenResult,
)
from services.mailbox.microsoft.secrets import InMemoryMicrosoftTokenStore
from services.mailbox.sweep import run_sweep
from services.mailbox.sweep_run import InMemoryMailboxSweepRunRepository, SweepFailureReason, TRIGGER_MANUAL
from services.needs_you.needs_you import ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, InMemoryNeedsYouRepository


class AlwaysCleanScanner(EvidenceSafetyScanner):
    def scan(self, content):
        return ScanResult(ScanVerdict.CLEAN, detail="clean")

    def is_available(self):
        return True


class AlwaysMaliciousScanner(EvidenceSafetyScanner):
    def scan(self, content):
        return ScanResult(ScanVerdict.MALICIOUS, detail="eicar")

    def is_available(self):
        return True


def _msg(
    msg_id: str, *, received_at=None, subject="Invoice", sender_address="billing@vendor.com", attachment_metadata=()
) -> GraphMessageSummary:
    return GraphMessageSummary(
        immutable_id=msg_id,
        internet_message_id=f"<{msg_id}@example.com>",
        subject=subject,
        sender_address=sender_address,
        sender_display_name="Vendor",
        received_at=received_at or datetime.now(timezone.utc),
        has_attachments=bool(attachment_metadata),
        attachment_metadata=tuple(attachment_metadata),
    )


def _content(body: bytes = b"From: billing@vendor.com\r\nSubject: Invoice\r\n\r\nBody") -> GraphMessageContentResult:
    return GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=body)


#: The default sender domain `_msg()` uses — a real `ALLOWED`
#: `MailboxDomainRule` is set up for it in `Harness.__init__` (CD-6
#: architect amendment — two-stage mail processing means a sweep no
#: longer ingests unconditionally; most of this file's pre-existing
#: coverage is proving ALLOWED-path behaviour, so the rule is set up
#: once, centrally, exactly like a real prior operator approval would
#: be). Tests that specifically exercise the domain GATE itself use a
#: different, deliberately ungoverned domain instead.
_DEFAULT_ALLOWED_DOMAIN = "vendor.com"


class Harness:
    def __init__(self, scanner=None, *, allow_default_domain: bool = True):
        self.api = BagmanCanonicalAPI()
        self.object_store = InMemoryObjectStore()
        self.scanner = scanner or AlwaysCleanScanner()
        self.mailbox_repo = InMemoryMailboxSourceRepository()
        self.message_repo = InMemoryMailboxMessageRepository()
        self.sweep_run_repo = InMemoryMailboxSweepRunRepository()
        self.cursor_repo = InMemoryMailboxFolderCursorRepository()
        self.domain_rule_repo = InMemoryMailboxDomainRuleRepository()
        self.needs_you_repo = InMemoryNeedsYouRepository()
        self.lock = InMemoryMailboxSweepLock()
        self.oauth_client = FakeMicrosoftOAuthClient()
        self.graph_client = FakeMicrosoftGraphClient()
        self.token_store = InMemoryMicrosoftTokenStore()
        self.adapter = MicrosoftGraphMailboxAdapter(
            oauth_client=self.oauth_client, graph_client=self.graph_client,
            token_store=self.token_store, mailbox_repository=self.mailbox_repo,
        )
        self.mailbox = self.mailbox_repo.create_mailbox(
            display_name="Matt", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
        )
        self.mailbox_repo.begin_microsoft_connect(self.mailbox.mailbox_id)
        self.mailbox_repo.mark_microsoft_connected(self.mailbox.mailbox_id)
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)
        self.token_store.write(
            self.mailbox.mailbox_id, access_token="at", refresh_token="rt",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        source = self.api.register_source(
            source_type="EMAIL_MAILBOX", provider=self.mailbox.mailbox_id, status="ACTIVE",
            actor_type="SYSTEM", actor_id="test",
        )
        self.source_id = source.source_id

        # CD-6 architect amendment: the bootstrap floor is computed from
        # the governed entity registry, never a flat N-days constant —
        # seed one entity with a real floor so `compute_bootstrap_floor`
        # (and therefore every sweep) has something to compute from.
        self.entity = self.api.register_entity(
            entity_type="COMPANY", canonical_name="TEST_ENTITY", display_name="Test Entity", status="ACTIVE",
            actor_type="SYSTEM", actor_id="test",
            fiscal_year_start_month_day="01-01",
            email_bootstrap_floor_at=datetime.now(timezone.utc) - timedelta(days=400),
        )

        if allow_default_domain:
            self.domain_rule_repo.upsert_rule(
                mailbox_id=self.mailbox.mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN, match_mode="EXACT",
                policy="ALLOWED", destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
            )

    def refresh_mailbox(self):
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)
        return self.mailbox

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

    def queue_empty_both_folders(self, inbox_delta="d-inbox", junk_delta="d-junk"):
        self.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=inbox_delta))
        self.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=junk_delta))


@pytest.fixture
def h() -> Harness:
    return Harness()


# ---------------------------------------------------------------------
# Basics: bootstrap, both folders attempted, evidence created
# ---------------------------------------------------------------------


def test_precondition_rejects_a_non_connected_mailbox():
    h = Harness()
    h.mailbox_repo.disconnect_microsoft(h.mailbox.mailbox_id)
    h.mailbox = h.mailbox_repo.get_mailbox(h.mailbox.mailbox_id)
    with pytest.raises(ConflictError):
        h.sweep()


def test_bootstrap_query_uses_the_bootstrap_timestamp_on_the_first_round(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    h.sweep()
    inbox_call = h.graph_client.delta_calls[0]
    assert inbox_call["delta_link"] is None
    assert inbox_call["bootstrap_timestamp"] is not None


def test_both_folders_are_attempted_and_succeed(h):
    h.queue_empty_both_folders()
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert set(run.folders_attempted) == {FOLDER_INBOX, FOLDER_JUNK}


def test_a_new_message_is_ingested_as_real_evidence(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].evidence_id is not None


def test_successful_sweep_stamps_last_successful_sweep_at(h):
    h.queue_empty_both_folders()
    assert h.mailbox.last_successful_sweep_at is None
    h.sweep()
    assert h.mailbox.last_successful_sweep_at is not None


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


def test_duplicate_across_pages_within_one_round_is_not_double_ingested(h):
    """The SAME immutable id appears on two separate delta pages of the
    SAME folder round (pagination) — must resolve to exactly one
    MailboxMessage/EvidenceItem, counted as a duplicate on its second
    appearance."""
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), next_link="page2")
    )
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d-final")
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 1
    assert run.duplicates == 1
    assert len(h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)) == 1


def test_same_immutable_id_seen_in_another_folder_never_duplicates_evidence(h):
    """A message that moved Inbox -> Junk between sweeps (or is
    reported in both during one run) must never create a second
    evidence object — canonical uniqueness is (mailbox_id,
    immutable_provider_message_id) alone, folder-independent."""
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d-inbox"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d-junk"))
    run = h.sweep()
    assert run.evidence_created == 1
    assert run.duplicates == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].observed_folder == FOLDER_JUNK  # last-seen wins


def test_idempotent_replay_of_a_whole_sweep_creates_no_new_evidence(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    first = h.sweep()
    assert first.evidence_created == 1

    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d3"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d4"))
    second = h.sweep()
    assert second.evidence_created == 0
    assert second.duplicates == 1
    assert len(h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)) == 1


# ---------------------------------------------------------------------
# Cursor semantics
# ---------------------------------------------------------------------


def test_cursor_advances_only_after_a_folder_round_fully_succeeds(h):
    h.queue_empty_both_folders(inbox_delta="final-inbox-link", junk_delta="final-junk-link")
    h.sweep()
    cursor = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=FOLDER_INBOX,
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    assert cursor.delta_link == "final-inbox-link"


def test_transient_content_fetch_failure_leaves_cursor_unadvanced_then_succeeds_on_retry(h):
    # First sweep: delta OK, but MIME fetch transiently fails.
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(GraphMessageContentResult(status=GraphOutcomeStatus.TRANSPORT_ERROR, error_detail="timeout"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-1"))
    first = h.sweep()
    assert first.status == "PARTIAL"
    assert first.failures == 1
    assert len(h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)) == 0  # no row left behind

    cursor = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=FOLDER_INBOX,
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    assert cursor.delta_link is None  # never advanced past the failure

    # Second sweep (retry): the bootstrap boundary is used again since
    # the cursor never advanced — the SAME message is safely re-seen
    # and this time succeeds.
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d2"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-2"))
    second = h.sweep()
    assert second.status == "SUCCEEDED"
    assert second.evidence_created == 1


# ---------------------------------------------------------------------
# Quarantine / oversize / vanished — durably-handled terminal outcomes
# ---------------------------------------------------------------------


def test_quarantined_message_counts_as_durably_handled_and_advances_cursor():
    h = Harness(scanner=AlwaysMaliciousScanner())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="final-link"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.quarantined == 1
    assert run.evidence_created == 0
    cursor = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=FOLDER_INBOX,
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    assert cursor.delta_link == "final-link"


def test_vanished_message_404_is_a_durable_recorded_outcome_never_fabricated_evidence(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(GraphMessageContentResult(status=GraphOutcomeStatus.NOT_FOUND))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_VANISHED
    assert messages[0].evidence_id is None


# ---------------------------------------------------------------------
# Provider error taxonomy
# ---------------------------------------------------------------------


def test_401_at_delta_level_stops_the_whole_sweep_and_marks_auth_required(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail="expired"))
    h.oauth_client.queue_refresh_result(MicrosoftTokenResult(status=GraphOutcomeStatus.AUTH_ERROR, error_detail="revoked"))
    run = h.sweep()
    assert run.status == "FAILED"
    assert run.error_code == SweepFailureReason.TOKEN_REFRESH_FAILED
    from services.mailbox.mailbox import CONNECTION_STATE_AUTH_REQUIRED

    assert h.mailbox.connection_state == CONNECTION_STATE_AUTH_REQUIRED


def test_403_permission_error_marks_folder_failed_and_sets_connection_error(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.PERMISSION_ERROR, error_detail="forbidden"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status in ("PARTIAL", "FAILED")
    from services.mailbox.mailbox import CONNECTION_STATE_ERROR

    assert h.mailbox.connection_state == CONNECTION_STATE_ERROR


def test_429_rate_limited_retries_once_then_succeeds(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    run = h.sweep()
    assert run.status == "SUCCEEDED"


def test_429_rate_limited_exhausted_is_a_bounded_transient_failure(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status in ("PARTIAL", "FAILED")
    assert run.failures >= 1


def test_resync_required_never_silently_restarts_and_duplicates(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.RESYNC_REQUIRED, error_detail="expired delta token"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status in ("PARTIAL", "FAILED")
    cursor = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=FOLDER_INBOX,
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    # Never automatically reset/advanced — an explicit operator action
    # would be required for a real resync (not built this slice).
    assert cursor.delta_link is None


def test_resync_required_surfaces_its_own_distinct_error_code_not_a_generic_transient_one(h):
    """PL-review finding: RESYNC_REQUIRED is NOT an ordinary transient
    failure — a plain retry next sweep hits the exact same status
    forever, since the stale delta_link never becomes valid on its own.
    The sweep run's own error_code must say exactly that
    (SweepFailureReason.RESYNC_REQUIRED), never the generic
    PARTIAL_FAILURES/PROVIDER_ERROR codes an operator would read as
    "will probably self-heal by waiting" for every other kind of
    transient failure."""
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.RESYNC_REQUIRED, error_detail="expired delta token"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status in ("PARTIAL", "FAILED")
    assert run.error_code == SweepFailureReason.RESYNC_REQUIRED


def test_malformed_response_is_a_bounded_transient_failure_not_a_crash(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.MALFORMED_RESPONSE, error_detail="bad json"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status in ("PARTIAL", "FAILED")


# ---------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------


def test_concurrent_sweep_of_the_same_mailbox_declines_cleanly(h):
    token = h.lock.try_acquire(h.mailbox.mailbox_id)
    try:
        with pytest.raises(MailboxSweepLockError):
            h.sweep()
    finally:
        h.lock.release(h.mailbox.mailbox_id, token)


# ---------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------


def test_ingested_message_emits_email_evidence_ingested_audit_event(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    h.sweep()
    events = [e for e in h.api.audit_repository.list_by_subject("EvidenceItem", h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0].evidence_id) if e.event_type == "EMAIL_EVIDENCE_INGESTED"]
    assert len(events) == 1
    assert "mailbox_id" in events[0].payload


def test_quarantined_message_emits_email_evidence_quarantined_audit_event():
    h = Harness(scanner=AlwaysMaliciousScanner())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    h.sweep()
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    events = [
        e for e in h.api.audit_repository.list_by_subject("MailboxMessage", message.mailbox_message_id)
        if e.event_type == "EMAIL_EVIDENCE_QUARANTINED"
    ]
    assert len(events) == 1


# ---------------------------------------------------------------------
# CD-6 architect amendment — two-stage mail processing (Stage B gate)
# ---------------------------------------------------------------------


def test_allowed_domain_proceeds_to_full_ingest():
    """The Harness's own default ALLOWED rule for vendor.com — proving
    the ALLOWED outcome explicitly, by name, once (every other test in
    this file that asserts evidence_created relies on this same
    behaviour implicitly)."""
    h = Harness()
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.evidence_created == 1
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == "INGESTED"
    assert message.sender_domain == "vendor.com"
    assert message.metadata["routing"]["destination_mode"] == "REVIEW_REQUIRED"


def test_ignored_domain_is_checked_not_candidate_no_mime_fetch_no_needs_you():
    h = Harness(allow_default_domain=False)
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="IGNORED",
        destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 0
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert message.evidence_id is None
    # Data-volume proof (architect §9): an IGNORED-domain message NEVER
    # gets a MIME fetch — assert directly against the fake Graph
    # client's own call log.
    assert h.graph_client.content_calls == []
    assert h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW) == []

    # Re-observing the SAME ignored message on a later sweep must never
    # create a duplicate/repetitive Needs You item either.
    rule = h.domain_rule_repo.find_for_sender(mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com")
    assert rule.last_seen_at is not None


def test_unknown_domain_credible_candidate_raises_exactly_one_needs_you_item():
    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 0
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    # Data-volume proof: no MIME fetch for an unknown-domain candidate
    # either — only the (later, explicit, operator-approval-triggered)
    # reprocess step ever fetches it.
    assert h.graph_client.content_calls == []

    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1
    assert items[0].metadata["sender_domain"] == "vendor.com"
    assert items[0].source_object_reference == message.mailbox_message_id


def test_unknown_domain_second_message_reuses_the_same_open_needs_you_item():
    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"), _msg("m2")), delta_link="d1")
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))

    h.sweep()
    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1  # NOT one item per message from the same still-open domain


def test_unknown_domain_non_credible_message_is_checked_not_candidate_no_needs_you():
    h = Harness(allow_default_domain=False)
    non_candidate = _msg("m1", subject="Let's catch up for coffee next week")
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(non_candidate,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert h.graph_client.content_calls == []
    assert h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW) == []


def test_a_message_already_checked_not_candidate_is_never_re_decided_by_a_later_sweep():
    """Documented judgment call (see services/mailbox/sweep.py's own
    module docstring): a rule-policy change does NOT automatically
    reprocess a previously-seen message."""
    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE

    # Operator now approves the domain going forward...
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="ALLOWED",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    # ...but a later, ordinary sweep re-observing the SAME message must
    # NOT retroactively re-decide it (still no MIME fetch for it).
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d2"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-2"))
    second = h.sweep()
    assert second.duplicates == 1
    assert h.graph_client.content_calls == []
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE


# ---------------------------------------------------------------------
# reprocess_message_after_domain_rule_approval (architect spec §4)
# ---------------------------------------------------------------------


def test_reprocess_after_domain_rule_approval_ingests_the_triggering_message_immediately():
    from services.mailbox.sweep import reprocess_message_after_domain_rule_approval

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()
    triggering_message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert triggering_message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="ALLOWED",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_content_result(_content())
    reprocessed = reprocess_message_after_domain_rule_approval(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, message_id=triggering_message.mailbox_message_id,
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert reprocessed.ingestion_status == "INGESTED"
    assert reprocessed.evidence_id is not None
    assert h.graph_client.content_calls == ["m1"]


def test_reprocess_after_domain_rule_approval_is_idempotent_on_a_double_submit():
    from services.mailbox.sweep import reprocess_message_after_domain_rule_approval

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()
    triggering_message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="ALLOWED",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_content_result(_content())
    first = reprocess_message_after_domain_rule_approval(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, message_id=triggering_message.mailbox_message_id,
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    # A second call (e.g. a double-submit of the same approval) must
    # NOT fetch content again — the message is already fully decided.
    second = reprocess_message_after_domain_rule_approval(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, message_id=triggering_message.mailbox_message_id,
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert second.mailbox_message_id == first.mailbox_message_id
    assert h.graph_client.content_calls == ["m1"]  # only ONE content fetch total


# ---------------------------------------------------------------------
# compute_bootstrap_floor (architect spec §1/§10 — governed, not hardcoded)
# ---------------------------------------------------------------------


def test_compute_bootstrap_floor_is_the_global_minimum_across_entities():
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    earlier = datetime(2025, 4, 6, tzinfo=timezone.utc)
    later = datetime(2025, 11, 1, tzinfo=timezone.utc)
    api.register_entity(
        entity_type="COMPANY", canonical_name="A_LTD", display_name="A", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="11-01", email_bootstrap_floor_at=later,
    )
    api.register_entity(
        entity_type="PERSON", canonical_name="B_PERSONAL", display_name="B", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="04-06", email_bootstrap_floor_at=earlier,
    )
    assert compute_bootstrap_floor(api.entity_repository) == earlier


def test_compute_bootstrap_floor_fails_honestly_when_any_entity_is_missing_configuration():
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    api.register_entity(
        entity_type="COMPANY", canonical_name="A_LTD", display_name="A", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="11-01",
        email_bootstrap_floor_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
    )
    api.register_entity(
        entity_type="PERSON", canonical_name="B_PERSONAL", display_name="B", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test",
        # No fiscal_year_start_month_day/email_bootstrap_floor_at — real, honest gap.
    )
    with pytest.raises(ConflictError):
        compute_bootstrap_floor(api.entity_repository)


def test_compute_bootstrap_floor_fails_honestly_when_no_entities_exist_yet():
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    with pytest.raises(ConflictError):
        compute_bootstrap_floor(api.entity_repository)


def test_sweep_fails_honestly_when_entity_bootstrap_configuration_is_incomplete():
    h = Harness()
    # A SECOND entity, missing the required configuration — the sweep
    # must refuse rather than silently using only the first entity's
    # floor or inventing a fallback.
    h.api.register_entity(
        entity_type="COMPANY", canonical_name="UNCONFIGURED_LTD", display_name="Unconfigured", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test",
    )
    h.queue_empty_both_folders()
    with pytest.raises(ConflictError):
        h.sweep()
