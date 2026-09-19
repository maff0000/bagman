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
from services.mailbox.domain_rule import DESTINATION_MODE_FIXED, InMemoryMailboxDomainRuleRepository
from services.mailbox.lock import InMemoryMailboxSweepLock, MailboxSweepLockError
from services.mailbox.mailbox import PROVIDER_MICROSOFT_GRAPH, InMemoryMailboxSourceRepository
from services.mailbox.message import (
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    INGESTION_STATUS_SECURITY_REVIEW,
    INGESTION_STATUS_VANISHED,
    InMemoryMailboxMessageRepository,
)
from services.mailbox.microsoft.adapter import MicrosoftGraphMailboxAdapter
from services.mailbox.microsoft.fake_client import FakeMicrosoftGraphClient, FakeMicrosoftOAuthClient
from services.mailbox.microsoft.graph_client import (
    EXCLUDED_WELL_KNOWN_FOLDER_NAMES,
    GraphDeltaPageResult,
    GraphFolderListResult,
    GraphFolderSummary,
    GraphMessageContentResult,
    GraphMessageHeadersResult,
    GraphMessageSummary,
    GraphOutcomeStatus,
    GraphWellKnownFoldersResult,
    MicrosoftTokenResult,
)
from services.mailbox.microsoft.secrets import InMemoryMicrosoftTokenStore
from services.mailbox.sweep import run_sweep
from services.mailbox.sweep_run import InMemoryMailboxSweepRunRepository, SweepFailureReason, TRIGGER_MANUAL
from services.needs_you.needs_you import (
    ITEM_TYPE_COMPANY_REQUIRED,
    ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
    InMemoryNeedsYouRepository,
)


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


#: CD-6 GUI-operations-foundation follow-on WO — the per-message
#: authentication check (`services.mailbox.sweep.evaluate_message_authentication`)
#: escalates a message with NO usable trusted header at all. Every
#: EXISTING test in this file that exercises the ordinary MUST_READ
#: MIME-fetch-and-evidence path is proving something else entirely (the
#: sweep engine's own idempotency/cursor/quarantine machinery) and must
#: keep passing exactly as before, so `_msg()` now defaults to a
#: PASSING signal set (synthesised into a real, trusted
#: `Authentication-Results` header carrying `compauth=pass`, the real
#: live-diagnostic shape — see
#: `services.mailbox.microsoft.authentication`'s own module docstring);
#: dedicated new tests further down override this explicitly (via
#: `raw_headers=`) to exercise the selector/escalation path itself.
_PASSING_AUTH_SIGNALS = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}


def _auth_results_header(auth_signals) -> dict:
    """Synthesise ONE real-shaped `Authentication-Results` header value
    from a flat `{"spf": ..., "dkim": ..., "dmarc": ...}` dict — mirrors
    the exact real, live, redacted diagnostic shape this WO's own PID
    captured (`spf=... dkim=... dmarc=... compauth=... reason=...`).
    `compauth` is derived from `dmarc` (pass -> pass, fail -> fail,
    anything else -> none) — a reasonable, deterministic test-fixture
    convention, not itself part of any production code path."""
    dmarc = auth_signals.get("dmarc")
    compauth = "pass" if dmarc == "pass" else ("fail" if dmarc == "fail" else "none")
    parts = []
    for mechanism in ("spf", "dkim", "dmarc"):
        value = auth_signals.get(mechanism)
        if value is not None:
            parts.append(f"{mechanism}={value}")
    parts.append(f"compauth={compauth} reason=100")
    return {"name": "Authentication-Results", "value": "; ".join(parts)}


def _msg(
    msg_id: str,
    *,
    received_at=None,
    subject="Invoice",
    sender_address="billing@vendor.com",
    attachment_metadata=(),
    auth_signals=None,
    raw_headers=None,
) -> GraphMessageSummary:
    """`raw_headers`, when explicitly supplied, drives the REAL
    authentication gate directly (a list of `{"name": ..., "value": ...}`
    dicts, exactly Graph's own `internetMessageHeaders` shape) — use this
    for any test constructing a specific/adversarial header scenario.
    Otherwise `auth_signals` (a flat spf/dkim/dmarc dict, defaulting to a
    real passing set) is synthesised into one trusted header via
    `_auth_results_header` — this keeps every pre-existing, non-auth-
    focused test in this file working unchanged."""
    resolved_auth_signals = dict(auth_signals) if auth_signals is not None else dict(_PASSING_AUTH_SIGNALS)
    if raw_headers is not None:
        resolved_raw_headers = tuple(raw_headers)
    elif not resolved_auth_signals:
        # An explicit empty dict means "no signal captured at all" —
        # the real shape for that is NO header present whatsoever.
        resolved_raw_headers = ()
    else:
        resolved_raw_headers = (_auth_results_header(resolved_auth_signals),)
    return GraphMessageSummary(
        immutable_id=msg_id,
        internet_message_id=f"<{msg_id}@example.com>",
        subject=subject,
        sender_address=sender_address,
        sender_display_name="Vendor",
        received_at=received_at or datetime.now(timezone.utc),
        has_attachments=bool(attachment_metadata),
        attachment_metadata=tuple(attachment_metadata),
        auth_signals=resolved_auth_signals,
        raw_headers=resolved_raw_headers,
    )


def _content(body: bytes = b"From: billing@vendor.com\r\nSubject: Invoice\r\n\r\nBody") -> GraphMessageContentResult:
    return GraphMessageContentResult(status=GraphOutcomeStatus.OK, content=body)


#: CD-6 GUI-operations-foundation follow-on WO (item C) — historical
#: back-processing (`_reprocess_one_message`) now fetches FRESH headers
#: before ever fetching MIME; every test that drives it must queue a
#: headers result too. A passing (real, trusted, `compauth=pass`) header
#: by default — tests exercising the historical FAIL/UNKNOWN split queue
#: their own explicit `GraphMessageHeadersResult` instead.
def _headers_ok(auth_signals=None) -> GraphMessageHeadersResult:
    resolved = dict(auth_signals) if auth_signals is not None else dict(_PASSING_AUTH_SIGNALS)
    return GraphMessageHeadersResult(status=GraphOutcomeStatus.OK, raw_headers=(_auth_results_header(resolved),))


#: The default sender domain `_msg()` uses — a real `ALLOWED`
#: `MailboxDomainRule` is set up for it in `Harness.__init__` (CD-6
#: architect amendment — two-stage mail processing means a sweep no
#: longer ingests unconditionally; most of this file's pre-existing
#: coverage is proving ALLOWED-path behaviour, so the rule is set up
#: once, centrally, exactly like a real prior operator approval would
#: be). Tests that specifically exercise the domain GATE itself use a
#: different, deliberately ungoverned domain instead.
_DEFAULT_ALLOWED_DOMAIN = "vendor.com"


#: Deliberately kept to exactly Inbox + Junk Email — matches the OLD
#: (pre-folder-expansion) monitored set — so the bulk of this file's
#: EXISTING coverage below needs zero changes to its own delta-queue
#: call counts/ordering. Dedicated new tests further down (see "CD-6
#: architect amendment — recursive folder discovery") opt into the
#: FULL monitored set (Deleted Items, nested/hidden/custom folders, the
#: Sent/Drafts/Outbox exclusion) via `h.sweep(folders=..., well_known_ids=...)`.
INBOX_FOLDER_ID = "AAMkADinbox000000000000000000000"
JUNK_FOLDER_ID = "AAMkADjunkemail0000000000000000"
DEFAULT_MONITORED_FOLDERS = (
    GraphFolderSummary(folder_id=INBOX_FOLDER_ID, display_name="Inbox", parent_folder_id=None, child_folder_count=0),
    GraphFolderSummary(
        folder_id=JUNK_FOLDER_ID, display_name="Junk Email", parent_folder_id=None, child_folder_count=0
    ),
)
DEFAULT_WELL_KNOWN_IDS = {"inbox": INBOX_FOLDER_ID, "junkemail": JUNK_FOLDER_ID}


