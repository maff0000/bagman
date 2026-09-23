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
    def __init__(self) -> None:
        #: Bounded call-count proof for Stage-A-only tests: a message
        #: that never leaves discovery-only handling must never reach
        #: `scan()` at all (scanning only happens downstream of a real
        #: MIME fetch, which discovery-only messages never trigger).
        self.scan_calls = 0

    def scan(self, content):
        self.scan_calls += 1
        return ScanResult(ScanVerdict.CLEAN, detail="clean")

    def is_available(self):
        return True


def _metadata(message_id: str, *, subject="Invoice", sender="billing@vendor.com", internal_date=None, extra_headers=None, label_ids=("INBOX",)):
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
        label_ids=tuple(label_ids),
        internal_date=internal_date or datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


def _genuine_trusted_gmail_headers(*, domain: str = "vendor.com", dmarc: str = "pass") -> list[dict]:
    """A sanitized, structurally-real Gmail trust-boundary header shape
    (RFC 5737 documentation IP `203.0.113.10`, Google's own real
    `mx.google.com` structural markers — never real captured data) —
    mirrors `tests/integration/test_mailbox_gmail_authentication.py`'s
    own genuine-sample shape exactly: a `Received ... by mx.google.com
    ...` hop immediately preceding a genuine `Authentication-Results:
    mx.google.com; ...` header, plus the genuine ARC triple
    (`ARC-Seal`/`ARC-Message-Signature`/`ARC-Authentication-Results`) —
    the exact header set the CD-6 metadata-contract fix added to
    `DEFAULT_METADATA_HEADERS` (see `gmail_client.py`'s own docstring).
    `ARC`/`Authentication-Results` header ORDER here is deliberately the
    same order `_metadata()` below appends `extra_headers` in — never
    reordered by anything downstream."""
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


class Harness:
    def __init__(
        self,
        *,
        allow_default_domain: bool = True,
        email: str = "mgs241171@gmail.com",
        fiscal_year_start_month_day: str = "01-01",
    ):
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
            actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day=fiscal_year_start_month_day,
        )

        if allow_default_domain:
            self.domain_rule_repo.upsert_rule(
                mailbox_id=self.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT",
                policy="MUST_READ", destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
            )

    def refresh_mailbox(self):
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)
        return self.mailbox

    def queue_discovery(self):
        """No-op — kept only for call-site compatibility across this
        file's many tests. `discover_monitored_folders` no longer calls
        `list_labels()` at all (see `gmail_adapter.py`'s own module
        docstring, "Label/folder normalisation" section) — it always
        deterministically returns the single, fixed `ALL_RECEIVED`
        stream, so there is nothing left to queue."""

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


# ---------------------------------------------------------------------
# CD-6 metadata-contract fix (DEFAULT_METADATA_HEADERS now requests
# `Received`) — MUST_READ + the REAL, unforced selector, provider-
# neutral sweep-level proof (section 3 of the fix's own WO): a
# genuinely, legitimately authenticated Gmail message from a MUST_READ
# source must PASS the real gate and reach evidence, never be forced to
# SECURITY_REVIEW merely because the metadata fetch omitted the
# selector's structurally-required trace header. The fail-closed
# counterpart proves this fix never weakens the selector's own
# conservative behaviour when a response genuinely lacks `Received`.
# ---------------------------------------------------------------------


def test_must_read_message_with_real_trusted_headers_passes_gate_and_ingests_evidence(h):
    """The metadata-contract fix's own headline proof: a MUST_READ-domain
    message carrying the REAL, structurally genuine Gmail trust-boundary
    headers (`Received ... by mx.google.com` immediately preceding a
    genuine `Authentication-Results: mx.google.com; ...dmarc=pass...`
    header) now resolves to PASS through the REAL, unforced
    `assess_gmail_authentication` selector and proceeds to evidence —
    the exact scenario the old `DEFAULT_METADATA_HEADERS` (missing
    `Received`) made structurally impossible, even for genuinely
    authenticated mail."""
    h.queue_discovery()
    h.queue_folder_round(
        message_ids=("m1",),
        metadata_by_id={"m1": _metadata("m1", extra_headers=_genuine_trusted_gmail_headers())},
    )
    h.queue_content()
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_INGESTED
    assert messages[0].evidence_id is not None


def test_must_read_message_missing_received_header_fails_closed_to_security_review(h):
    """The fail-closed regression proof this fix must NEVER weaken: a
    message whose fetched metadata genuinely lacks a `Received` header
    (whatever the reason — including, historically, exactly what the OLD
    buggy `DEFAULT_METADATA_HEADERS` produced by never requesting it)
    still correctly resolves to UNKNOWN and SECURITY_REVIEW, never PASS.
    This fix changes what is REQUESTED; it must never change what
    happens when a real response genuinely omits `Received`."""
    headers_without_received = [hdr for hdr in _genuine_trusted_gmail_headers() if hdr["name"] != "Received"]
    h.queue_discovery()
    h.queue_folder_round(
        message_ids=("m1",),
        metadata_by_id={"m1": _metadata("m1", extra_headers=headers_without_received)},
    )
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


