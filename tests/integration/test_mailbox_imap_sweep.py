"""CD-6 GUI-operations-foundation follow-on WO tests for the IMAP
provider wired into `services.mailbox.sweep.run_sweep` — the SAME
provider-neutral sweep engine the Microsoft provider already uses (see
`tests/integration/test_mailbox_sweep.py` for that provider's own
equivalent coverage; this file proves the identical engine behaves
correctly for the second provider, driven entirely by
`FakeImapClient` — zero real network/TLS).

Covers (per the WO's own explicit test list):
* `NOT_CONFIGURED` behaviour when credential files are absent.
* A successful connect + sweep with the fake client.
* Stage-A bounded metadata discovery: `MailboxMessage` records created,
  zero `EvidenceItem`, zero MIME-content-fetch calls to the fake
  client's content method, for an UNKNOWN-domain message.
* Zero automatic `MailboxDomainRule` creation from discovery alone.
* Zero default entity assignment anywhere for this mailbox.
* UID/UIDVALIDITY identity: re-running discovery against the SAME fake-
  client state produces no duplicate `MailboxMessage` rows.
* A `UIDVALIDITY` change between two discovery runs is a distinct epoch.
* The real client wrapper never calls a non-`.PEEK` fetch and never
  issues a mutating command (proven structurally — see
  `test_mailbox_imap_client.py::test_imap_client_never_exposes_a_mutating_method`
  and this file's own `FakeImapClient`-call-shape assertions).
"""
from __future__ import annotations

import pytest

from core.api import BagmanCanonicalAPI
from core.errors import ConflictError
from persistence.objects.memory_store import InMemoryObjectStore
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict
from services.mailbox.cursor import InMemoryMailboxFolderCursorRepository
from services.mailbox.domain_rule import InMemoryMailboxDomainRuleRepository
from services.mailbox.imap.fake_imap_client import FakeImapClient
from services.mailbox.imap.imap_adapter import ImapMailboxAdapter, compose_immutable_message_id
from services.mailbox.imap.imap_client import (
    ImapCapabilityResult,
    ImapConnectResult,
    ImapFetchContentResult,
    ImapFetchHeadersResult,
    ImapFolderInfo,
    ImapFolderListResult,
    ImapMessageHeaders,
    ImapOutcomeStatus,
    ImapSearchResult,
)
from services.mailbox.lock import InMemoryMailboxSweepLock
from services.mailbox.mailbox import PROVIDER_IMAP, InMemoryMailboxSourceRepository
from services.mailbox.message import (
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_SECURITY_REVIEW,
    InMemoryMailboxMessageRepository,
)
from services.mailbox.sweep import run_sweep
from services.mailbox.sweep_run import InMemoryMailboxSweepRunRepository, SweepFailureReason, TRIGGER_MANUAL
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


def _headers(uid: int, *, subject: str = "Invoice", sender: str = "billing@vendor.com", passing_auth: bool = True):
    raw = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": f"Vendor <{sender}>"},
        {"name": "Message-ID", "value": f"<msg-{uid}@vendor.com>"},
        {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
    ]
    if passing_auth:
        raw.append({"name": "Authentication-Results", "value": "mail.noust.ai; spf=pass; dkim=pass; dmarc=pass"})
    return ImapMessageHeaders(uid=uid, raw_headers=tuple(raw))


class Harness:
    def __init__(self, *, allow_default_domain: bool = True, configured: bool = True):
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
        self.client = FakeImapClient()

        credentials_provider = (lambda: _creds()) if configured else (lambda: None)
        self.adapter = ImapMailboxAdapter(
            client=self.client, mailbox_repository=self.mailbox_repo, credentials_provider=credentials_provider
        )

        self.mailbox = self.mailbox_repo.create_mailbox(
            display_name="Matt NoustAI", email_address="matt@noust.ai", provider_kind=PROVIDER_IMAP
        )
        # See app/api/routers/mailboxes_imap.py's own documented
        # judgment call — these "microsoft"-named repository methods
        # drive the generic, provider-neutral connection_state machine
        # and are reused as-is for IMAP.
        self.mailbox_repo.begin_microsoft_connect(self.mailbox.mailbox_id)
        self.mailbox_repo.mark_microsoft_connected(self.mailbox.mailbox_id)
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)

        source = self.api.register_source(
            source_type="EMAIL_MAILBOX", provider=self.mailbox.mailbox_id, status="ACTIVE",
            actor_type="SYSTEM", actor_id="test",
        )
        self.source_id = source.source_id

        self.entity = self.api.register_entity(
            entity_type="COMPANY", canonical_name="TEST_ENTITY", display_name="Test Entity", status="ACTIVE",
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

    def queue_discovery(self, *, folders=("INBOX",)):
        self.client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
        self.client.queue_capability_result(
            ImapCapabilityResult(status=ImapOutcomeStatus.OK, capabilities=("IMAP4rev1",))
        )
        self.client.queue_list_folders_result(
            ImapFolderListResult(status=ImapOutcomeStatus.OK, folders=tuple(ImapFolderInfo(name=f) for f in folders))
        )

    def queue_folder_round(self, *, uids=(), uidvalidity=100, headers_by_uid=None, folder="INBOX"):
        """One `fetch_folder_delta` round for ONE folder — see
        `ImapMailboxAdapter._search_for_round`'s own bootstrap-vs-delta
        branching: a fresh cursor (this harness's normal case) always
        uses the bootstrap `SINCE` search path, so only ONE queued
        search result is needed per round."""
        self.client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
        self.client.queue_search_result(ImapSearchResult(status=ImapOutcomeStatus.OK, uids=tuple(uids), uidvalidity=uidvalidity))
        if uids:
            headers = headers_by_uid or {u: _headers(u) for u in uids}
            self.client.queue_fetch_headers_result(
                ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=tuple(headers[u] for u in uids))
            )

    def queue_content(self, body: bytes = b"From: billing@vendor.com\r\nSubject: Invoice\r\n\r\nBody"):
        self.client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
        self.client.queue_fetch_content_result(ImapFetchContentResult(status=ImapOutcomeStatus.OK, content=body))

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