class Harness:
    INBOX_FOLDER_ID = INBOX_FOLDER_ID
    JUNK_FOLDER_ID = JUNK_FOLDER_ID
    DEFAULT_MONITORED_FOLDERS = DEFAULT_MONITORED_FOLDERS

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
        )

        if allow_default_domain:
            self.domain_rule_repo.upsert_rule(
                mailbox_id=self.mailbox.mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN, match_mode="EXACT",
                policy="MUST_READ", destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
            )

    def refresh_mailbox(self):
        self.mailbox = self.mailbox_repo.get_mailbox(self.mailbox.mailbox_id)
        return self.mailbox

    def _queue_folder_discovery(self, *, folders=None, well_known_ids=None):
        """CD-6 architect amendment (recursive folder discovery) —
        `run_sweep` now calls `adapter.discover_monitored_folders` ONCE
        before iterating any folder; queue that discovery result here so
        every existing/new test's own `sweep()` call continues to "just
        work" without hand-queuing it individually. Uses a SEPARATE Fake
        queue from delta/content (see `FakeMicrosoftGraphClient`'s own
        docstring) — never interferes with a test's own queued delta/
        content results."""
        resolved_folders = folders if folders is not None else self.DEFAULT_MONITORED_FOLDERS
        resolved_well_known = well_known_ids if well_known_ids is not None else DEFAULT_WELL_KNOWN_IDS
        self.graph_client.queue_folder_list_result(
            GraphFolderListResult(status=GraphOutcomeStatus.OK, folders=tuple(resolved_folders))
        )
        self.graph_client.queue_well_known_folders_result(
            GraphWellKnownFoldersResult(status=GraphOutcomeStatus.OK, folder_ids=dict(resolved_well_known))
        )

    def sweep(self, *, folders=None, well_known_ids=None):
        self._queue_folder_discovery(folders=folders, well_known_ids=well_known_ids)
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
    assert {f['folder_id'] for f in run.folders_attempted} == {h.INBOX_FOLDER_ID, h.JUNK_FOLDER_ID}
    assert {f['display_name'] for f in run.folders_attempted} == {"Inbox", "Junk Email"}


def test_a_new_message_is_ingested_as_real_evidence(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_headers_result(_headers_ok())
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
    h.graph_client.queue_headers_result(_headers_ok())
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
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d-junk"))
    run = h.sweep()
    assert run.evidence_created == 1
    assert run.duplicates == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].observed_folder == h.JUNK_FOLDER_ID  # last-seen wins


def test_idempotent_replay_of_a_whole_sweep_creates_no_new_evidence(h):
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_headers_result(_headers_ok())
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
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=h.INBOX_FOLDER_ID,
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
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=h.INBOX_FOLDER_ID,
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    assert cursor.delta_link is None  # never advanced past the failure

    # Second sweep (retry): the bootstrap boundary is used again since
    # the cursor never advanced — the SAME message is safely re-seen
    # and this time succeeds.
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d2"))
    h.graph_client.queue_headers_result(_headers_ok())
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
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.quarantined == 1
    assert run.evidence_created == 0
    cursor = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=h.INBOX_FOLDER_ID,
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
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=h.INBOX_FOLDER_ID,
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
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d2"))
    h.sweep()
    events = [e for e in h.api.audit_repository.list_by_subject("EvidenceItem", h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0].evidence_id) if e.event_type == "EMAIL_EVIDENCE_INGESTED"]
    assert len(events) == 1
    assert "mailbox_id" in events[0].payload


def test_quarantined_message_emits_email_evidence_quarantined_audit_event():
    h = Harness(scanner=AlwaysMaliciousScanner())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_headers_result(_headers_ok())
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
    h.graph_client.queue_headers_result(_headers_ok())
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
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="BLACKLIST",
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
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
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
# reprocess_all_historical_candidates_for_domain (architect spec §4 +
# operational addendum, ahead of the first real large historical sweep)
# ---------------------------------------------------------------------


def test_reprocess_all_historical_candidates_ingests_the_triggering_message_immediately():
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()
    triggering_message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert triggering_message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    assert reprocessed[0].mailbox_message_id == triggering_message.mailbox_message_id
    assert reprocessed[0].ingestion_status == "INGESTED"
    assert reprocessed[0].evidence_id is not None
    assert h.graph_client.content_calls == ["m1"]


def test_historical_reprocess_fixed_destination_assigns_real_entity_id_to_evidence():
    """Regression, already existed, reconfirmed here explicitly for the
    HISTORICAL back-process path (mirrors
    `test_must_read_fixed_destination_assigns_real_entity_id_to_evidence`'s
    own identical proof for the ORDINARY live-sweep path): a FIXED-
    destination rule's real `destination_entity_id` is threaded through
    to the resulting `EvidenceItem.entity_id` at registration time when a
    historical candidate is back-processed, not merely when it is swept
    live."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=h.entity.entity_id, destination_mode="FIXED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    evidence = h.api.get_evidence(reprocessed[0].evidence_id)
    assert evidence.entity_id == h.entity.entity_id


def test_reprocess_all_historical_candidates_is_idempotent_on_a_double_submit_of_the_whole_domain():
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    first = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(first) == 1
    # A second call for the SAME domain (e.g. a genuine double-submit of
    # the same ALLOW decision) must NOT fetch content again, and must
    # find NO remaining eligible candidates at all — the one historical
    # message is no longer `CHECKED_NOT_CANDIDATE`, so
    # `list_candidate_messages_for_domain` naturally returns nothing the
    # second time round.
    second = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert second == []
    assert h.graph_client.content_calls == ["m1"]  # only ONE content fetch total


def test_reprocess_all_historical_candidates_back_processes_every_historical_candidate_not_just_the_trigger():
    """The architect's own most important new requirement (operational
    addendum): 'the domain-learning mechanism is not useful if it only
    affects future mail'. Simulates a small 'N invoices from one new
    supplier' scenario: THREE historical `CHECKED_NOT_CANDIDATE`
    messages from the same unknown domain trigger exactly ONE Needs You
    item (existing dedup behaviour — re-confirmed here), and approving
    that ONE item back-processes ALL THREE, not just the one that
    happened to trigger it.

    This is proof #5 of the WO's required-tests list — the key
    differentiating proof."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(
            status=GraphOutcomeStatus.OK,
            messages=(_msg("m1"), _msg("m2"), _msg("m3")),
            delta_link="d1",
        )
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 3
    assert all(m.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE for m in messages)

    # Exactly ONE Needs You item, despite THREE candidate messages.
    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1
    assert items[0].metadata["candidate_message_count"] == 3

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    # Content is fetched once per message, sequentially — queue THREE.
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())

    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 3
    assert {m.ingestion_status for m in reprocessed} == {"INGESTED"}
    assert sorted(h.graph_client.content_calls) == ["m1", "m2", "m3"]

    # Durably true — re-read every message from the repository fresh.
    refreshed = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(refreshed) == 3
    assert all(m.ingestion_status == "INGESTED" for m in refreshed)
    assert all(m.evidence_id is not None for m in refreshed)


# ---------------------------------------------------------------------
# Second CD-6 architect amendment — persisted discovery decision. The
# above `reprocess_all_historical_candidates_for_domain` coverage was
# insufficient: every fixture message in it happens to be a genuine
# candidate, so it could never catch the real defect (approving a
# domain back-processing EVERY historical CHECKED_NOT_CANDIDATE message
# from it, including ordinary non-financial mail and previously-
# IGNORED-domain messages sharing the identical status).
# ---------------------------------------------------------------------