# ---------------------------------------------------------------------
# ALL_RECEIVED discovery-model fix — provider-neutral sweep proof (WO
# section 11). Zero Gmail `MailboxDomainRule`s, one mixed `ALL_RECEIVED`
# page (Inbox + archived + Spam + Sent + Draft — the same shape as
# `test_mailbox_gmail_adapter.py::test_mixed_page_only_received_family_
# messages_returned_within_one_stream`), driven through the REAL
# `run_sweep()`.
# ---------------------------------------------------------------------


def test_all_received_stream_provider_neutral_sweep_proof_mixed_page_zero_domain_rules():
    """Proves, end-to-end through the real, unmodified provider-neutral
    sweep engine: Inbox/archived/Spam messages are all discovered as
    `MailboxMessage` rows; SENT/DRAFT never even reach discovery-only
    storage (excluded inside the adapter, before `sweep.py` ever sees
    them as a candidate message at all); the bounded, non-AI
    `evaluate_discovery_candidate` heuristic still runs normally for
    every included message; zero MIME fetch, zero `EvidenceItem`, zero
    scanner invocation, zero entity assignment; and the generic
    `sweep.py` observed-folder threading produces exactly
    `observed_folder="ALL_RECEIVED"`/`observed_folder_display_name="All
    received mail"` on every created row — with ZERO Gmail-specific
    code added to `sweep.py` itself (see `gmail_adapter.py`'s own module
    docstring's "Label/folder normalisation" section)."""
    harness = Harness(allow_default_domain=False)
    assert harness.domain_rule_repo.list_rules(mailbox_id=harness.mailbox.mailbox_id) == []

    harness.queue_discovery()
    harness.queue_folder_round(
        message_ids=("m-inbox", "m-archived", "m-spam", "m-sent", "m-draft"),
        metadata_by_id={
            "m-inbox": _metadata("m-inbox", subject="Hello", sender="alice@example.com", label_ids=("INBOX",)),
            "m-archived": _metadata("m-archived", subject="Hello", sender="bob@example.com", label_ids=()),
            "m-spam": _metadata("m-spam", subject="Hello", sender="carol@example.com", label_ids=("SPAM",)),
            "m-sent": _metadata("m-sent", subject="Hello", sender="me@example.com", label_ids=("SENT",)),
            "m-draft": _metadata("m-draft", subject="Hello", sender="me@example.com", label_ids=("DRAFT",)),
        },
    )
    run = harness.sweep()
    assert run.status == "SUCCEEDED"

    messages = harness.message_repo.list_messages(mailbox_id=harness.mailbox.mailbox_id)
    by_id = {m.immutable_provider_message_id: m for m in messages}

    # Inbox/archived/Spam discovered; SENT/DRAFT never reach storage.
    assert set(by_id.keys()) == {"m-inbox", "m-archived", "m-spam"}

    # The bounded, non-AI discovery-candidate heuristic ran for every
    # included message — never left unevaluated.
    for message in by_id.values():
        assert message.discovery_candidate is not None
        assert message.discovery_checked_at is not None

    # Generic, provider-neutral sweep.py observed-folder threading —
    # zero Gmail-specific code in sweep.py itself.
    for message in by_id.values():
        assert message.observed_folder == "ALL_RECEIVED"
        assert message.observed_folder_display_name == "All received mail"

    # Zero deep processing throughout.
    assert harness.gmail_client.raw_calls == []
    assert run.evidence_created == 0
    assert harness.scanner.scan_calls == 0
    assert harness.refresh_mailbox().default_entity_id is None


# -- Gmail `has_attachments` discovery-signal fallback (CD-6 fix) --------
#
# Real, end-to-end proof (not the unit-level `discovery_signals` tests
# above) that a Gmail message with a genuine attachment but no
# accounting-keyword subject/filename/content-type detail — the exact
# shape Gmail's Stage-A `format=metadata` fetch structurally always
# produces — is still surfaced as a discovery candidate, driven through
# the real provider-neutral sweep engine via the real header-derivation
# path (`_derive_has_attachments`), never a hand-injected boolean.