def _creds():
    from services.mailbox.imap.secrets import NoustAIImapCredentials

    return NoustAIImapCredentials(username="matt@noust.ai", password="pw")


def _force_auth_pass(monkeypatch):
    """PL correction (post-adversarial-review): `assess_imap_authentication`
    was rewritten to ALWAYS return UNKNOWN — no header content is trusted
    for gating yet (see `services/mailbox/imap/authentication.py`'s own
    module docstring; an earlier version trusted `Authentication-Results`
    header content whose `authserv-id` merely matched an expected
    hostname string, which is fully attacker-forgeable and was a real,
    proven security bug — `tests/integration/test_mailbox_imap_authentication.py`
    covers that fix directly).

    A handful of THIS file's own tests exist to prove the DOWNSTREAM
    sweep mechanics (evidence creation, entity-assignment doctrine,
    idempotency) work correctly for IMAP once a genuine PASS verdict
    occurs — a concern entirely separate from what that verdict should
    be for any particular header content, which is the auth module's own
    job to decide (and is deliberately unable to produce PASS at all
    right now, correctly, until a live diagnostic replaces it). This
    helper monkeypatches the sweep engine's own imported name for the
    IMAP selector to return a synthetic PASS, so those specific tests can
    keep proving the mechanics beyond the auth gate without depending on
    (or re-introducing a reason to weaken) the real selector's own
    correctly-conservative behaviour."""
    from services.mailbox.authentication_assessment import AUTH_ASSESSMENT_PASS, AuthenticationAssessment
    import services.mailbox.sweep as sweep_module

    monkeypatch.setattr(
        sweep_module,
        "assess_imap_authentication",
        lambda raw_headers: AuthenticationAssessment(
            verdict=AUTH_ASSESSMENT_PASS, reason="test-forced PASS — proving downstream mechanics only", evidence={}
        ),
    )


@pytest.fixture
def h() -> Harness:
    return Harness()


# -- NOT_CONFIGURED -----------------------------------------------------


def test_not_configured_credentials_absent_is_an_honest_config_error_never_a_crash():
    harness = Harness(configured=False)
    run = harness.sweep()
    assert run.status == "FAILED"
    assert run.error_code == SweepFailureReason.CONFIG_ERROR
    assert harness.client.connect_calls == []


# -- successful connect + sweep -----------------------------------------


def test_successful_sweep_with_fake_client_ingests_evidence(h, monkeypatch):
    _force_auth_pass(monkeypatch)  # see _force_auth_pass's own docstring
    h.queue_discovery()
    h.queue_folder_round(uids=(1,))
    h.queue_content()
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_INGESTED
    assert messages[0].immutable_provider_message_id == compose_immutable_message_id(folder="INBOX", uidvalidity=100, uid=1)
    assert messages[0].internet_message_id == "<msg-1@vendor.com>"


def test_successful_sweep_never_assigns_a_default_entity_for_review_required_rule(h, monkeypatch):
    """The WO's own explicit requirement: `matt@noust.ai` is provenance
    only, never an implicit entity assignment — a REVIEW_REQUIRED rule
    (the harness's default) must leave `entity_id` unresolved."""
    _force_auth_pass(monkeypatch)  # see _force_auth_pass's own docstring
    h.queue_discovery()
    h.queue_folder_round(uids=(1,))
    h.queue_content()
    h.sweep()
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    evidence = h.api.evidence_repository.get_evidence(messages[0].evidence_id)
    assert evidence.entity_id is None


def test_must_read_message_with_the_real_unforced_selector_goes_to_security_review_never_evidence(h):
    """The REAL, current (correctly conservative) end-to-end behavior —
    no monkeypatch. `assess_imap_authentication` always returns UNKNOWN
    right now (see `services/mailbox/imap/authentication.py`'s own
    module docstring), so a MUST_READ-domain message must route to
    SECURITY_REVIEW, never proceed to MIME/evidence, even though the
    synthetic header in `_headers()` claims a passing dmarc verdict —
    that claim is exactly the kind of unverified content this selector
    must not trust yet."""
    h.queue_discovery()
    h.queue_folder_round(uids=(1,))
    # Deliberately NOT calling h.queue_content() — MIME must never be
    # fetched for a SECURITY_REVIEW-routed message.
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 0
    assert h.client.fetch_content_calls == []

    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert messages[0].evidence_id is None

    escalations = [
        i for i in h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION)
        if i.metadata.get("mailbox_id") == h.mailbox.mailbox_id
    ]
    assert len(escalations) == 1