def test_reprocess_mixed_domain_only_back_processes_actual_candidates_never_every_checked_not_candidate_message():
    """The key differentiating proof for this WO: SEVEN historical
    CHECKED_NOT_CANDIDATE messages from the SAME unknown domain — THREE
    genuine candidates (default `_msg()` subject "Invoice") and FOUR
    ordinary non-candidate messages. Exactly one MAILBOX_DOMAIN_REVIEW
    item must exist (dedup unaffected), its candidate_message_count must
    be 3 (not 7 — the aggregate accumulation was already correctly
    gated on `signal.is_candidate`, only the PERSISTED per-message field
    was the actual gap), and approving it must reprocess exactly the 3
    real candidates — never all 7."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    candidate_msgs = [_msg(f"cand-{i}") for i in range(1, 4)]  # default subject "Invoice" -> is_candidate True
    non_candidate_msgs = [
        _msg(f"noncand-{i}", subject="Let's catch up for coffee next week") for i in range(1, 5)
    ]
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(
            status=GraphOutcomeStatus.OK, messages=tuple(candidate_msgs + non_candidate_msgs), delta_link="d1"
        )
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 7
    assert all(m.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE for m in messages)
    # Persisted per-message discovery decision (the actual fix) — real
    # candidates got `discovery_candidate is True`, ordinary mail got
    # `False`.
    by_id = {m.immutable_provider_message_id: m for m in messages}
    for i in range(1, 4):
        assert by_id[f"cand-{i}"].discovery_candidate is True
        assert by_id[f"cand-{i}"].discovery_reason
    for i in range(1, 5):
        assert by_id[f"noncand-{i}"].discovery_candidate is False
        assert by_id[f"noncand-{i}"].discovery_reason is None

    # Exactly ONE Needs You item, with the honest candidate-only count.
    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1
    assert items[0].metadata["candidate_message_count"] == 3

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())

    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    # Exactly 3 messages returned/reprocessed — NEVER 7.
    assert len(reprocessed) == 3
    assert {m.immutable_provider_message_id for m in reprocessed} == {"cand-1", "cand-2", "cand-3"}
    assert {m.ingestion_status for m in reprocessed} == {"INGESTED"}
    # Exactly 3 MIME content fetches occurred — never 7.
    assert sorted(h.graph_client.content_calls) == ["cand-1", "cand-2", "cand-3"]

    refreshed = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    remaining_non_candidates = [m for m in refreshed if m.immutable_provider_message_id.startswith("noncand-")]
    assert len(remaining_non_candidates) == 4
    assert all(m.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE for m in remaining_non_candidates)
    assert all(m.evidence_id is None for m in remaining_non_candidates)

    # Double-submit for the same domain: zero additional MIME fetches,
    # empty result list — the 3 already-eligible candidates are now
    # INGESTED (no longer CHECKED_NOT_CANDIDATE) and the 4 non-
    # candidates were never eligible and still aren't.
    second_call = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert second_call == []
    assert sorted(h.graph_client.content_calls) == ["cand-1", "cand-2", "cand-3"]


# ---------------------------------------------------------------------
# CD-6 GUI-operations-foundation follow-on WO (item C) — historical
# MUST_READ back-processing must now pass the SAME security gate an
# ordinary live sweep applies, via a bounded, headers-only refresh
# BEFORE any MIME fetch.
# ---------------------------------------------------------------------


def test_historical_reprocess_fetches_headers_before_any_mime_fetch_and_passes_through_on_pass():
    """Historical candidate authentication happens BEFORE any MIME fetch
    — proven via the fake client's own call-tracking (mirrors the
    existing "zero MIME fetch" proofs elsewhere in this file): a PASSING
    fresh headers refresh proceeds to the ordinary MIME-fetch/ingest
    path exactly as before."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    assert reprocessed[0].ingestion_status == "INGESTED"
    # Headers fetched, then content fetched — and content fetch never
    # attempted before the headers call this same message.
    assert h.graph_client.headers_calls == ["m1"]
    assert h.graph_client.content_calls == ["m1"]


def test_historical_reprocess_auth_fail_never_fetches_mime_and_marks_security_review():
    """Auth FAIL on the fresh headers refresh -> zero MIME fetch, marked
    SECURITY_REVIEW, and an authentication-escalation item is raised —
    never silently skipped, never silently proceeded as if it passed."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(
        GraphMessageHeadersResult(
            status=GraphOutcomeStatus.OK,
            raw_headers=(
                {
                    "name": "Authentication-Results",
                    "value": "spf=fail smtp.mailfrom=vendor.com; dkim=fail header.d=vendor.com; "
                    "dmarc=fail action=quarantine header.from=vendor.com; compauth=fail reason=001",
                },
            ),
        )
    )
    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    assert reprocessed[0].ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert h.graph_client.headers_calls == ["m1"]
    assert h.graph_client.content_calls == []  # never MIME-fetched
    escalation_items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION)
    assert len(escalation_items) == 1


def test_historical_reprocess_auth_unknown_never_fetches_mime_and_marks_security_review():
    """Auth UNKNOWN (no usable header at all on the fresh refresh) ->
    zero MIME fetch, marked SECURITY_REVIEW."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    h.graph_client.queue_headers_result(GraphMessageHeadersResult(status=GraphOutcomeStatus.OK, raw_headers=()))
    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 1
    assert reprocessed[0].ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert h.graph_client.content_calls == []


def test_historical_back_process_splits_passed_and_failed_auth_within_the_same_run():
    """`reprocess_all_historical_candidates_for_domain` stays candidate-
    only and idempotent, AND now correctly splits historical candidates
    into 'passed auth, deep-ingested' vs 'failed/unknown auth, security-
    reviewed' outcomes within the SAME back-process run — never silently
    treating every candidate identically."""
    from services.mailbox.sweep import reprocess_all_historical_candidates_for_domain

    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(
            status=GraphOutcomeStatus.OK, messages=(_msg("m1"), _msg("m2"), _msg("m3")), delta_link="d1"
        )
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    rule = h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    fail_headers = GraphMessageHeadersResult(
        status=GraphOutcomeStatus.OK,
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=fail smtp.mailfrom=vendor.com; dkim=fail header.d=vendor.com; "
                "dmarc=fail action=quarantine header.from=vendor.com; compauth=fail reason=001",
            },
        ),
    )
    # m1 discovered/queued first (see `list_candidate_messages_for_domain`'s
    # own oldest-received-first ordering) -> PASS, m2 -> FAIL, m3 -> PASS.
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_headers_result(fail_headers)
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())

    reprocessed = reprocess_all_historical_candidates_for_domain(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, sender_domain="vendor.com",
        rule=rule, adapter=h.adapter, message_repository=h.message_repo, needs_you_repository=h.needs_you_repo, api=h.api, object_store=h.object_store,
        scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert len(reprocessed) == 3
    statuses = {m.immutable_provider_message_id: m.ingestion_status for m in reprocessed}
    assert statuses["m1"] == "INGESTED"
    assert statuses["m2"] == INGESTION_STATUS_SECURITY_REVIEW
    assert statuses["m3"] == "INGESTED"
    # Only the two PASSING candidates were ever MIME-fetched.
    assert sorted(h.graph_client.content_calls) == ["m1", "m3"]
    escalation_items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION)
    assert len(escalation_items) == 1
    assert escalation_items[0].source_object_reference == [
        m.mailbox_message_id for m in reprocessed if m.immutable_provider_message_id == "m2"
    ][0]


def test_ignored_domain_message_is_never_swept_in_even_after_the_domain_is_later_allowed():
    """An IGNORED-domain message's `ingestion_status` is the IDENTICAL
    `CHECKED_NOT_CANDIDATE` value a real candidate carries — proving the
    new `discovery_candidate` gate, not the status, is what keeps it out
    of `list_candidate_messages_for_domain`. Also proves a realistic
    scenario: a domain starts IGNORED, is later reconsidered and
    approved — the OLD IGNORED-era message must not be swept into
    back-processing merely because the domain's CURRENT policy changed
    and the ingestion_status happens to match."""
    h = Harness(allow_default_domain=False)
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="BLACKLIST",
        destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("ignored-1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "ignored-1")
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert message.discovery_candidate is None  # the heuristic never ran for an IGNORED-domain message
    assert message.discovery_checked_at is not None  # but "checked" IS honestly recorded

    assert h.message_repo.list_candidate_messages_for_domain(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com"
    ) == []

    # Realistic scenario: the domain, originally IGNORED, is later
    # reconsidered and approved by an operator.
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="MUST_READ",
        destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )
    # The old IGNORED-era message must still NOT be swept in — only
    # messages the heuristic actually flagged discovery_candidate=True
    # are ever eligible, regardless of the domain's CURRENT policy.
    assert h.message_repo.list_candidate_messages_for_domain(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com"
    ) == []