def test_gmail_message_with_only_multipart_mixed_header_is_a_candidate_via_real_header_derivation():
    """Positive: zero `MailboxDomainRule`s for this mailbox at all, a
    generic/non-financial subject, and `has_attachments=True` produced
    ENTIRELY by the real Gmail header-derivation path (a genuine
    top-level `Content-Type: multipart/mixed` header) — never a
    hand-injected `has_attachments` boolean."""
    harness = Harness(allow_default_domain=False)
    assert harness.domain_rule_repo.list_rules(mailbox_id=harness.mailbox.mailbox_id) == []

    harness.queue_discovery()
    harness.queue_folder_round(
        message_ids=("m1",),
        metadata_by_id={
            "m1": _metadata(
                "m1",
                subject="Your document",
                sender="someone@example.com",
                extra_headers=[{"name": "Content-Type", "value": "multipart/mixed; boundary=xyz"}],
            )
        },
    )
    run = harness.sweep()
    assert run.status == "SUCCEEDED"

    messages = harness.message_repo.list_messages(mailbox_id=harness.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert messages[0].discovery_candidate is True
    assert messages[0].discovery_reason == "message metadata indicates one or more attachments"

    items = harness.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status="OPEN")
    matching = [i for i in items if i.metadata.get("mailbox_id") == harness.mailbox.mailbox_id]
    assert len(matching) == 1

    # Zero deep processing throughout: no MIME/raw fetch, no evidence,
    # no scanner invocation, no entity assignment.
    assert harness.gmail_client.raw_calls == []
    assert run.evidence_created == 0
    assert messages[0].evidence_id is None
    assert harness.scanner.scan_calls == 0
    assert harness.refresh_mailbox().default_entity_id is None


def test_gmail_message_with_no_attachment_indication_at_all_is_not_a_candidate_and_raises_no_review_item():
    """Negative: identical setup, but the message genuinely carries no
    attachment indication at all (plain `text/plain`, no
    `Content-Disposition`) and no other candidate signal -> not a
    candidate, NO domain-review item raised, and the same zero-deep-
    processing guarantees as the positive case."""
    harness = Harness(allow_default_domain=False)
    harness.queue_discovery()
    harness.queue_folder_round(
        message_ids=("m1",),
        metadata_by_id={
            "m1": _metadata(
                "m1",
                subject="Your document",
                sender="someone@example.com",
                extra_headers=[{"name": "Content-Type", "value": "text/plain"}],
            )
        },
    )
    run = harness.sweep()
    assert run.status == "SUCCEEDED"

    messages = harness.message_repo.list_messages(mailbox_id=harness.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert messages[0].discovery_candidate is False

    items = harness.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status="OPEN")
    matching = [i for i in items if i.metadata.get("mailbox_id") == harness.mailbox.mailbox_id]
    assert len(matching) == 0

    assert harness.gmail_client.raw_calls == []
    assert run.evidence_created == 0
    assert messages[0].evidence_id is None
    assert harness.scanner.scan_calls == 0
    assert harness.refresh_mailbox().default_entity_id is None