# -- Stage-A bounded metadata discovery ----------------------------------


def test_unknown_domain_message_creates_mailbox_message_zero_evidence_zero_mime_fetch(h):
    h.queue_discovery()
    h.queue_folder_round(uids=(1,), headers_by_uid={1: _headers(1, subject="Invoice attached", sender="new@unknown-domain.com")})
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 0
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert messages[0].discovery_candidate is True
    # Zero MIME fetch — the fake client's own content-fetch method was
    # never called for this message.
    assert h.client.fetch_content_calls == []


def test_unknown_domain_credible_candidate_raises_exactly_one_needs_you_item_never_auto_creates_a_rule(h):
    h.queue_discovery()
    h.queue_folder_round(uids=(1,), headers_by_uid={1: _headers(1, subject="Your invoice", sender="ap@newsupplier.com")})
    h.sweep()

    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status="OPEN")
    matching = [i for i in items if i.metadata.get("mailbox_id") == h.mailbox.mailbox_id]
    assert len(matching) == 1

    # Zero automatic MailboxDomainRule creation from discovery alone.
    rules = h.domain_rule_repo.list_rules(mailbox_id=h.mailbox.mailbox_id)
    rule_domains = {r.sender_domain for r in rules}
    assert "newsupplier.com" not in rule_domains


# -- idempotency / restart-safety ----------------------------------------


def test_rerunning_discovery_against_the_same_fake_state_produces_no_duplicate_messages(h, monkeypatch):
    """Simulates a process restart: re-running a sweep against a FRESH
    `FakeImapClient` queue but the SAME underlying (uidvalidity, uid)
    identity must resolve to the SAME `MailboxMessage` row, never a
    duplicate — proven here by re-queuing the identical search/fetch
    results and sweeping twice."""
    _force_auth_pass(monkeypatch)  # see _force_auth_pass's own docstring
    h.queue_discovery()
    h.queue_folder_round(uids=(1,))
    h.queue_content()
    first_run = h.sweep()
    assert first_run.evidence_created == 1

    # Simulate a process restart that lost the durable cursor (the
    # architect's own `reset_composition_for_tests()`-style scenario —
    # see this test's own docstring): a FRESH cursor repository, but the
    # SAME message repository/evidence store the first sweep already
    # populated. The bootstrap `SINCE` search re-covers the same date
    # range and re-observes the SAME (uidvalidity, uid) — idempotency
    # must hold via (mailbox_id, immutable_provider_message_id), never a
    # second `MailboxMessage`/`EvidenceItem`.
    h.cursor_repo = InMemoryMailboxFolderCursorRepository()
    h.queue_discovery()
    h.queue_folder_round(uids=(1,))
    second_run = h.sweep()
    assert second_run.evidence_created == 0
    assert second_run.duplicates == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1


# -- UIDVALIDITY epoch change --------------------------------------------


def test_uidvalidity_change_is_never_silently_collapsed_with_the_old_uid(h):
    h.queue_discovery()
    h.queue_folder_round(uids=(7,), uidvalidity=100, headers_by_uid={7: _headers(7, sender="new@unknown-domain.com")})
    h.sweep()
    first_messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(first_messages) == 1
    old_id = first_messages[0].immutable_provider_message_id

    # A NEW UIDVALIDITY epoch — UID 7 now means something DIFFERENT.
    h.queue_discovery()
    h.queue_folder_round(uids=(7,), uidvalidity=200, headers_by_uid={7: _headers(7, sender="new@unknown-domain.com")})
    h.sweep()
    all_messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(all_messages) == 2
    ids = {m.immutable_provider_message_id for m in all_messages}
    assert old_id in ids
    assert compose_immutable_message_id(folder="INBOX", uidvalidity=200, uid=7) in ids


# -- concurrency / lock reuse (provider-neutral, already-proven engine) --


def test_concurrent_sweep_declines_cleanly(h):
    h.queue_discovery()
    h.queue_folder_round(uids=())
    token = h.lock.try_acquire(h.mailbox.mailbox_id)
    try:
        with pytest.raises(Exception):
            h.sweep()
    finally:
        h.lock.release(h.mailbox.mailbox_id, token)


# -- precondition ---------------------------------------------------------


def test_precondition_rejects_a_non_connected_mailbox():
    harness = Harness()
    harness.mailbox_repo.disconnect_microsoft(harness.mailbox.mailbox_id)
    harness.mailbox = harness.mailbox_repo.get_mailbox(harness.mailbox.mailbox_id)
    with pytest.raises(ConflictError):
        harness.sweep()