def test_reobserved_candidate_still_checked_not_candidate_preserves_its_discovery_fields():
    """Second latent defect fix (WO instruction): the existing-final
    re-observation short-circuit (a message already
    CHECKED_NOT_CANDIDATE, re-seen on a later sweep round before any
    operator decision) omits the new discovery params entirely — this
    must never silently reset an already-set `discovery_candidate=True`
    back to `None`."""
    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.discovery_candidate is True
    assert message.discovery_reason is not None
    first_checked_at = message.discovery_checked_at
    assert first_checked_at is not None

    # Same message re-observed on a LATER sweep round, still unresolved
    # (no operator decision yet) — hits the already-final short-circuit,
    # never a Stage-B re-decision.
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d2"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-2"))
    second = h.sweep()
    assert second.duplicates == 1

    reobserved = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert reobserved.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert reobserved.discovery_candidate is True  # NOT silently reset to None
    assert reobserved.discovery_reason == message.discovery_reason
    assert reobserved.discovery_checked_at == first_checked_at  # this path never recomputes it


# ---------------------------------------------------------------------
# compute_bootstrap_floor (architect spec §1/§10 — governed AND
# mailbox-scoped, second correction: NEVER a global magic date)
# ---------------------------------------------------------------------
#
# `compute_bootstrap_floor` no longer takes just an `entity_repository`
# and returns the global minimum unconditionally — see
# `services/mailbox/sweep.py`'s own module docstring, "Historical
# bootstrap boundary" section, for the full corrected design this
# section proves: a mailbox's in-scope destination entities (its own
# `default_entity_id` hint UNION any `ALLOWED` `MailboxDomainRule`
# `destination_entity_id`s) are derived and minimised; an EMPTY
# in-scope set (the real, live `matt@infosecurs.com` state) falls back
# to every seeded entity. Pure per-entity derivation (`services.mailbox
# .bootstrap_policy.compute_entity_historical_bootstrap`) is proven
# separately in `tests/services/test_bootstrap_policy.py` — the tests
# below prove the MAILBOX-SCOPING/fallback/minimum behaviour on top of
# it, plus the two honest-failure paths.