def test_default_entity_id_stays_null_for_both_gmail_mailbox_rows():
    """Explicit WO-required proof: NOTHING in the Gmail router/adapter/
    composition wiring ever assigns `default_entity_id` — for either of
    the two real Gmail accounts this delivery targets."""
    repo = InMemoryMailboxSourceRepository()
    mailbox_a = repo.create_mailbox(display_name="Personal Gmail", email_address="mgs241171@gmail.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox_b = repo.create_mailbox(display_name="Work Gmail", email_address="matt.george.scott@gmail.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    assert mailbox_a.default_entity_id is None
    assert mailbox_b.default_entity_id is None


def test_operational_preflight_both_connected_gmail_mailboxes_start_with_zero_domain_rules():
    """Operational preflight (CD-6 discovery-signal fix, §7): before ANY
    historical reprocessing run against BOTH real, connected Gmail
    mailboxes (`mgs241171@gmail.com` / `matt.george.scott@gmail.com`),
    the real `MailboxDomainRuleRepository.list_rules` query must show
    ZERO rows for each — this is what makes the new `has_attachments`
    fallback signal safe to enable broadly: with no `MUST_READ`/
    `BLACKLIST` rule governing either mailbox yet, every message still
    flows through the SAME bounded, non-AI `evaluate_discovery_candidate`
    heuristic this delivery extends, never a silently-broader auto-read
    path.

    This is a TEST-LEVEL proof only, using the real repository query
    against a representative fixture (freshly created mailboxes, exactly
    mirroring `test_default_entity_id_stays_null_for_both_gmail_mailbox_
    rows` above) — it does NOT reach for, and cannot substitute for, the
    real production database. The PL is expected to run the equivalent
    live check directly against the real deployed system separately."""
    mailbox_repo = InMemoryMailboxSourceRepository()
    domain_rule_repo = InMemoryMailboxDomainRuleRepository()
    mailbox_a = mailbox_repo.create_mailbox(display_name="Personal Gmail", email_address="mgs241171@gmail.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox_b = mailbox_repo.create_mailbox(display_name="Work Gmail", email_address="matt.george.scott@gmail.com", provider_kind=PROVIDER_GOOGLE_GMAIL)

    assert domain_rule_repo.list_rules(mailbox_id=mailbox_a.mailbox_id) == []
    assert domain_rule_repo.list_rules(mailbox_id=mailbox_b.mailbox_id) == []


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


# -- single-stream discovery: no cross-label duplication is possible ------
#
# The old three-label model risked the SAME Gmail message being
# enumerated once per matching monitored label within one sweep round
# (`test_message_under_two_monitored_labels_is_discovered_exactly_once_
# not_once_per_label`, REMOVED — it tested exactly this now-impossible
# scenario: a second, separate `INBOX`/`SPAM` folder pass for the same
# message id). See `gmail_adapter.py`'s own module docstring, "Label/
# folder normalisation" section, point 4: with the single `ALL_RECEIVED`
# stream there is exactly ONE `fetch_folder_delta`/`list_messages` call
# per sweep round, so a message can never be enumerated twice within one
# round at all — proven below.


def test_all_received_stream_discovers_a_message_exactly_once_via_a_single_list_messages_call(h):
    metadata = _metadata("single-pass-message", subject="Invoice", sender="new@unknown-domain.com")
    h.queue_discovery()
    h.queue_folder_round(message_ids=("single-pass-message",), metadata_by_id={"single-pass-message": metadata})

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert run.duplicates == 0
    # Exactly one `list_messages` call this whole sweep round — one
    # stream, one cursor, never a per-label loop.
    assert len(h.gmail_client.list_messages_calls) == 1
    assert h.gmail_client.metadata_calls.count("single-pass-message") == 1


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


# ---------------------------------------------------------------------
# CD-6 metadata-contract fix — historical-candidate reprocessing (section
# 4 of the fix's own WO, the architect's own highest-priority scenario:
# "A real Gmail historical candidate must not be forced to
# SECURITY_REVIEW merely because the refresh path omitted the
# selector's trusted trace header."). `reprocess_all_historical_
# candidates_for_domain` -> `_reprocess_one_message` performs a fresh,
# bounded, METADATA-ONLY `adapter.fetch_message_headers()` refresh
# BEFORE the authentication gate runs (real production call shape — see
# `services/mailbox/sweep.py`'s own module docstring) — this refresh now
# carries `Received` (this fix), so a genuinely authenticated historical
# candidate reaches a real dmarc=pass verdict instead of being forced to
# UNKNOWN. The fail-closed counterpart proves the historical path is
# equally honest when the refresh genuinely still lacks `Received`.
# ---------------------------------------------------------------------


def test_historical_reprocess_with_fixed_metadata_headers_reaches_real_dmarc_pass():
    """A Gmail historical candidate (discovered as `CHECKED_NOT_CANDIDATE`
    before its domain was `MUST_READ`) is promoted by an operator to
    `MUST_READ`. The resulting back-processing refresh
    (`adapter.fetch_message_headers()`) now returns headers that include
    `Received` (thanks to the `DEFAULT_METADATA_HEADERS` fix), so the
    REAL, unforced `assess_gmail_authentication` selector reaches
    dmarc=pass and the message proceeds all the way to evidence — never
    forced to SECURITY_REVIEW merely because the refresh path omitted
    the trace header."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.queue_discovery()
    h.queue_folder_round(
        message_ids=("m1",),
        metadata_by_id={"m1": _metadata("m1", subject="Your invoice", sender="ap@newsupplier.com")},
    )
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="newsupplier.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    # The fresh headers-only refresh `_reprocess_one_message` performs —
    # simulating exactly what Gmail's real API returns for the FIXED
    # `DEFAULT_METADATA_HEADERS` set (`Received` now included).
    h.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=_metadata(
                "m1", subject="Your invoice", sender="ap@newsupplier.com",
                extra_headers=_genuine_trusted_gmail_headers(domain="newsupplier.com"),
            ),
        )
    )
    h.queue_content(body=b"From: ap@newsupplier.com\r\nSubject: Your invoice\r\n\r\nBody")

    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="newsupplier.com",
        rule=rule, mailbox_domain_rule_repository=h.domain_rule_repo, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo,
        api=h.api, object_store=h.object_store, scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    assert reprocessed[0].ingestion_status == INGESTION_STATUS_INGESTED
    assert reprocessed[0].evidence_id is not None
    # Metadata fetched twice: once during original Stage-A discovery,
    # once as the historical-reprocessing refresh — then, and only after
    # a PASS verdict, exactly one MIME fetch.
    assert h.gmail_client.metadata_calls.count("m1") == 2
    assert h.gmail_client.raw_calls == ["m1"]


def test_historical_reprocess_still_fails_closed_when_refreshed_headers_omit_received():
    """The historical-reprocessing fail-closed regression proof: if the
    refreshed headers genuinely still lack `Received` (for any reason),
    the selector correctly returns UNKNOWN and the message is marked
    SECURITY_REVIEW, never silently proceeded as PASS. This must keep
    working exactly as before this fix — the fix changes what is
    REQUESTED, never what happens when a real response genuinely omits
    `Received`."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.queue_discovery()
    h.queue_folder_round(
        message_ids=("m1",),
        metadata_by_id={"m1": _metadata("m1", subject="Your invoice", sender="ap@newsupplier.com")},
    )
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="newsupplier.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    headers_without_received = [
        hdr for hdr in _genuine_trusted_gmail_headers(domain="newsupplier.com") if hdr["name"] != "Received"
    ]
    h.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=_metadata(
                "m1", subject="Your invoice", sender="ap@newsupplier.com", extra_headers=headers_without_received,
            ),
        )
    )
    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="newsupplier.com",
        rule=rule, mailbox_domain_rule_repository=h.domain_rule_repo, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo,
        api=h.api, object_store=h.object_store, scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    assert reprocessed[0].ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert reprocessed[0].evidence_id is None
    assert h.gmail_client.raw_calls == []  # never MIME-fetched
    escalation_items = [
        i for i in h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION)
        if i.metadata.get("mailbox_id") == h.mailbox.mailbox_id
    ]
    assert len(escalation_items) == 1


# ---------------------------------------------------------------------
# Effective-rule scoping x Gmail quota pacing regression — the new
# per-candidate `find_for_sender` re-resolution must never trigger the
# proactive `_GmailMessagesGetGovernor` pacing for a candidate that
# effective-rule filtering skips (see
# `tests/integration/test_mailbox_gmail_adapter.py`'s own dedicated
# governor test section for the governor's direct unit coverage; this
# proves the INTEGRATION invariant — the governor is only ever consulted
# for messages that actually reach `adapter.fetch_message_headers`).
# ---------------------------------------------------------------------


class _FakeMonotonicClock:
    """Deterministic stand-in for `time.monotonic` (mirrors
    `test_mailbox_gmail_adapter.py`'s own identical fixture) — only ever
    advances via `_FakeSleep.__call__`, never on its own, so this test
    proves the exact NUMBER of governor `wait()` calls with zero real
    sleeping."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSleep:
    def __init__(self, clock: _FakeMonotonicClock) -> None:
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


def test_reprocess_gmail_quota_governor_paces_only_actually_processed_candidates_never_skipped_ones():
    """Two historical candidates from `newsupplier.com`
    (`keep@newsupplier.com`, `skip@newsupplier.com`) are discovered
    while the domain is ungoverned — TWO `fetch_message_metadata` calls
    during discovery, so the shared governor's `wait()` is called twice
    (the second one sleeps, since the fake clock never advances on its
    own). A more-specific BLACKLIST/EXACT_ADDRESS override is then set
    on `skip@newsupplier.com`, and `newsupplier.com` MUST_READ/EXACT is
    approved. Only `keep@newsupplier.com` is eligible after effective-
    rule filtering, so exactly TWO further governor `wait()` invocations
    occur for it (its own fresh headers refresh, THEN its own raw MIME
    fetch — `fetch_message_content` is now governed too, CD-6 quota-
    scaling follow-on fix) — never any call at all for the skipped
    candidate. Total: 4 governor `wait()` invocations -> exactly 3
    sleeps (the first invocation for this mailbox_id never sleeps)."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    clock = _FakeMonotonicClock()
    sleep = _FakeSleep(clock)
    # Replace the Harness's own adapter with an identically-configured
    # one carrying an injectable fake clock/sleep — same collaborators
    # (oauth_client/gmail_client/token_store/mailbox_repository)
    # otherwise, so nothing else about the harness's behaviour changes.
    h.adapter = GmailMailboxAdapter(
        oauth_client=h.oauth_client, gmail_client=h.gmail_client, token_store=h.token_store,
        mailbox_repository=h.mailbox_repo, monotonic_fn=clock, sleep_fn=sleep,
    )

    h.queue_discovery()
    h.queue_folder_round(
        message_ids=("keep-1", "skip-1"),
        metadata_by_id={
            "keep-1": _metadata("keep-1", subject="Your invoice", sender="keep@newsupplier.com"),
            "skip-1": _metadata("skip-1", subject="Your invoice", sender="skip@newsupplier.com"),
        },
    )
    h.sweep()
    # Two sequential metadata fetches during discovery -> the SECOND one
    # sleeps (fake clock hasn't moved on its own); the first never does.
    assert sleep.calls == pytest.approx([0.30])

    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="newsupplier.com", match_mode="EXACT_ADDRESS",
        policy="BLACKLIST", destination_entity_id=None, destination_mode=None, source="OPERATOR",
        sender_address="skip@newsupplier.com",
    )
    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="newsupplier.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    # Only ONE headers-refresh result queued — if the skipped candidate
    # wrongly reached the adapter too, this test would fail with an
    # empty-queue error rather than silently passing.
    h.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=_metadata(
                "keep-1", subject="Your invoice", sender="keep@newsupplier.com",
                extra_headers=_genuine_trusted_gmail_headers(domain="newsupplier.com"),
            ),
        )
    )
    h.queue_content(body=b"From: keep@newsupplier.com\r\nSubject: Your invoice\r\n\r\nBody")

    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="newsupplier.com",
        rule=rule, mailbox_domain_rule_repository=h.domain_rule_repo, adapter=h.adapter,
        message_repository=h.message_repo, needs_you_repository=h.needs_you_repo,
        api=h.api, object_store=h.object_store, scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert [m.immutable_provider_message_id for m in reprocessed] == ["keep-1"]
    assert reprocessed[0].ingestion_status == INGESTION_STATUS_INGESTED

    skip_msg = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "skip-1")
    assert skip_msg.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE

    # Exactly 4 total governor `wait()` invocations for this mailbox_id
    # (2 discovery + 1 reprocess-headers-refresh + 1 reprocess-raw-fetch,
    # both for `keep-1` only) -> exactly 3 sleeps. A 4th sleep entry
    # would mean the governor was wrongly consulted a 5th time — i.e.
    # for the skipped candidate.
    assert sleep.calls == pytest.approx([0.30, 0.30, 0.30])