def _plain_mailbox(display_name: str = "M", email_address: str = "scoping@example.com"):
    """A bare `MailboxSource` with no `default_entity_id` hint and an
    empty `MailboxDomainRuleRepository` — the minimum fixture needed to
    call `compute_bootstrap_floor` directly, outside a full `Harness`."""
    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(
        display_name=display_name, email_address=email_address, provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    return mailbox, InMemoryMailboxDomainRuleRepository()


#: The exact "now" the architect's own worked examples were verified
#: against (see `services/mailbox/bootstrap_policy.py`'s own module
#: docstring) — reused here so this file's own assertions match those
#: worked examples exactly, not merely "some plausible date".
_ARCHITECT_WORKED_NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def test_compute_bootstrap_floor_falls_back_to_all_entities_and_returns_their_derived_minimum():
    """No in-scope destination entities (mailbox.default_entity_id is
    None, zero MailboxDomainRule rows) — the real, current state of
    `matt@infosecurs.com` — falls back to every seeded entity and
    returns the MINIMUM of their DERIVED (never stored-literal)
    historical-bootstrap values. Matches the architect's own worked
    Infosecurs example (2024-11-01 is the earliest of the two)."""
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    api.register_entity(
        entity_type="COMPANY", canonical_name="A_LTD", display_name="A", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="11-01",
    )
    api.register_entity(
        entity_type="PERSON", canonical_name="B_PERSONAL", display_name="B", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="04-06",
    )
    mailbox, domain_rule_repo = _plain_mailbox()

    floor = compute_bootstrap_floor(
        mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo,
        now=_ARCHITECT_WORKED_NOW,
    )
    assert floor == datetime(2024, 11, 1, tzinfo=timezone.utc)


def test_compute_bootstrap_floor_fails_honestly_when_any_in_scope_entity_is_missing_configuration():
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    api.register_entity(
        entity_type="COMPANY", canonical_name="A_LTD", display_name="A", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="11-01",
    )
    api.register_entity(
        entity_type="PERSON", canonical_name="B_PERSONAL", display_name="B", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test",
        # No fiscal_year_start_month_day — real, honest gap.
    )
    mailbox, domain_rule_repo = _plain_mailbox()
    with pytest.raises(ConflictError):
        compute_bootstrap_floor(mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo)


def test_compute_bootstrap_floor_fails_honestly_when_no_entities_exist_yet():
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    mailbox, domain_rule_repo = _plain_mailbox()
    with pytest.raises(ConflictError):
        compute_bootstrap_floor(mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo)


def test_sweep_fails_honestly_when_entity_bootstrap_configuration_is_incomplete():
    h = Harness()
    # A SECOND entity, missing the required configuration — the
    # Harness's own mailbox has no in-scope destination entity (its
    # domain rule for vendor.com is REVIEW_REQUIRED with
    # destination_entity_id=None — see `_DEFAULT_ALLOWED_DOMAIN`'s own
    # setup — so it never enters the in-scope set), so this falls back
    # to every seeded entity and must refuse rather than silently using
    # only the first entity's floor or inventing a fallback.
    h.api.register_entity(
        entity_type="COMPANY", canonical_name="UNCONFIGURED_LTD", display_name="Unconfigured", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test",
    )
    h.queue_empty_both_folders()
    with pytest.raises(ConflictError):
        h.sweep()


# ---------------------------------------------------------------------
# compute_bootstrap_floor — mailbox scoping (the key differentiating
# proof: scoped vs. global-minimum answers genuinely differ)
# ---------------------------------------------------------------------


def _three_real_entities(api: BagmanCanonicalAPI) -> dict[str, "GovernedEntity"]:
    """The three real canonical entities this delivery seeds (see
    `app/api/composition.py::SEED_ENTITIES`), registered directly
    against a fresh `api` for a test that wants real, distinctly-
    derived floors to scope between. Derived floors as of
    `_ARCHITECT_WORKED_NOW` (2026-09-18):

        INFOSECURS_LIMITED     -> 2024-11-01  (global minimum)
        MATTHEW_SCOTT_PERSONAL -> 2025-04-06
        NOUSTAI_LIMITED        -> 2025-12-05  (clamped by its override)
    """
    infosecurs = api.register_entity(
        entity_type="COMPANY", canonical_name="INFOSECURS_LIMITED", display_name="Infosecurs Limited",
        status="ACTIVE", actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="11-01",
    )
    personal = api.register_entity(
        entity_type="PERSON", canonical_name="MATTHEW_SCOTT_PERSONAL", display_name="Matthew Scott Personal",
        status="ACTIVE", actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="04-06",
    )
    noustai = api.register_entity(
        entity_type="COMPANY", canonical_name="NOUSTAI_LIMITED", display_name="NoustAI Limited",
        status="ACTIVE", actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="01-01",
        historical_floor_override_at=datetime(2025, 12, 5, tzinfo=timezone.utc),
    )
    return {"INFOSECURS_LIMITED": infosecurs, "MATTHEW_SCOTT_PERSONAL": personal, "NOUSTAI_LIMITED": noustai}


def test_mailbox_scoped_via_default_entity_id_uses_its_own_entitys_floor_not_the_global_minimum():
    """The key differentiating proof (architect spec): a mailbox scoped
    to ONLY the entity whose derived floor is NOT the global minimum
    (NoustAI, 2025-12-05) must get NoustAI's own floor back — never the
    global minimum (Infosecurs, 2024-11-01) a pre-scoping implementation
    would have returned."""
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    entities = _three_real_entities(api)
    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(
        display_name="Mailbox A", email_address="mailbox-a@example.com", provider_kind=PROVIDER_MICROSOFT_GRAPH,
        default_entity_id=entities["NOUSTAI_LIMITED"].entity_id,
    )
    domain_rule_repo = InMemoryMailboxDomainRuleRepository()

    floor = compute_bootstrap_floor(
        mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo,
        now=_ARCHITECT_WORKED_NOW,
    )
    assert floor == datetime(2025, 12, 5, tzinfo=timezone.utc)
    assert floor != datetime(2024, 11, 1, tzinfo=timezone.utc)  # NOT the global minimum


def test_mailbox_with_no_scope_hints_falls_back_to_global_minimum_matching_matt_infosecurs():
    """Mailbox B: `default_entity_id=None`, zero domain rules — the
    real, live `matt@infosecurs.com` state. Must fall back to the
    global minimum across all three real entities (2024-11-01),
    matching the architect's own worked example verbatim: "If
    matt@infosecurs.com is presently scoped only to Infosecurs for the
    first acceptance, its bootstrap is: 2024-11-01T00:00:00Z" — the
    fallback-to-all answer and a hypothetical Infosecurs-only-scoped
    answer coincide here because Infosecurs's own derived floor IS the
    global minimum; that is expected, not a bug."""
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    _three_real_entities(api)
    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(
        display_name="Mailbox B (matt@infosecurs.com)", email_address="matt@infosecurs.com",
        provider_kind=PROVIDER_MICROSOFT_GRAPH,
    )
    domain_rule_repo = InMemoryMailboxDomainRuleRepository()

    floor = compute_bootstrap_floor(
        mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo,
        now=_ARCHITECT_WORKED_NOW,
    )
    assert floor == datetime(2024, 11, 1, tzinfo=timezone.utc)


def test_mailbox_scoped_via_an_allowed_domain_rule_destination_also_participates_in_scope():
    """A mailbox with no `default_entity_id` hint at all, but ONE
    `ALLOWED` `MailboxDomainRule` naming a real `destination_entity_id`
    — the domain-rule-derived scope must participate exactly like the
    hint does, not only `default_entity_id`."""
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    entities = _three_real_entities(api)
    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(
        display_name="Mailbox C", email_address="mailbox-c@example.com", provider_kind=PROVIDER_MICROSOFT_GRAPH,
    )
    domain_rule_repo = InMemoryMailboxDomainRuleRepository()
    domain_rule_repo.upsert_rule(
        mailbox_id=mailbox.mailbox_id, sender_domain="noustai-supplier.example", match_mode="EXACT",
        policy="MUST_READ", destination_entity_id=entities["NOUSTAI_LIMITED"].entity_id,
        destination_mode="FIXED", source="OPERATOR",
    )

    floor = compute_bootstrap_floor(
        mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo,
        now=_ARCHITECT_WORKED_NOW,
    )
    assert floor == datetime(2025, 12, 5, tzinfo=timezone.utc)  # NoustAI's own floor, not the global minimum


def test_mailbox_scoping_ignores_an_ignored_rules_destination_and_a_review_required_rules_none():
    """Two negative-scoping proofs in one test: an `IGNORED` rule's
    (nonexistent) destination never enters scope, and an `ALLOWED`
    rule with `destination_mode="REVIEW_REQUIRED"`
    (`destination_entity_id=None`) contributes nothing to scope either
    — only a real, non-None `destination_entity_id` on an `ALLOWED`
    rule ever does. With no real in-scope entity from either rule, this
    still falls back to the global minimum."""
    from services.mailbox.sweep import compute_bootstrap_floor

    api = BagmanCanonicalAPI()
    _three_real_entities(api)
    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(
        display_name="Mailbox D", email_address="mailbox-d@example.com", provider_kind=PROVIDER_MICROSOFT_GRAPH,
    )
    domain_rule_repo = InMemoryMailboxDomainRuleRepository()
    domain_rule_repo.upsert_rule(
        mailbox_id=mailbox.mailbox_id, sender_domain="spam.example", match_mode="EXACT",
        policy="BLACKLIST", destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    domain_rule_repo.upsert_rule(
        mailbox_id=mailbox.mailbox_id, sender_domain="unsure.example", match_mode="EXACT",
        policy="MUST_READ", destination_entity_id=None, destination_mode="REVIEW_REQUIRED", source="OPERATOR",
    )

    floor = compute_bootstrap_floor(
        mailbox=mailbox, entity_repository=api.entity_repository, domain_rule_repository=domain_rule_repo,
        now=_ARCHITECT_WORKED_NOW,
    )
    assert floor == datetime(2024, 11, 1, tzinfo=timezone.utc)  # fallback to global minimum, exactly as with no rules at all


# ---------------------------------------------------------------------
# CD-6 architect amendment — recursive Microsoft Graph folder discovery
# ---------------------------------------------------------------------

DELETED_ITEMS_FOLDER_ID = "AAMkADdeleteditems00000000000000"
CUSTOM_FOLDER_ID = "AAMkADcustom0000000000000000000"
CUSTOM_CHILD_FOLDER_ID = "AAMkADcustomchild000000000000000"
SENT_FOLDER_ID = "AAMkADsentitems00000000000000000"
DRAFTS_FOLDER_ID = "AAMkADdrafts000000000000000000000"
OUTBOX_FOLDER_ID = "AAMkADoutbox000000000000000000000"
HIDDEN_FOLDER_ID = "AAMkADhidden000000000000000000000"


def test_folder_discovery_error_stops_the_whole_sweep_before_any_folder_is_attempted():
    """A non-OK folder-discovery outcome is a WHOLE-SWEEP precondition
    failure — never blended into a per-folder transient-failure code
    (module docstring's own 'Folder discovery' section)."""
    h = Harness()
    h.graph_client.queue_folder_list_result(
        GraphFolderListResult(status=GraphOutcomeStatus.PROVIDER_ERROR, error_detail="boom")
    )
    h.graph_client.queue_well_known_folders_result(GraphWellKnownFoldersResult(status=GraphOutcomeStatus.OK))
    run = run_sweep(
        mailbox=h.mailbox, mailbox_source_id=h.source_id, trigger=TRIGGER_MANUAL, adapter=h.adapter,
        message_repository=h.message_repo, sweep_run_repository=h.sweep_run_repo,
        cursor_repository=h.cursor_repo, sweep_lock=h.lock, mailbox_repository=h.mailbox_repo,
        domain_rule_repository=h.domain_rule_repo, needs_you_repository=h.needs_you_repo,
        entity_repository=h.api.entity_repository,
        api=h.api, object_store=h.object_store, scanner=h.scanner, actor_type="SYSTEM", actor_id="test",
    )
    assert run.status == "FAILED"
    assert run.error_code == SweepFailureReason.FOLDER_DISCOVERY_FAILED
    assert list(run.folders_attempted) == []


def test_full_monitored_set_excludes_sent_drafts_outbox_and_includes_nested_hidden_custom():
    h = Harness()
    full_raw_folders = (
        GraphFolderSummary(folder_id=h.INBOX_FOLDER_ID, display_name="Inbox", parent_folder_id=None, child_folder_count=0),
        GraphFolderSummary(folder_id=h.JUNK_FOLDER_ID, display_name="Junk Email", parent_folder_id=None, child_folder_count=0),
        GraphFolderSummary(folder_id=DELETED_ITEMS_FOLDER_ID, display_name="Deleted Items", parent_folder_id=None, child_folder_count=0),
        GraphFolderSummary(folder_id=SENT_FOLDER_ID, display_name="Sent Items", parent_folder_id=None, child_folder_count=0),
        GraphFolderSummary(folder_id=DRAFTS_FOLDER_ID, display_name="Drafts", parent_folder_id=None, child_folder_count=0),
        GraphFolderSummary(folder_id=OUTBOX_FOLDER_ID, display_name="Outbox", parent_folder_id=None, child_folder_count=0),
        GraphFolderSummary(folder_id=CUSTOM_FOLDER_ID, display_name="Supplier Invoices", parent_folder_id=None, child_folder_count=1),
        GraphFolderSummary(folder_id=CUSTOM_CHILD_FOLDER_ID, display_name="2026", parent_folder_id=CUSTOM_FOLDER_ID, child_folder_count=0),
        GraphFolderSummary(folder_id=HIDDEN_FOLDER_ID, display_name="Clutter", parent_folder_id=None, child_folder_count=0, is_hidden=True),
    )
    full_well_known_ids = {
        "inbox": h.INBOX_FOLDER_ID, "junkemail": h.JUNK_FOLDER_ID, "deleteditems": DELETED_ITEMS_FOLDER_ID,
        "sentitems": SENT_FOLDER_ID, "drafts": DRAFTS_FOLDER_ID, "outbox": OUTBOX_FOLDER_ID,
    }
    # Six MONITORED folders (Sent/Drafts/Outbox filtered out before the
    # sweep ever iterates) — one empty delta round each.
    for i in range(6):
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d{i}"))

    run = h.sweep(folders=full_raw_folders, well_known_ids=full_well_known_ids)
    assert run.status == "SUCCEEDED"
    attempted_ids = {f["folder_id"] for f in run.folders_attempted}
    assert attempted_ids == {
        h.INBOX_FOLDER_ID, h.JUNK_FOLDER_ID, DELETED_ITEMS_FOLDER_ID, CUSTOM_FOLDER_ID, CUSTOM_CHILD_FOLDER_ID, HIDDEN_FOLDER_ID,
    }
    assert SENT_FOLDER_ID not in attempted_ids
    assert DRAFTS_FOLDER_ID not in attempted_ids
    assert OUTBOX_FOLDER_ID not in attempted_ids


_THREE_FOLDER_SET = (
    GraphFolderSummary(folder_id=Harness.INBOX_FOLDER_ID, display_name="Inbox", parent_folder_id=None, child_folder_count=0),
    GraphFolderSummary(folder_id=Harness.JUNK_FOLDER_ID, display_name="Junk Email", parent_folder_id=None, child_folder_count=0),
    GraphFolderSummary(folder_id=DELETED_ITEMS_FOLDER_ID, display_name="Deleted Items", parent_folder_id=None, child_folder_count=0),
)
_THREE_FOLDER_WELL_KNOWN_IDS = {
    "inbox": Harness.INBOX_FOLDER_ID, "junkemail": Harness.JUNK_FOLDER_ID, "deleteditems": DELETED_ITEMS_FOLDER_ID,
}


def test_deleted_items_is_genuinely_swept_through_the_full_pipeline_same_as_inbox():
    """Deleted Items is explicitly in scope — a message discovered
    there runs through the EXACT SAME Stage-A/Stage-B gate, MIME-fetch,
    evidence-ingest pipeline as an Inbox message; its deleted location
    is recorded as provenance only."""
    h = Harness()
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-inbox"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("del-1"),), delta_link="d-deleted")
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())

    run = h.sweep(folders=_THREE_FOLDER_SET, well_known_ids=_THREE_FOLDER_WELL_KNOWN_IDS)
    assert run.status == "SUCCEEDED"
    assert run.evidence_created == 1
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == "INGESTED"
    assert message.evidence_id is not None
    assert message.observed_folder == DELETED_ITEMS_FOLDER_ID
    assert message.observed_folder_display_name == "Deleted Items"


def test_no_mime_fetch_for_a_non_candidate_message_in_deleted_items():
    """Data-volume proof (architect §9), extended to a non-Inbox/Junk
    folder: an IGNORED-domain message in Deleted Items still costs
    metadata processing only — never a MIME fetch."""
    h = Harness(allow_default_domain=False)
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="BLACKLIST",
        destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-inbox"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("del-ignored"),), delta_link="d-deleted")
    )

    run = h.sweep(folders=_THREE_FOLDER_SET, well_known_ids=_THREE_FOLDER_WELL_KNOWN_IDS)
    assert run.status == "SUCCEEDED"
    assert h.graph_client.content_calls == []
    message = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)[0]
    assert message.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
    assert message.observed_folder == DELETED_ITEMS_FOLDER_ID