def test_resolve_domain_review_allow_resume_after_interrupted_backfill_only_paces_governor_for_candidates_actually_processed_in_the_retry(monkeypatch):
    """CD-6 GUI-operations-foundation follow-on WO — resumable ALLOW
    MUST_READ historical backfill (`services.mailbox.review_resolution
    .resolve_domain_review`), Gmail-specific regression: the shared
    `_GmailMessagesGetGovernor` must only ever be consulted for
    candidates the RETRY call actually (re-)processes, never for a
    candidate that already reached a final state on an earlier attempt.

    Two candidates (`gm1`, `gm2`) from an unknown domain are discovered
    (2 governor `wait()` calls). The domain is then approved via
    `resolve_domain_review` ALLOW: `gm1`'s own fresh headers refresh
    succeeds (a 3rd governor `wait()`) and its own raw MIME fetch
    succeeds too (a 4th governor `wait()` — `fetch_message_content` is
    now governed identically to `fetch_message_headers`, CD-6 quota-
    scaling follow-on fix), fully ingesting it; `gm2`'s own fresh headers
    refresh then hits a genuine provider error (a 5th governor `wait()`
    — the governor is consulted BEFORE the underlying API call,
    regardless of the call's own outcome), which propagates out of
    `resolve_domain_review` uncaught — `gm2` never reaches its own
    content fetch at all this attempt. An identical retry resumes ONLY
    `gm2` — `gm1` is no longer `CHECKED_NOT_CANDIDATE` and is never
    returned by the candidate query at all, so it can never reach
    `fetch_message_headers`/`fetch_message_content`/the governor a
    second time. Exactly TWO more governor `wait()` invocations (a 6th
    and 7th — gm2's own headers refresh, then its own content fetch)
    occur on retry."""
    from datetime import datetime as _datetime, timezone as _timezone

    from core.errors import ConflictError as _ConflictError
    from services.mailbox.review_resolution import resolve_domain_review

    h = Harness(allow_default_domain=False)
    clock = _FakeMonotonicClock()
    sleep = _FakeSleep(clock)
    h.adapter = GmailMailboxAdapter(
        oauth_client=h.oauth_client, gmail_client=h.gmail_client, token_store=h.token_store,
        mailbox_repository=h.mailbox_repo, monotonic_fn=clock, sleep_fn=sleep,
    )

    domain = "quota-resume.example"
    h.queue_discovery()
    h.queue_folder_round(
        message_ids=("gm1", "gm2"),
        metadata_by_id={
            "gm1": _metadata("gm1", subject="Invoice", sender=f"billing@{domain}", internal_date=_datetime(2024, 1, 1, 12, 0, tzinfo=_timezone.utc)),
            "gm2": _metadata("gm2", subject="Invoice", sender=f"billing@{domain}", internal_date=_datetime(2024, 1, 1, 12, 1, tzinfo=_timezone.utc)),
        },
    )
    h.sweep()
    # Discovery: 2 sequential metadata fetches -> the SECOND one sleeps.
    assert sleep.calls == pytest.approx([0.30])

    items = [
        i for i in h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
        if i.metadata.get("mailbox_id") == h.mailbox.mailbox_id
    ]
    assert len(items) == 1
    item = items[0]

    # gm1's own fresh headers refresh — genuine, trusted PASS.
    h.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=_metadata("gm1", subject="Invoice", sender=f"billing@{domain}", extra_headers=_genuine_trusted_gmail_headers(domain=domain)),
        )
    )
    h.queue_content(body=f"From: billing@{domain}\r\nSubject: Invoice\r\n\r\nBody".encode())
    # gm2's own fresh headers refresh — a genuine provider error (still
    # consumes the governor's own `wait()` first, per
    # `GmailMailboxAdapter.fetch_message_headers`'s own ordering).
    h.gmail_client.queue_metadata_result(GmailMessageMetadataResult(status=GmailOutcomeStatus.PERMISSION_ERROR, error_detail="simulated"))

    kwargs = dict(
        needs_you_repository=h.needs_you_repo, mailbox_message_repository=h.message_repo,
        mailbox_domain_rule_repository=h.domain_rule_repo, entity_repository=h.api.entity_repository,
        api=h.api, object_store=h.object_store, scanner=h.scanner, adapter=h.adapter, mailbox=h.mailbox,
        mailbox_id=h.mailbox.mailbox_id, mailbox_source_id=h.source_id, item_id=item.item_id,
        actor_type="SYSTEM", actor_id="test", decision="ALLOW", destination_entity_id=None,
        destination_mode="REVIEW_REQUIRED", match_mode="EXACT", processor_hint=None, sender_address=None,
    )
    with pytest.raises(_ConflictError):
        resolve_domain_review(**kwargs)

    # 2 discovery waits + 3 reprocess waits (gm1 headers, gm1 content,
    # gm2 headers failed attempt) = 5 total -> 4 sleeps (first-ever call
    # never sleeps).
    assert sleep.calls == pytest.approx([0.30, 0.30, 0.30, 0.30])
    refreshed_item = h.needs_you_repo.get_needs_you_item(item.item_id)
    assert refreshed_item.status == "OPEN"
    gm1_msg = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "gm1")
    assert gm1_msg.ingestion_status == INGESTION_STATUS_INGESTED
    gm2_msg = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "gm2")
    assert gm2_msg.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE

    # Retry — resumes ONLY gm2; gm1 must never touch the governor again.
    h.gmail_client.queue_metadata_result(
        GmailMessageMetadataResult(
            status=GmailOutcomeStatus.OK,
            metadata=_metadata("gm2", subject="Invoice", sender=f"billing@{domain}", extra_headers=_genuine_trusted_gmail_headers(domain=domain)),
        )
    )
    h.queue_content(body=f"From: billing@{domain}\r\nSubject: Invoice\r\n\r\nBody".encode())

    result = resolve_domain_review(**kwargs)

    assert [m["mailbox_message_id"] for m in result["reprocessed_messages"]] == [gm2_msg.mailbox_message_id]
    assert result["needs_you_item"]["status"] == "RESOLVED"
    # Exactly TWO more governor wait() invocations during the retry (a
    # 5th and 6th sleep — gm2's own headers refresh, then its own
    # content fetch) — never a 7th, which would mean gm1 was wrongly
    # re-consulted.
    assert sleep.calls == pytest.approx([0.30, 0.30, 0.30, 0.30, 0.30, 0.30])


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