def test_message_moving_to_a_third_folder_deleted_items_still_resolves_to_one_canonical_row():
    """Extends the original Inbox<->Junk idempotency proof to a THIRD
    folder — canonical uniqueness is (mailbox_id,
    immutable_provider_message_id) alone, folder-count-independent."""
    h = Harness()
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m-moved"),), delta_link="d-inbox")
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m-moved"),), delta_link="d-deleted")
    )

    run = h.sweep(folders=_THREE_FOLDER_SET, well_known_ids=_THREE_FOLDER_WELL_KNOWN_IDS)
    assert run.evidence_created == 1
    assert run.duplicates == 1
    messages = h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)
    assert len(messages) == 1
    assert messages[0].observed_folder == DELETED_ITEMS_FOLDER_ID  # last-seen wins


def test_folder_id_keyed_cursors_never_collide_even_constructed_at_the_same_moment(h):
    """Folder-ID-keyed cursor independence with real, GUID-shaped
    identifiers (not just the generic 'INBOX'/'JUNK' strings other
    cursor tests use) — proves the identity key is genuinely the real
    folder id, not a coincidence of two short literal strings."""
    ts = datetime.now(timezone.utc)
    custom_a = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=CUSTOM_FOLDER_ID,
        bootstrap_timestamp=ts,
    )
    custom_b = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=CUSTOM_CHILD_FOLDER_ID,
        bootstrap_timestamp=ts,
    )
    h.cursor_repo.advance_cursor(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=CUSTOM_FOLDER_ID,
        delta_link="custom-a-delta",
    )
    still_none = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=CUSTOM_CHILD_FOLDER_ID,
        bootstrap_timestamp=ts,
    )
    assert custom_a.delta_link is None
    assert custom_b.delta_link is None
    assert still_none.delta_link is None  # advancing custom_a never touched custom_b's own cursor


def test_old_inbox_junk_literal_cursor_rows_are_left_untouched_and_new_folder_id_cursor_starts_fresh_safely():
    """Part D's own migration judgment call, proven directly (see
    services/mailbox/sweep.py's own module docstring, 'Folder-ID-keyed
    cursors'): a pre-existing OLD-scheme cursor row keyed by the
    literal string "INBOX" (Slice 4A's own pre-amendment convention) is
    NEVER read or written by the new folder-ID-keyed sweep — and
    re-establishing a FRESH bootstrap-floor-bounded cursor for Inbox's
    own REAL folder id does not re-ingest/duplicate a message already
    known under the old scheme; idempotency alone makes this safe."""
    h = Harness()
    # Simulate the OLD, pre-amendment cursor scheme's own row, still
    # sitting in the cursor repository (a real Slice 4A artifact) —
    # never touched by the new folder-ID-keyed lookups below.
    h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder="INBOX",
        bootstrap_timestamp=datetime.now(timezone.utc) - timedelta(days=400),
    )
    h.cursor_repo.advance_cursor(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder="INBOX",
        delta_link="old-scheme-delta-link",
    )

    # A message ingested "under the new folder-ID-keyed scheme" —
    # stands in for one of the real 128 already-known Infosecurs
    # messages.
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("already-known"),), delta_link="d1")
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    first = h.sweep()
    assert first.evidence_created == 1

    # The OLD "INBOX"-keyed cursor row is completely untouched.
    still_old = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder="INBOX",
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    assert still_old.delta_link == "old-scheme-delta-link"

    # A later sweep re-discovers the SAME already-known message from
    # the (real, folder-id-keyed) bootstrap floor — idempotency
    # ((mailbox_id, immutable_provider_message_id)) makes this safe:
    # never a new MailboxMessage row, never a second evidence object.
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("already-known"),), delta_link="d2")
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-2"))
    second = h.sweep()
    assert second.evidence_created == 0
    assert second.duplicates == 1
    assert len(h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)) == 1

    # The new, real-folder-id-keyed cursor is its OWN row, independent
    # of the old "INBOX"-keyed one.
    new_style = h.cursor_repo.get_or_bootstrap(
        mailbox_id=h.mailbox.mailbox_id, provider_kind=h.mailbox.provider_kind, folder=h.INBOX_FOLDER_ID,
        bootstrap_timestamp=datetime.now(timezone.utc),
    )
    assert new_style.delta_link == "d2"


# ---------------------------------------------------------------------
# Operational addendum (ahead of the first real large historical sweep)
# — rich sweep-run aggregate reporting + per-folder operational counts
# ---------------------------------------------------------------------