# ---------------------------------------------------------------------
# CD-6 fix — RATE_LIMITED page retry must retry the EXACT SAME logical
# page request (see `services/mailbox/sweep.py`'s own retry-block
# comment). These two tests prove the PRECISE Gmail exploit the bug
# caused: the old code hardcoded `delta_link=None, bootstrap_timestamp=
# None` on retry, so `GmailMailboxAdapter.fetch_folder_delta` (see its
# own module docstring's "since_epoch resolution" section) resolved
# `since_epoch=0` whenever BOTH were lost — issuing an UNBOUNDED
# `list_messages(query=None)` call that would enumerate the mailbox's
# entire lifetime instead of the governed bootstrap floor / delta
# cursor. Driven end-to-end through the real, provider-neutral
# `run_sweep()` (not the adapter in isolation) because the retry itself
# is `run_sweep`'s own loop's responsibility — `GmailMailboxAdapter`
# has no retry logic of its own; a rate limit simply passes its status
# straight through (see `fetch_folder_delta`'s `list_result.status !=
# GmailOutcomeStatus.OK` branch). Testing at the adapter level alone
# could not exercise the actual bug or its fix.
# ---------------------------------------------------------------------


def test_429_rate_limited_bootstrap_first_page_preserves_after_bound_on_retry_gmail_exploit():
    """Bootstrap first page (fresh Gmail mailbox, no cursor yet) hits a
    429, then succeeds on retry. Both underlying `list_messages` calls
    must carry the identical `after:<epoch>` bound, derived from the
    SAME bootstrap timestamp — never a `query=None` unbounded call.

    `fiscal_year_start_month_day="11-01"` derives the exact, real,
    documented worked example in
    `services/mailbox/bootstrap_policy.py`'s own module docstring
    ("Infosecurs Limited"): historical bootstrap =
    `datetime(2024, 11, 1, tzinfo=timezone.utc)`.
    """
    from datetime import datetime, timezone

    h = Harness(fiscal_year_start_month_day="11-01")
    h.queue_discovery()
    h.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))
    run = h.sweep()
    assert run.status == "SUCCEEDED"

    calls = h.gmail_client.list_messages_calls
    assert len(calls) == 2
    expected_epoch = int(datetime(2024, 11, 1, tzinfo=timezone.utc).timestamp())
    expected_query = f"after:{expected_epoch}"
    assert calls[0].query == expected_query
    assert calls[1].query == expected_query
    # The exact regression this bug caused: neither call fell back to an
    # unbounded (query=None) lifetime enumeration.
    assert all(call.query is not None for call in calls)