def test_sweep_run_aggregate_reporting_fields_across_mixed_messages_and_rate_limiting():
    """Proof #1 of the operational addendum's required tests: a
    synthetic scenario mixing ALLOWED/IGNORED/unknown-credible/unknown-
    non-credible messages, one with an attachment, plus an injected
    Graph `RATE_LIMITED` response — proves every new `MailboxSweepRun`
    aggregate field this addendum adds."""
    h = Harness()  # allow_default_domain=True -> vendor.com is ALLOWED
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="ignored.example", match_mode="EXACT", policy="BLACKLIST",
        destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    allowed_msg = _msg(
        "m-allowed", sender_address="billing@vendor.com", subject="Invoice",
        attachment_metadata=({"filename": "invoice.pdf", "content_type": "application/pdf", "size_bytes": 100},),
    )
    ignored_msg = _msg("m-ignored", sender_address="promo@ignored.example", subject="Weekly newsletter")
    credible_msg = _msg("m-candidate", sender_address="billing@new-supplier.example", subject="Invoice attached")
    non_credible_msg = _msg(
        "m-non-candidate", sender_address="friend@random.example", subject="Let's catch up for coffee"
    )

    # Inbox: one RATE_LIMITED response, retried once, then all four
    # messages on the (successful) retry's page.
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(
            status=GraphOutcomeStatus.OK,
            messages=(allowed_msg, ignored_msg, credible_msg, non_credible_msg),
            delta_link="d1",
        )
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())  # the ALLOWED-domain message's own MIME fetch
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    assert run.messages_seen == 4
    assert run.evidence_created == 1
    # 4 distinct sender domains: vendor.com, ignored.example,
    # new-supplier.example, random.example.
    assert run.unique_sender_domains == 4
    assert run.allowed_domain_messages == 1
    assert run.ignored_domain_messages == 1
    assert run.unknown_domain_messages == 2
    assert run.likely_financial_candidates == 1  # only the credible one
    assert run.messages_with_attachments == 1
    assert run.graph_throttle_retries == 1
    assert h.graph_client.content_calls == ["m-allowed"]


def test_per_folder_operational_counts_are_correct_and_distinct_per_folder():
    """Proof #2 of the operational addendum's required tests: a
    two-folder sweep where Inbox and Junk Email have DIFFERENT
    message/candidate mixes — proves the per-folder entries genuinely
    differ, not just a copy of the whole-sweep totals."""
    h = Harness()  # allow_default_domain=True -> vendor.com is ALLOWED
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m-inbox"),), delta_link="d-inbox")
    )
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    junk_msg = _msg("m-junk", sender_address="billing@new-supplier.example", subject="Invoice attached")
    h.graph_client.queue_delta_result(
        GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(junk_msg,), delta_link="d-junk")
    )

    run = h.sweep()
    assert run.status == "SUCCEEDED"
    by_folder = {f["folder_id"]: f for f in run.folders_attempted}
    inbox_entry = by_folder[h.INBOX_FOLDER_ID]
    junk_entry = by_folder[h.JUNK_FOLDER_ID]

    assert inbox_entry["messages_seen"] == 1
    assert inbox_entry["new_discovery_records"] == 1
    assert inbox_entry["deep_processing_count"] == 1  # ALLOWED-domain path -> a real MIME fetch attempt
    assert inbox_entry["completed"] is True
    assert inbox_entry["cursor_established"] is True

    assert junk_entry["messages_seen"] == 1
    assert junk_entry["new_discovery_records"] == 1
    assert junk_entry["deep_processing_count"] == 0  # unknown-domain candidate -> discovery only, no MIME fetch
    assert junk_entry["completed"] is True
    assert junk_entry["cursor_established"] is True

    # Genuinely different per folder — not a copy of the whole-sweep totals.
    assert inbox_entry["deep_processing_count"] != junk_entry["deep_processing_count"]
    assert inbox_entry != junk_entry


# ---------------------------------------------------------------------
# Operational addendum — domain-review item aggregate metadata
# accumulation across repeat candidates from the same still-open domain
# ---------------------------------------------------------------------


def test_domain_review_item_accumulates_aggregate_stats_across_repeat_candidates():
    """Proof #3 of the operational addendum's required tests: a SECOND
    candidate message from the same still-open domain updates
    `candidate_message_count` (1->2), `last_seen_at` (advances), and
    `attachment_bearing_count` (increments only if the new message has
    an attachment), while `first_seen_at` stays fixed at the first
    message's timestamp — and a THIRD candidate proves the accumulation
    genuinely continues, not just a 1->2 one-off."""
    h = Harness(allow_default_domain=False)
    m1 = _msg(
        "m1", subject="Invoice",
        attachment_metadata=({"filename": "invoice1.pdf", "content_type": "application/pdf", "size_bytes": 10},),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(m1,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1
    item = items[0]
    assert item.metadata["candidate_message_count"] == 1
    first_seen = item.metadata["first_seen_at"]
    assert item.metadata["last_seen_at"] == first_seen
    assert item.metadata["attachment_bearing_count"] == 1
    assert item.metadata["proposed_processor_hint"] is None
    assert "confidence_reason" in item.metadata

    # Second candidate — no attachment this time.
    m2 = _msg("m2", subject="Invoice", attachment_metadata=())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(m2,), delta_link="d2"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-2"))
    h.sweep()

    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1  # still just ONE item, not a second one
    item = items[0]
    assert item.metadata["candidate_message_count"] == 2
    assert item.metadata["first_seen_at"] == first_seen  # unchanged
    assert item.metadata["last_seen_at"] >= first_seen
    assert item.metadata["attachment_bearing_count"] == 1  # unchanged — m2 had no attachment

    # Third candidate — WITH an attachment again.
    m3 = _msg(
        "m3", subject="Invoice",
        attachment_metadata=({"filename": "invoice3.pdf", "content_type": "application/pdf", "size_bytes": 20},),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(m3,), delta_link="d3"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk-3"))
    h.sweep()

    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1
    item = items[0]
    assert item.metadata["candidate_message_count"] == 3
    assert item.metadata["first_seen_at"] == first_seen  # STILL unchanged
    assert item.metadata["attachment_bearing_count"] == 2


# =======================================================================
# CD-6 GUI-operations-foundation follow-on WO — three-state operator-
# learning policy model (MUST_READ/GRAYLIST/BLACKLIST), the real
# entity-assignment fix, authentication escalation, and document-level
# destination review. Tests below are numbered against the architect's
# own required-tests list (see the WO itself) where a direct mapping
# exists.
# =======================================================================


def test_must_read_fixed_destination_assigns_real_entity_id_to_evidence(h):
    """WO required test #2: Matt confirms MUST_READ + FIXED destination
    — the resulting evidence really gets entity_id == that fixed
    entity, not None (the real fix, see
    services/mailbox/microsoft/evidence_ingest.py)."""
    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN, match_mode="EXACT",
        policy="MUST_READ", destination_entity_id=h.entity.entity_id, destination_mode="FIXED", source="OPERATOR",
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()
    assert run.evidence_created == 1

    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.evidence_id is not None
    evidence = h.api.get_evidence(message.evidence_id)
    assert evidence.entity_id == h.entity.entity_id


def test_confirmed_must_read_source_never_reraises_domain_review_across_multiple_messages(h):
    """WO required tests #3 and #4: after Matt confirms a source
    MUST_READ, a SECOND, THIRD and FOURTH message from the SAME
    authenticated source are all deep-processed with ZERO new
    MAILBOX_DOMAIN_REVIEW items — the core durable-memory invariant,
    proven across real, repeated sweep rounds (never just 'the second
    one')."""
    for i, msg_id in enumerate(["m1", "m2", "m3", "m4"], start=1):
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg(msg_id),), delta_link=f"d{i}"))
        h.graph_client.queue_headers_result(_headers_ok())
        h.graph_client.queue_content_result(_content())
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-junk-{i}"))
        run = h.sweep()
        assert run.status == "SUCCEEDED"
        assert run.evidence_created == 1
        assert h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW) == []

    assert len(h.message_repo.list_messages(mailbox_id=h.mailbox.mailbox_id)) == 4


def test_must_read_review_required_raises_company_required_per_evidence_not_domain_review(h):
    """WO required test #5: MUST_READ + REVIEW_REQUIRED never raises a
    second source-relevance (MAILBOX_DOMAIN_REVIEW) item for later
    messages, but DOES raise a COMPANY_REQUIRED item scoped to each new
    evidence item it creates (the Harness's own default rule already
    uses destination_mode=REVIEW_REQUIRED)."""
    for i, msg_id in enumerate(["m1", "m2"], start=1):
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg(msg_id),), delta_link=f"d{i}"))
        h.graph_client.queue_headers_result(_headers_ok())
        h.graph_client.queue_content_result(_content())
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-junk-{i}"))
        h.sweep()

    assert h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW) == []
    company_items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_COMPANY_REQUIRED)
    assert len(company_items) == 2
    evidence_ids = {i.source_object_reference for i in company_items}
    assert len(evidence_ids) == 2  # one distinct item per evidence item, never shared
    for item in company_items:
        assert item.allowed_action_type == "COMPANY_WHAT_WHY"
        assert item.status == "OPEN"


def test_blacklist_suppresses_future_domain_review_noise():
    """WO required test #7: BLACKLIST suppresses future
    MAILBOX_DOMAIN_REVIEW noise for that domain — mirrors test #3's
    proof but for BLACKLIST (no new item on a later credible-looking
    message)."""
    h = Harness(allow_default_domain=False)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()
    assert len(h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)) == 1

    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com", match_mode="EXACT", policy="BLACKLIST",
        destination_entity_id=None, destination_mode=None, source="OPERATOR",
    )
    for i, msg_id in enumerate(["m2", "m3"], start=2):
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg(msg_id),), delta_link=f"d{i}"))
        h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link=f"d-junk-{i}"))
        h.sweep()

    # Still just the ONE original item — later credible-looking mail
    # from the now-BLACKLISTed domain raises nothing further.
    items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW)
    assert len(items) == 1


def test_must_read_message_with_hard_auth_failure_escalates_never_ingests(h):
    """WO required test #8 (real fix's own item B) — a MUST_READ-policy
    message with a genuine, TRUSTED authentication FAIL (a real
    `compauth=fail` on the plain `Authentication-Results` header)
    escalates to the security-review outcome/item type — WITHOUT
    touching the MailboxDomainRule's own policy — and does NOT get
    MIME-fetched/evidence-created via the normal trusted path."""
    failing_msg = _msg(
        "m1",
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=fail smtp.mailfrom=vendor.com; dkim=fail header.d=vendor.com; "
                "dmarc=fail action=quarantine header.from=vendor.com; compauth=fail reason=001",
            },
        ),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(failing_msg,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()

    assert run.evidence_created == 0
    assert h.graph_client.content_calls == []  # never MIME-fetched

    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert message.evidence_id is None
    assert message.metadata["auth_assessment"]["verdict"] == "FAIL"

    escalation_items = h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION)
    assert len(escalation_items) == 1
    assert escalation_items[0].source_object_reference == message.mailbox_message_id
    # Never conflated with the domain-relevance question.
    assert h.needs_you_repo.list_needs_you_items(item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW) == []

    rule = h.domain_rule_repo.find_for_sender(mailbox_id=h.mailbox.mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN)
    assert rule.policy == "MUST_READ"  # the rule itself is untouched — a per-message event, not a re-ask


def test_must_read_message_with_no_auth_signal_at_all_also_escalates(h):
    """No Authentication-Results (or ARC-Authentication-Results) header
    present at all -> UNKNOWN -> ALSO escalates (treated identically to
    FAIL by the sweep gate — an inconclusive verdict is never silently
    trusted)."""
    silent_msg = _msg("m1", auth_signals={})
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(silent_msg,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert message.metadata["auth_assessment"]["verdict"] == "UNKNOWN"


def test_must_read_message_with_dmarc_pass_on_mixed_lower_signals_still_passes(h):
    """A trusted header's own dmarc=pass (or compauth=pass) is what
    matters — mixed/weaker spf/dkim tokens underneath it never change
    the outcome (this selector never gates on spf/dkim alone — see
    `services.mailbox.microsoft.authentication`'s own module
    docstring)."""
    passing_msg = _msg(
        "m1",
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=softfail smtp.mailfrom=vendor.com; dkim=pass header.d=vendor.com; "
                "dmarc=pass action=none header.from=vendor.com; compauth=pass reason=100",
            },
        ),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(passing_msg,), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()

    assert run.evidence_created == 1
    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.ingestion_status == "INGESTED"


def test_dmarc_alignment_sensitive_case_spf_fail_under_dmarc_pass_never_escalates(h):
    """WO's own explicitly-named architect concern (real problem case
    #5): 'a DMARC PASS combined with an SPF FAIL can currently be
    escalated merely because SPF contains fail, even though DMARC may
    legitimately have passed through aligned DKIM' — the real, required
    acceptance test that this is now fixed: SPF FAIL underneath a real,
    trusted DMARC/compauth PASS must NEVER escalate."""
    msg = _msg(
        "m1",
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=fail (sender IP is 10.0.0.1) smtp.mailfrom=vendor.com; "
                "dkim=pass (signature was verified) header.d=vendor.com; "
                "dmarc=pass action=none header.from=vendor.com; compauth=pass reason=100",
            },
        ),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link="d1"))
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()

    assert run.evidence_created == 1
    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.ingestion_status == "INGESTED"
    assert message.metadata["auth_assessment"]["verdict"] == "PASS"


def test_forged_duplicate_header_cannot_manufacture_pass_over_a_genuine_fail(h):
    """WO's own required adversarial proof, exercised end to end through
    `run_sweep` (CD-6 second architect review, Finding 2 — corrected
    selected-header-only model): a forged, LATER `Authentication-Results`
    header claiming `compauth=pass` can never override the GENUINE,
    FIRST/selected header's own real `compauth=fail` — see
    `services.mailbox.microsoft.authentication`'s own module docstring
    ("ONLY the selected final-hop header gates") for the full
    reproduction this closes. The genuine header is placed FIRST
    (exactly as Microsoft Graph's own real header ordering guarantees —
    a new hop's own header is always prepended ahead of an earlier
    one), with the forged header appended SECOND — the realistic shape
    of this attack, and the one this selector is now specifically
    designed to defeat."""
    msg = _msg(
        "m1",
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=fail smtp.mailfrom=vendor.com; dkim=fail header.d=vendor.com; "
                "dmarc=fail action=quarantine header.from=vendor.com; compauth=fail reason=001",
            },
            {
                "name": "Authentication-Results",
                "value": "spf=pass smtp.mailfrom=vendor.com; dkim=pass header.d=vendor.com; "
                "dmarc=pass action=none header.from=vendor.com; compauth=pass reason=100",
            },
        ),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    run = h.sweep()

    assert run.evidence_created == 0
    assert h.graph_client.content_calls == []
    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    assert message.ingestion_status == INGESTION_STATUS_SECURITY_REVIEW
    assert message.metadata["auth_assessment"]["verdict"] == "FAIL"


def test_fixed_destination_never_inferred_from_mailbox_default_entity_id(h):
    """WO required test #12: no mailbox's own identity/default_entity_id
    ever implies a destination — every FIXED-destination assignment
    comes from an explicit rule, never inferred from which mailbox the
    message arrived through. Proven by giving the mailbox a DIFFERENT
    default_entity_id hint than the rule's own destination_entity_id,
    then proving the evidence gets the RULE's entity, never the
    mailbox's hint."""
    hint_entity = h.api.register_entity(
        entity_type="COMPANY", canonical_name="HINT_ONLY_ENTITY", display_name="Hint Only", status="ACTIVE",
        actor_type="SYSTEM", actor_id="test", fiscal_year_start_month_day="01-01",
    )
    h.mailbox_repo.update_mailbox(
        h.mailbox.mailbox_id,
        display_name=h.mailbox.display_name,
        email_address=h.mailbox.email_address,
        provider_kind=h.mailbox.provider_kind,
        default_entity_id=hint_entity.entity_id,
    )
    h.refresh_mailbox()

    h.domain_rule_repo.upsert_rule(
        mailbox_id=h.mailbox.mailbox_id, sender_domain=_DEFAULT_ALLOWED_DOMAIN, match_mode="EXACT",
        policy="MUST_READ", destination_entity_id=h.entity.entity_id, destination_mode="FIXED", source="OPERATOR",
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(_msg("m1"),), delta_link="d1"))
    h.graph_client.queue_headers_result(_headers_ok())
    h.graph_client.queue_content_result(_content())
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    message = h.message_repo.find_by_provider_id(h.mailbox.mailbox_id, "m1")
    evidence = h.api.get_evidence(message.evidence_id)
    assert evidence.entity_id == h.entity.entity_id
    assert evidence.entity_id != hint_entity.entity_id