def test_429_rate_limited_existing_cursor_preserves_after_bound_on_retry_gmail_exploit():
    """Incremental (non-bootstrap) round: a synthetic existing
    `delta_link` encoding `{"since_epoch": X}` (the adapter's own real
    `_encode_delta_link` format) hits a 429 on its first page, then
    succeeds on retry. Both underlying `list_messages` calls must carry
    `after:X` — the SAME `X` from the existing cursor — never falling
    back to an unbounded/lifetime query."""
    from datetime import datetime, timezone

    from services.mailbox.gmail.gmail_adapter import GMAIL_ALL_RECEIVED_STREAM_ID, _encode_delta_link

    h = Harness()
    since_epoch_x = int(datetime(2025, 3, 15, tzinfo=timezone.utc).timestamp())
    h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=PROVIDER_GOOGLE_GMAIL, folder=GMAIL_ALL_RECEIVED_STREAM_ID,
        bootstrap_timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    h.cursor_repo.advance_cursor(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=PROVIDER_GOOGLE_GMAIL, folder=GMAIL_ALL_RECEIVED_STREAM_ID,
        delta_link=_encode_delta_link(since_epoch=since_epoch_x),
    )

    h.queue_discovery()
    h.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.OK, message_ids=()))
    run = h.sweep()
    assert run.status == "SUCCEEDED"

    calls = h.gmail_client.list_messages_calls
    assert len(calls) == 2
    expected_query = f"after:{since_epoch_x}"
    assert calls[0].query == expected_query
    assert calls[1].query == expected_query
    assert all(call.query is not None for call in calls)


# ---------------------------------------------------------------------
# CD-6 quota-classification fix — connection-state invariant.
#
# The headline production incident this delivery fixes: a real Gmail
# historical sweep once hit Google's own per-user quota mid-round, which
# the OLD `_status_for_http_error` misclassified as `PERMISSION_ERROR`
# (ALL 403s, regardless of body) — causing `services/mailbox/sweep.py`'s
# own `if page.status == GraphOutcomeStatus.PERMISSION_ERROR:
# adapter.report_connection_error(...)` branch to mark a genuinely
# healthy mailbox's `connection_state -> ERROR` over nothing more than
# transient quota exhaustion. Now that `gmail_client.py`'s own fix routes
# a quota-shaped 403 to `RATE_LIMITED` instead, this scenario is
# equivalent to Gmail's plain `429` case — the run terminalizes
# PARTIAL/FAILED after `sweep.py`'s existing bounded single-retry-then-
# fail policy is exhausted, but the mailbox itself is NEVER touched.
# ---------------------------------------------------------------------


def test_quota_rate_limited_exhausted_retry_never_poisons_connection_state():
    """12. Two consecutive `RATE_LIMITED` page results (the shape a
    quota-exhaustion 403, correctly classified by the CD-6 fix, now
    surfaces as — exactly mirroring the existing plain-429-exhausted
    exploit tests above) exhaust `sweep.py`'s own bounded single retry.
    Headline proof: the `MailboxSource` remains `CONNECTED` — queried
    directly from the repository, never merely inferred from the run's
    own fields. Also asserts the run terminalizes PARTIAL/FAILED, the
    folder cursor never advances (`delta_link` stays `None`), and zero
    `EvidenceItem`/zero `MailboxMessage` deep processing occurred."""
    from services.mailbox.gmail.gmail_adapter import GMAIL_ALL_RECEIVED_STREAM_ID

    h = Harness()
    h.queue_discovery()
    h.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.gmail_client.queue_list_messages_result(GmailMessageListPageResult(status=GmailOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))

    run = h.sweep()

    # The run itself terminalizes PARTIAL/FAILED — never SUCCEEDED — once
    # the bounded retry is exhausted.
    assert run.status in ("PARTIAL", "FAILED")

    # Headline proof: the mailbox's own `connection_state` was NEVER
    # touched by a RATE_LIMITED outcome — queried directly from the
    # repository, not inferred from the run.
    assert h.mailbox_repo.get_mailbox(h.mailbox.mailbox_id).connection_state == "CONNECTED"

    # The cursor was never advanced — no delta round ever succeeded.
    cursor = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=PROVIDER_GOOGLE_GMAIL, folder=GMAIL_ALL_RECEIVED_STREAM_ID,
        bootstrap_timestamp=h.mailbox_repo.get_mailbox(h.mailbox.mailbox_id).created_at,
    )
    assert cursor.delta_link is None

    # Zero deep processing: no MailboxMessage rows, no EvidenceItem rows.
    assert h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id) == []
    assert h.api.evidence_repository.list_evidence() == []
