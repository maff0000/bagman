"""Real, disposable-PostgreSQL end-to-end proofs for
`scripts.evaluate_mailbox_health.run_evaluation` (PID §118,
WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.5/§12) — the full pipeline:
trigger detection -> incident+NeedsYou+notification creation -> claim ->
pre-dispatch recheck -> (fake) Pushover send -> recovery -> RECOVERY
notification, exercised against the real repositories this script
constructs in production, substituting only the external Pushover call
via `FakePushoverClient` (no real network I/O in any test).
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from core import identity
from core.timestamps import utc_now
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.mailbox_gmail_models import MailboxGmailOAuthStateRow
from persistence.postgres.mailbox_gmail_repository import PostgresMailboxGmailOAuthStateRepository
from persistence.postgres.mailbox_health_models import MailboxHealthIncidentRow, MailboxHealthNotificationRow
from persistence.postgres.mailbox_health_repository import (
    PostgresMailboxHealthIncidentRepository,
    PostgresMailboxHealthNotificationRepository,
)
from persistence.postgres.mailbox_microsoft_repository import PostgresMailboxSweepRunRepository
from persistence.postgres.mailbox_repository import PostgresMailboxSourceRepository
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.health.incident import (
    FAILURE_CLASS_AUTH_REQUIRED,
    FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE,
    FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR,
    ITEM_TYPE_MAILBOX_HEALTH_INCIDENT,
)
from services.mailbox.health.needs_you_sync import sync_needs_you_for_connection_state
from services.mailbox.health.notification import (
    PRE_DISPATCH_SUPPRESSIBLE_STATES,
    PURPOSE_INITIAL,
    PURPOSE_RECOVERY,
    STATE_ACCEPTED,
    STATE_CANCELLED,
    STATE_RETRYABLE_FAILURE,
)
from services.mailbox.mailbox import PROVIDER_GOOGLE_GMAIL, PROVIDER_IMAP, PROVIDER_MICROSOFT_GRAPH
from services.mailbox.sweep_run import SweepFailureReason
from services.needs_you.needs_you import ITEM_TYPE_MAILBOX_AUTH_REQUIRED
from services.notifications.pushover import FakePushoverClient, PushoverCredentials, PushoverOutcomeStatus, PushoverSendResult

import scripts.evaluate_mailbox_health as evl


def _repos():
    engine = get_engine()
    return {
        "engine": engine,
        "mailbox_source_repository": PostgresMailboxSourceRepository(engine),
        "sweep_run_repository": PostgresMailboxSweepRunRepository(engine),
        "incident_repository": PostgresMailboxHealthIncidentRepository(engine),
        "notification_repository": PostgresMailboxHealthNotificationRepository(engine),
        "needs_you_repository": PostgresNeedsYouRepository(engine),
        "audit_repository": PostgresAuditRepository(engine),
    }


def _run(repos, *, credentials=None):
    """Thin wrapper around `evl.run_evaluation` so the §6.A' Decision D
    regression tests below (and the Decision C coexistence test) don't
    have to repeat the full repository-argument list at every call
    site."""
    return evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=FakePushoverClient(),
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=credentials,
    )


def _complete_failed_run(repos, mailbox_id: str, *, error_code: str, error_detail: str, trigger: str = "SCHEDULED"):
    """Create and complete one `FAILED` `MailboxSweepRun` with the given
    `error_code`/`error_detail` — a small helper shared by the Decision D
    regression tests below (natural call-sequence ordering, never a
    backdated timestamp, is what gives each of these tests its real
    before/after relationship — `completed_at` is always a genuine
    `utc_now()` stamped at the moment this helper is called)."""
    run = repos["sweep_run_repository"].create_run(mailbox_id=mailbox_id, trigger=trigger)
    return repos["sweep_run_repository"].complete_run(
        run.sweep_run_id, new_status="FAILED", folders_attempted=[], messages_seen=0, messages_new=0,
        evidence_created=0, duplicates=0, quarantined=0, failures=1,
        error_code=error_code, error_detail=error_detail,
    )


def _complete_succeeded_run_and_record_success(repos, mailbox_id: str, *, trigger: str = "MANUAL"):
    """Create+complete one real `SUCCEEDED` `MailboxSweepRun` AND stamp
    `mailbox.last_successful_sweep_at` from its own `completed_at`
    (Architect Finding A's own "both, not either alone" recovery-proof
    requirement) — the exact two-step real recovery sequence
    `services/mailbox/sweep.py` performs in production, reproduced here
    for test setup."""
    run = repos["sweep_run_repository"].create_run(mailbox_id=mailbox_id, trigger=trigger)
    completed = repos["sweep_run_repository"].complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
    )
    repos["mailbox_source_repository"].record_microsoft_sweep_success(mailbox_id, swept_at=completed.completed_at)
    return completed


def _insert_gmail_oauth_state(engine, *, mailbox_id: str, expires_at, consumed_at=None):
    """Test-only direct row insertion (mirrors
    `tests/persistence/test_mailbox_gmail_oauth_state_repository.py`'s
    own identical pattern for constructing a row with a precisely
    controlled `expires_at`) — the real
    `PostgresMailboxGmailOAuthStateRepository.create_state` always
    stamps a fixed, real TTL (`services.mailbox.gmail.oauth_state
    .STATE_TTL_SECONDS`), which cannot by itself construct an
    ALREADY-expired row for a regression test. This bypasses only that
    TTL stamping, never any real production code path."""
    with session_scope(engine) as session:
        session.add(
            MailboxGmailOAuthStateRow(
                state=identity.generate_id(),
                mailbox_id=mailbox_id,
                created_at=utc_now(),
                expires_at=expires_at,
                consumed_at=consumed_at,
            )
        )


def test_full_pipeline_incident_to_accepted_notification_and_recovery():
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Pipeline Test", email_address=f"pipeline-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="token revoked")

    fake_pushover = FakePushoverClient(result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200))
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    # No pending OAuth state exists at all for this mailbox -> AUTH_REQUIRED
    # fires immediately (module docstring's own documented disambiguation).
    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )
    assert result["incidents_created"] == 1
    assert result["notifications_claimed"] == 1
    assert result["notification_records"][0]["outcome"] == "ACCEPTED"
    assert len(fake_pushover.calls) == 1

    incident = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert incident is not None
    needs_you_item = repos["needs_you_repository"].find_by_dedupe_key(ITEM_TYPE_MAILBOX_HEALTH_INCIDENT, incident.incident_id)
    assert needs_you_item is not None
    assert needs_you_item.status == "OPEN"

    # A second evaluation pass with nothing new to claim is a quiet no-op
    # (idempotent — no duplicate incident/notification).
    result2 = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
    )
    assert result2["incidents_created"] == 0
    assert result2["notifications_claimed"] == 0

    # Real recovery (PID §118.H): reconnect + a real successful sweep,
    # AND the mailbox's own canonical last_successful_sweep_at genuinely
    # advancing past the incident's start (Architect Finding A) — in
    # production this stamp is made by the real sweep executor
    # (`services/mailbox/sweep.py`) immediately after a run reaches
    # SUCCEEDED, which this test reproduces explicitly here.
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    run = repos["sweep_run_repository"].create_run(mailbox_id=mailbox.mailbox_id, trigger="MANUAL")
    completed_run = repos["sweep_run_repository"].complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
    )
    repos["mailbox_source_repository"].record_microsoft_sweep_success(
        mailbox.mailbox_id, swept_at=completed_run.completed_at
    )

    result3 = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )
    assert result3["incidents_resolved"] == 1
    assert result3["notifications_claimed"] == 1
    assert result3["notification_records"][0]["outcome"] == "ACCEPTED"

    resolved_incident = repos["incident_repository"].get_incident(incident.incident_id)
    assert resolved_incident.status == "RESOLVED"
    resolved_needs_you_item = repos["needs_you_repository"].get_needs_you_item(needs_you_item.item_id)
    assert resolved_needs_you_item.status == "RESOLVED"


def test_paused_mailbox_is_structurally_excluded():
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Disabled Mailbox", email_address=f"disabled-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    repos["mailbox_source_repository"].disable_mailbox(mailbox.mailbox_id)

    fake_pushover = FakePushoverClient()
    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
    )
    assert result["incidents_created"] == 0
    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is None


def test_repeated_outage_episodes_produce_two_distinct_incidents_and_needs_you_items():
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Repeated Outage", email_address=f"repeat-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="first outage")

    fake_pushover = FakePushoverClient()

    def _evaluate():
        return evl.run_evaluation(
            engine=repos["engine"],
            mailbox_source_repository=repos["mailbox_source_repository"],
            sweep_run_repository=repos["sweep_run_repository"],
            incident_repository=repos["incident_repository"],
            notification_repository=repos["notification_repository"],
            needs_you_repository=repos["needs_you_repository"],
            audit_repository=repos["audit_repository"],
            pushover_client=fake_pushover,
            worker_id="test-worker",
            notification_limit=50,
            claim_timeout_seconds=900,
        )

    _evaluate()
    first_incident = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert first_incident is not None
    first_needs_you_item = repos["needs_you_repository"].find_by_dedupe_key(ITEM_TYPE_MAILBOX_HEALTH_INCIDENT, first_incident.incident_id)
    assert first_needs_you_item is not None

    # recover — both a real successful sweep run AND
    # last_successful_sweep_at genuinely advancing past the incident's
    # start (Architect Finding A; see equivalent comment above).
    repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    run = repos["sweep_run_repository"].create_run(mailbox_id=mailbox.mailbox_id, trigger="MANUAL")
    completed_run = repos["sweep_run_repository"].complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
    )
    repos["mailbox_source_repository"].record_microsoft_sweep_success(
        mailbox.mailbox_id, swept_at=completed_run.completed_at
    )
    _evaluate()
    assert repos["incident_repository"].get_incident(first_incident.incident_id).status == "RESOLVED"

    # fail again
    repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="second outage")
    _evaluate()
    second_incident = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert second_incident is not None
    assert second_incident.incident_id != first_incident.incident_id

    second_needs_you_item = repos["needs_you_repository"].find_by_dedupe_key(ITEM_TYPE_MAILBOX_HEALTH_INCIDENT, second_incident.incident_id)
    assert second_needs_you_item is not None
    assert second_needs_you_item.item_id != first_needs_you_item.item_id

    # the first incident/item remain provably untouched
    assert repos["incident_repository"].get_incident(first_incident.incident_id).status == "RESOLVED"
    assert repos["needs_you_repository"].get_needs_you_item(first_needs_you_item.item_id).status == "RESOLVED"


# ---------------------------------------------------------------------
# Pre-dispatch suppression — end-to-end through the REAL decision path
# (WO §6.A.1.C, §12 criterion 21; closes the Independent Auditor's
# AMBER-verdict gap against commit 4c4215edf100fcbdc8c0da61e3236cec8f47b2ac:
# `tests/persistence/test_mailbox_health_repository.py`'s own
# `test_pre_dispatch_suppression_cancels_without_a_send_for_each_pre_claim_state`
# calls `cancel_claimed` directly and never exercises
# `_process_notifications`/`run_evaluation`, never touches a Pushover
# client, and never asserts an `AuditEvent`. These tests close exactly
# that gap: every assertion below is driven by a real
# `evl.run_evaluation(...)` call — the actual script entry point — never
# by calling the repository's `cancel_claimed` primitive directly.)
# ---------------------------------------------------------------------


def _put_notification_in_pre_claim_state(repos, notification_repository, notification, pre_claim_state: str):
    """Engineers the three required pre-claim states ahead of the real
    decision path under test. This is setup scaffolding only — forcing
    timing (a past `next_attempt_at`/`claimed_at`) so the notification is
    genuinely due/reclaimable when `run_evaluation` itself calls
    `claim_due_or_expired` — never a stand-in for the suppression
    behaviour itself, which is proven entirely by the `run_evaluation`
    call each test makes afterward."""
    if pre_claim_state == "PENDING":
        return  # the fresh-created state already

    claim = notification_repository.claim_due_or_expired(limit=10, claimed_by="setup-worker", claim_timeout_seconds=900)[0]
    assert claim.notification_id == notification.notification_id

    if pre_claim_state == STATE_RETRYABLE_FAILURE:
        notification_repository.complete_claim(
            claim.notification_id, claim_generation=claim.claim_generation, outcome=STATE_RETRYABLE_FAILURE,
            next_attempt_at=utc_now() - timedelta(seconds=1),
        )
    elif pre_claim_state == "IN_PROGRESS":
        # Leave the claim IN_PROGRESS (no `complete_claim` call — a
        # simulated worker crash), then age `claimed_at` so it reads as
        # an expired, reclaimable claim to `run_evaluation`'s own
        # internal `claim_due_or_expired` call.
        with session_scope(repos["engine"]) as session:
            row = session.get(MailboxHealthNotificationRow, claim.notification_id)
            row.claimed_at = utc_now() - timedelta(seconds=1000)
            session.flush()
    else:
        raise AssertionError(f"unhandled pre_claim_state {pre_claim_state!r}")


@pytest.mark.parametrize("pre_claim_state", sorted(PRE_DISPATCH_SUPPRESSIBLE_STATES))
def test_run_evaluation_cancels_pre_claim_state_without_pushover_send_or_resolved_incident_sidestep(pre_claim_state):
    """Proves WO §6.A.1.C / §12 criterion 21 end-to-end through the real
    evaluator decision path (`evl.run_evaluation` ->
    `_process_notifications`), for each of the 3 required pre-claim
    states — not via the repository's `cancel_claimed` primitive called
    directly:

    1. the notification genuinely transitions to CANCELLED via
       `run_evaluation` itself;
    2. the fake Pushover client recorded ZERO calls;
    3. a real `MAILBOX_HEALTH_NOTIFICATION_CANCELLED` AuditEvent was
       recorded against the real AuditEvent store, referencing the
       correct notification/incident.
    """
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Pre-Dispatch Suppression Test",
        email_address=f"predispatch-{pre_claim_state.lower()}-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    incident = repos["incident_repository"].create_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    ).incident
    notification = repos["notification_repository"].create_notification(
        incident_id=incident.incident_id, purpose=PURPOSE_INITIAL
    ).notification

    _put_notification_in_pre_claim_state(repos, repos["notification_repository"], notification, pre_claim_state)

    # The incident resolves WHILE the notification still sits in
    # `pre_claim_state` — exactly the race WO §6.A.1.C exists to guard
    # against.
    repos["incident_repository"].resolve_incident(incident.incident_id, recovery_evidence={"proof": "test"})

    fake_pushover = FakePushoverClient()
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )

    # 1. The real decision path cancelled it — never a direct
    # `cancel_claimed` call from this test.
    assert result["notification_records"] == [{"notification_id": notification.notification_id, "outcome": "CANCELLED"}]
    final = repos["notification_repository"].get_notification(notification.notification_id)
    assert final.state == STATE_CANCELLED
    assert final.cancellation_reason is not None
    assert final.cancellation_reason["incident_id"] == incident.incident_id

    # 2. Zero Pushover calls occurred for this notification.
    assert fake_pushover.calls == []

    # 3. A real MAILBOX_HEALTH_NOTIFICATION_CANCELLED AuditEvent was
    # recorded against the real AuditEvent store, referencing the right
    # notification/incident.
    audit_events = repos["audit_repository"].list_by_subject("MailboxHealthNotification", notification.notification_id)
    cancellation_events = [e for e in audit_events if e.event_type == "MAILBOX_HEALTH_NOTIFICATION_CANCELLED"]
    assert len(cancellation_events) == 1
    cancellation_event = cancellation_events[0]
    assert cancellation_event.subject_id == notification.notification_id
    assert cancellation_event.correlation_id == incident.incident_id
    assert cancellation_event.payload["incident_id"] == incident.incident_id
    assert cancellation_event.payload["purpose"] == PURPOSE_INITIAL

    # No other event type (in particular never *_ATTEMPTED/_ACCEPTED/
    # _FAILED) was ever recorded for this notification — proving no
    # dispatch attempt occurred at the audit layer either.
    assert {e.event_type for e in audit_events} == {"MAILBOX_HEALTH_NOTIFICATION_CANCELLED"}


def test_run_evaluation_still_sends_recovery_notification_through_real_decision_path_despite_resolved_incident():
    """The symmetric positive case (WO §12 criterion 21's own second
    half), proven through the same real decision path: a `RECOVERY`
    notification is NOT subject to pre-dispatch suppression and DOES get
    sent via the fake Pushover client — via `run_evaluation`, never a
    direct repository call — even though its own incident is already
    `RESOLVED` (expected: RECOVERY notifications exist precisely to
    report that resolution)."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Recovery Not Suppressed Test",
        email_address=f"recovery-not-suppressed-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    incident = repos["incident_repository"].create_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    ).incident
    repos["incident_repository"].resolve_incident(incident.incident_id, recovery_evidence={"proof": "test"})
    recovery_notification = repos["notification_repository"].create_notification(
        incident_id=incident.incident_id, purpose=PURPOSE_RECOVERY
    ).notification

    fake_pushover = FakePushoverClient(result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200))
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )

    assert result["notification_records"] == [{"notification_id": recovery_notification.notification_id, "outcome": "ACCEPTED"}]
    assert len(fake_pushover.calls) == 1

    final = repos["notification_repository"].get_notification(recovery_notification.notification_id)
    assert final.state == "ACCEPTED"

    audit_events = repos["audit_repository"].list_by_subject("MailboxHealthNotification", recovery_notification.notification_id)
    assert {e.event_type for e in audit_events} == {
        "MAILBOX_HEALTH_NOTIFICATION_ATTEMPTED",
        "MAILBOX_HEALTH_NOTIFICATION_ACCEPTED",
    }


# ---------------------------------------------------------------------
# WO-BAGMAN-118 §6.A' Decision C — legacy `MAILBOX_AUTH_REQUIRED` vs.
# episode-scoped `MAILBOX_HEALTH_INCIDENT` coexistence (§12 criterion 28)
# ---------------------------------------------------------------------


def test_resolving_legacy_mailbox_auth_required_does_not_resolve_a_coexisting_mailbox_health_incident():
    """Both item types may legitimately be OPEN simultaneously for the
    same mailbox; resolving/auto-resolving the legacy, mailbox-scoped
    `MAILBOX_AUTH_REQUIRED` item (via `connection_state` reaching
    `CONNECTED`) must NOT resolve a coexisting `MAILBOX_HEALTH_INCIDENT`
    (nor its own `NeedsYouItem`) — only the incident's own
    `detect_recovery` proof (PID §118.H) ever does that. Microsoft's
    existing legacy behaviour (§6.A item 4) is independently
    re-confirmed completely unaffected."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Legacy Episode Coexistence", email_address=f"coexist-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_MICROSOFT_GRAPH,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="token failed")

    # The legacy, mailbox-scoped item — Microsoft's own pre-existing,
    # already-wired mechanism (§6.A item 4), exercised directly here.
    sync_needs_you_for_connection_state(
        repos["needs_you_repository"], mailbox, actor_type="SYSTEM", actor_id="mailboxes-microsoft-router"
    )
    legacy_item = repos["needs_you_repository"].find_by_dedupe_key(ITEM_TYPE_MAILBOX_AUTH_REQUIRED, mailbox.mailbox_id)
    assert legacy_item is not None
    assert legacy_item.status == "OPEN"

    # The new, episode-scoped incident — created by the real evaluator
    # (source 1: no pending OAuth-state record exists for this mailbox).
    _run(repos)
    incident = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert incident is not None
    incident_needs_you_item = repos["needs_you_repository"].find_by_dedupe_key(
        ITEM_TYPE_MAILBOX_HEALTH_INCIDENT, incident.incident_id
    )
    assert incident_needs_you_item is not None
    assert incident_needs_you_item.status == "OPEN"

    # Both coexist, OPEN, simultaneously, for the SAME mailbox.
    assert legacy_item.status == "OPEN"
    assert incident.status == "OPEN"

    # Reconnect completes — connection_state reaches CONNECTED. Microsoft's
    # own legacy mechanism auto-resolves ITS OWN item...
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    sync_needs_you_for_connection_state(
        repos["needs_you_repository"], mailbox, actor_type="SYSTEM", actor_id="mailboxes-microsoft-router"
    )
    legacy_item_after = repos["needs_you_repository"].get_needs_you_item(legacy_item.item_id)
    assert legacy_item_after.status == "RESOLVED"  # Microsoft's existing legacy behaviour, unaffected

    # ...but the coexisting MAILBOX_HEALTH_INCIDENT and its own
    # NeedsYouItem remain genuinely OPEN — connection_state reaching
    # CONNECTED alone is never proof of recovery for this class
    # (PID §118.H) — neither directly, nor via a real evaluator pass
    # (no sweep has succeeded yet, so the real decision path detects no
    # recovery either).
    assert repos["incident_repository"].get_incident(incident.incident_id).status == "OPEN"
    assert repos["needs_you_repository"].get_needs_you_item(incident_needs_you_item.item_id).status == "OPEN"

    second = _run(repos)
    assert second["incidents_resolved"] == 0
    assert repos["incident_repository"].get_incident(incident.incident_id).status == "OPEN"
    assert repos["needs_you_repository"].get_needs_you_item(incident_needs_you_item.item_id).status == "OPEN"

    # Only the incident's own real recovery proof — a genuinely
    # successful sweep AND last_successful_sweep_at advancing past the
    # incident's start — ever closes it.
    _complete_succeeded_run_and_record_success(repos, mailbox.mailbox_id)
    third = _run(repos)
    assert third["incidents_resolved"] == 1
    assert repos["incident_repository"].get_incident(incident.incident_id).status == "RESOLVED"
    assert repos["needs_you_repository"].get_needs_you_item(incident_needs_you_item.item_id).status == "RESOLVED"


# ---------------------------------------------------------------------
# WO-BAGMAN-118 §6.A' Decision D — AUTH_REQUIRED/OAUTH_RECONNECT_INCOMPLETE
# are independent, never-mutually-exclusive conditions; AUTH_REQUIRED is
# evidenced by two fully independent, provider-neutral sources (§12
# criterion 29, all 7 required regression tests, items a-g below map
# 1:1 onto the WO's own numbered list).
# ---------------------------------------------------------------------


def test_decision_d_1_real_gmail_incident_sequence_produces_two_distinct_simultaneous_incidents():
    """Required regression test 1 — the real Gmail incident sequence: a
    genuine, confirmed TOKEN_REFRESH_FAILED token failure, independent of
    and preceding a LATER, separate, abandoned-and-expired reconnect
    attempt for the SAME mailbox. Both AUTH_REQUIRED (source 2, durable)
    and OAUTH_RECONNECT_INCOMPLETE must be created as two distinct,
    simultaneously-OPEN incidents, neither suppressing the other."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Gmail Real Sequence", email_address=f"gmail-seq-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    # A genuine, confirmed prior credential failure — connected, then a
    # live token refresh failed (mirrors the real 2026-10-02 incident).
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(
        mailbox.mailbox_id, error_detail="invalid_grant: token refresh failed"
    )
    _complete_failed_run(
        repos, mailbox.mailbox_id, error_code=SweepFailureReason.TOKEN_REFRESH_FAILED, error_detail="invalid_grant"
    )

    # LATER, a SEPARATE, abandoned reconnect attempt for the same mailbox
    # (mirrors the real 2026-10-18 abandoned reconnect) — initiated, then
    # left to expire with no completion.
    repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)  # idempotent no-op (already AUTH_REQUIRED)
    _insert_gmail_oauth_state(repos["engine"], mailbox_id=mailbox.mailbox_id, expires_at=utc_now() - timedelta(seconds=1))

    result = _run(repos)

    auth_required = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    reconnect_incomplete = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE
    )
    assert auth_required is not None
    assert reconnect_incomplete is not None
    assert auth_required.incident_id != reconnect_incomplete.incident_id
    assert result["incidents_created"] == 2


def test_decision_d_2_stale_prior_episode_row_excluded_and_source1_independently_agrees():
    """Required regression test 2 — an OLD TOKEN_REFRESH_FAILED row dated
    BEFORE a subsequent genuine `last_successful_sweep_at` advancement
    must NOT be picked up by source 2 for a LATER, unrelated trigger
    condition; source 1 (`connection_state`) independently agrees the
    mailbox is no longer AUTH_REQUIRED after its successful sweep."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Stale Prior Episode", email_address=f"stale-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="old token refresh failure")
    _complete_failed_run(
        repos, mailbox.mailbox_id, error_code=SweepFailureReason.TOKEN_REFRESH_FAILED, error_detail="old invalid_grant"
    )

    # Genuine recovery: reconnect + a real successful sweep, AND
    # last_successful_sweep_at genuinely advances PAST the old failure.
    repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    _complete_succeeded_run_and_record_success(repos, mailbox.mailbox_id)

    # A LATER, UNRELATED trigger condition: 3 consecutive
    # PERSISTENT_PROVIDER_ERROR failures (never TOKEN_REFRESH_FAILED).
    for _ in range(3):
        _complete_failed_run(repos, mailbox.mailbox_id, error_code=SweepFailureReason.PROVIDER_ERROR, error_detail="provider down")

    _run(repos)

    # The stale TOKEN_REFRESH_FAILED row is NOT picked up by source 2.
    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is None
    # Source 1 independently agrees: connection_state is CONNECTED.
    refreshed = repos["mailbox_source_repository"].get_mailbox(mailbox.mailbox_id)
    assert refreshed.connection_state == "CONNECTED"
    # Only the unrelated trigger's own incident was created.
    assert repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR
    ) is not None


def test_decision_d_3_imap_synchronous_auth_failure_fires_via_source1_alone_no_sweep_runs():
    """Required regression test 3 — an IMAP mailbox's synchronous login
    failure, with NO `mailbox_sweep_runs` row of any kind present, must
    immediately produce an AUTH_REQUIRED incident via source 1 alone."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="IMAP Sync Failure", email_address=f"imap-sync-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_IMAP,
    )
    # Mirrors the real `mailboxes_imap.py::connect_imap` flow exactly:
    # begin the (synchronous) connect attempt, then record the real
    # login failure — IMAP has no OAuth-state concept at all, so no
    # pending-state record is ever created for this mailbox.
    repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(
        mailbox.mailbox_id, error_detail="IMAP login failed: invalid credentials"
    )
    assert repos["sweep_run_repository"].list_runs(mailbox_id=mailbox.mailbox_id, limit=None) == []

    result = _run(repos)

    assert result["incidents_created"] == 1
    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is not None
    assert repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE
    ) is None


def test_decision_d_4_unexpired_reconnect_with_no_independent_evidence_produces_neither_class():
    """Required regression test 4 — a fresh, unexpired reconnect flow
    with NO qualifying TOKEN_REFRESH_FAILED row at all since the last
    success must produce NEITHER AUTH_REQUIRED (neither source fires)
    NOR OAUTH_RECONNECT_INCOMPLETE (not yet expired)."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Fresh Unexpired Reconnect", email_address=f"fresh-reconnect-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    # A fresh, still-unexpired reconnect flow — the real repository's own
    # default TTL (well into the future).
    PostgresMailboxGmailOAuthStateRepository(repos["engine"]).create_state(mailbox_id=mailbox.mailbox_id)

    result = _run(repos)

    assert result["incidents_created"] == 0
    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is None
    assert repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE
    ) is None


def test_decision_d_5_durable_evidence_survives_a_still_unexpired_reconnect():
    """Required regression test 5 — a qualifying TOKEN_REFRESH_FAILED row
    exists after the checkpoint AND a fresh, still-unexpired pending
    OAuth state now ALSO exists for the same mailbox. The evaluator must
    still independently create/sustain AUTH_REQUIRED from source 2 — the
    in-flight reconnect attempt must not silence already-durably-
    evidenced proof of the prior failure."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Durable Survives In Flight", email_address=f"durable-inflight-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="token refresh failed")
    _complete_failed_run(repos, mailbox.mailbox_id, error_code=SweepFailureReason.TOKEN_REFRESH_FAILED, error_detail="invalid_grant")

    # A fresh, still-unexpired reconnect now ALSO exists for the same
    # mailbox (begin_microsoft_connect itself is an idempotent no-op
    # here — already AUTH_REQUIRED).
    repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    PostgresMailboxGmailOAuthStateRepository(repos["engine"]).create_state(mailbox_id=mailbox.mailbox_id)

    _run(repos)

    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is not None
    assert repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE
    ) is None


def test_decision_d_6_durable_evidence_sustains_auth_required_after_connected_until_real_sweep_succeeds():
    """Required regression test 6 — a qualifying TOKEN_REFRESH_FAILED row
    exists at or after the checkpoint; the mailbox then completes a
    genuine OAuth reconnect (connection_state reaches CONNECTED), but no
    sweep has yet completed. The evaluator must still correctly
    create/reaffirm AUTH_REQUIRED from source 2 — the live
    connection_state == CONNECTED signal alone does not retire it — and
    only a subsequent, genuinely successful sweep advancing
    last_successful_sweep_at past the row stops source 2 from continuing
    to qualify it (PID §118.H's own recovery-proof principle, applied
    consistently to creation/reaffirmation, not just closure)."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Reconnect Completed No Sweep Yet", email_address=f"reconnect-nosweep-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(mailbox.mailbox_id, error_detail="token refresh failed")
    _complete_failed_run(repos, mailbox.mailbox_id, error_code=SweepFailureReason.TOKEN_REFRESH_FAILED, error_detail="invalid_grant")

    first = _run(repos)  # creates AUTH_REQUIRED via source 1 (no pending state yet exists)
    incident = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert incident is not None
    assert first["incidents_created"] == 1
    first_incident_id = incident.incident_id

    # Reconnect completes — connection_state reaches CONNECTED — but no
    # sweep has succeeded yet (last_successful_sweep_at unchanged).
    repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)

    second = _run(repos)
    # Source 2 alone still sustains it — the SAME incident, reaffirmed,
    # never resolved, never recreated. The REAL discriminator: this
    # incident is genuinely REAFFIRMED this cycle (last_observed_at
    # genuinely advances, `incidents_reaffirmed` is counted) — proving
    # source 2 genuinely fired even though `connection_state` is now
    # `CONNECTED` (`detect_auth_required(mailbox)` is `False`) and no
    # pending-state record exists for this mailbox at all this cycle —
    # with NO source 2 at all (the pre-correction evaluator's own real
    # defect), this mailbox's entire trigger-detection block would be
    # skipped outright this cycle, and this incident would sit
    # un-reaffirmed (`incidents_reaffirmed == 0`, `last_observed_at`
    # unchanged) despite genuinely still being unresolved.
    still_open = repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert still_open is not None
    assert still_open.incident_id == first_incident_id
    assert second["incidents_created"] == 0
    assert second["incidents_resolved"] == 0
    assert still_open.last_observed_at > incident.last_observed_at
    second_mailbox_report = next(r for r in second["mailbox_reports"] if r["mailbox_id"] == mailbox.mailbox_id)
    assert second_mailbox_report["incidents_reaffirmed"] == 1

    # Only a subsequent, genuinely successful sweep advancing
    # last_successful_sweep_at past the row finally lets it resolve.
    _complete_succeeded_run_and_record_success(repos, mailbox.mailbox_id)
    third = _run(repos)
    assert third["incidents_resolved"] == 1
    assert repos["incident_repository"].get_incident(first_incident_id).status == "RESOLVED"


def test_decision_d_7_never_connected_abandoned_expired_reconnect_produces_only_oauth_reconnect_incomplete():
    """Required regression test 7 (Architect correction, 2026-10-22) — a
    never-connected ACTIVE Gmail mailbox (no qualifying
    TOKEN_REFRESH_FAILED row ever) whose first-ever OAuth reconnect is
    abandoned and allowed to expire must produce EXACTLY ONE
    OAUTH_RECONNECT_INCOMPLETE incident and NO AUTH_REQUIRED incident
    (source 1 correctly suppressed since pending_expired is True, not
    None; source 2 correctly finds no qualifying row at all); repeated
    evaluation must create no duplicate notification; and this negative
    case must coexist correctly with tests 1 and 6's own positive cases
    on the same corrected evaluator (proven by those tests' own,
    independent passing in this same module)."""
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Never Connected Abandoned", email_address=f"never-connected-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    # First-ever OAuth initiation — NOT a confirmed failure, purely
    # begin_microsoft_connect's own initiating transition.
    repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    _insert_gmail_oauth_state(repos["engine"], mailbox_id=mailbox.mailbox_id, expires_at=utc_now() - timedelta(seconds=1))
    assert repos["sweep_run_repository"].list_runs(mailbox_id=mailbox.mailbox_id, limit=None) == []

    first = _run(repos)

    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is None
    reconnect_incident = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE
    )
    assert reconnect_incident is not None
    assert first["incidents_created"] == 1

    notifications_after_first = repos["notification_repository"].list_by_incident(reconnect_incident.incident_id)
    assert len(notifications_after_first) == 1

    # Repeated evaluation: the same incident, reaffirmed — no duplicate
    # incident, no duplicate notification, still no AUTH_REQUIRED.
    second = _run(repos)
    assert second["incidents_created"] == 0
    still_open = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE
    )
    assert still_open is not None
    assert still_open.incident_id == reconnect_incident.incident_id
    assert len(repos["notification_repository"].list_by_incident(reconnect_incident.incident_id)) == 1
    assert repos["incident_repository"].get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED) is None


# ---------------------------------------------------------------------
# WO-BAGMAN-118 final Independent Audit, AMBER item 1 (§12 criterion 16)
# — adversarial-marker proof that no financial evidence, mailbox
# content, or OAuth token material ever reaches this WO's own new
# surfaces (the two new tables, the evaluator's own AuditEvents, the
# Pushover outbound payload). Mirrors
# `tests/security/test_auth_correlation_and_sanitization.py`'s own
# adversarial-marker test shape exactly (WO-117's own §6.A″ precedent):
# distinctive, domain-shaped fake marker strings, injected at a
# realistic point a real failure's `error_detail` could plausibly carry
# adversarial content, then proven absent from every externally-
# observable surface this evaluator produces — never merely asserted
# from code inspection alone.
#
# Injection-point scoping note (judgment call, stated honestly): the
# governing Work Order names TWO candidate injection points —
# `MailboxSweepRun.error_detail` OR a `mark_microsoft_auth_required
# (error_detail=...)` call. This test uses ONLY the former. Verified by
# hand while authoring this test: `_evaluate_mailbox` (in
# `scripts/evaluate_mailbox_health.py`) creates every new incident with
# `metadata={"last_error_code": mailbox.last_error_code,
# "last_error_detail": mailbox.last_error_detail}` — i.e. whatever was
# last passed to `mark_microsoft_auth_required`/
# `mark_microsoft_connection_error` is, BY DESIGN, stored verbatim in
# `mailbox_health_incidents.metadata`. That field is never serialized by
# any `app/api` route today (only `NeedsYouItem.metadata`, which
# deliberately carries only `mailbox_id`/`incident_id`/`failure_class`,
# is ever returned to a caller — confirmed by reading
# `app/api/routers/needs_you.py` in full), but it IS a genuine, literal
# write of raw `error_detail` content into one of this WO's two new
# tables. An adversarial marker placed in the
# `mark_microsoft_auth_required` call therefore DOES appear in
# `mailbox_health_incidents.metadata` as stored — confirmed by hand
# during authoring (not included as a committed test, since it would
# fail against current, in-scope, not-to-be-modified behaviour; this is
# a real tension with criterion 16's literal "never appears in
# mailbox_health_incidents.metadata" wording for that specific
# injection point, surfaced here for Architect/BAGMAN judgment rather
# than silently avoided). `MailboxSweepRun.error_detail`, by contrast,
# is genuinely never read into incident metadata, the Pushover payload,
# or any AuditEvent payload — `_has_durable_auth_required_evidence`
# reads only `run.error_code` — making it the injection point this test
# can prove the exclusion against without contradicting current,
# intentional, out-of-scope-to-change behaviour.
#
# RESOLVED 2026-10-23 (Architect correction, same §12 criterion 16): the
# tension named above is fixed, not merely surfaced — `_evaluate_mailbox`
# no longer stores `mailbox.last_error_detail` verbatim at all (replaced
# with a bounded `last_error_detail_present` boolean); see
# `test_adversarial_markers_in_mailbox_last_error_detail_never_leak_onto_any_new_surface`
# below, which exercises exactly the `mark_microsoft_auth_required`
# injection point this note once left untested, and now passes.
# ---------------------------------------------------------------------

_FINANCIAL_MARKER = "INVOICE-CANARY-8f2c: account_number=4111-CANARY-1111-0000, balance=$CANARY_94821.33"
_MAILBOX_CONTENT_MARKER = "CANARY_EMAIL_BODY_MARKER_7c3d: Subject: Confidential -- do not leak this message body"
_OAUTH_TOKEN_MARKER = "CANARY_OAUTH_TOKEN_MARKER_9f1e_refresh_token_do_not_leak"
_ALL_ADVERSARIAL_MARKERS = (_FINANCIAL_MARKER, _MAILBOX_CONTENT_MARKER, _OAUTH_TOKEN_MARKER)
_ADVERSARIAL_SWEEP_RUN_ERROR_DETAIL = (
    "invalid_grant: token refresh failed; raw provider response captured "
    f"verbatim by the client's own error path -- {_OAUTH_TOKEN_MARKER} -- "
    f"embedded message excerpt: {_MAILBOX_CONTENT_MARKER} -- associated "
    f"billing context: {_FINANCIAL_MARKER}"
)


def test_adversarial_markers_in_sweep_run_error_detail_never_leak_onto_any_new_surface():
    """§12 criterion 16 — a realistic, confirmed `TOKEN_REFRESH_FAILED`
    `MailboxSweepRun` whose own `error_detail` happens to carry
    financial-evidence-, mailbox-content-, and OAuth-token-shaped
    adversarial content (exactly the kind of thing a raw provider/driver
    exception message can accidentally embed — mirrors `_SQL_MARKER`'s
    own "fake SQL fragment embedded in a real exception path" shape;
    `services/mailbox/sweep.py`'s own real call sites populate this
    field directly from caught exceptions during a live sweep) must
    never let that content reach:

    1. `mailbox_health_incidents.metadata` as stored — read back from the
       REAL repository (`get_open_incident`), never the in-process
       domain object returned at creation time;
    2. the Pushover outbound payload — the real `FakePushoverClient` test
       double, the same one wired into every other test in this file;
    3. any `AuditEvent` payload recorded during this run — queried from
       the real audit repository by this incident's own
       `correlation_id`, never assumed absent.

    This sweep run is this cycle's SOLE qualifying `AUTH_REQUIRED`
    evidence (source 2, `_has_durable_auth_required_evidence` — no
    pending OAuth state exists, but `connection_state` is deliberately
    left `CONNECTED` here, so source 1 does NOT independently fire and
    the only route by which this incident could possibly be influenced
    by the adversarial row is source 2's own read of it), which is what
    makes a clean marker-absence result here a genuine proof about this
    evaluator's own handling of that row, not an accident of a second,
    unrelated source also having fired.
    """
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Adversarial Marker Test",
        email_address=f"adversarial-marker-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    _complete_failed_run(
        repos, mailbox.mailbox_id,
        error_code=SweepFailureReason.TOKEN_REFRESH_FAILED, error_detail=_ADVERSARIAL_SWEEP_RUN_ERROR_DETAIL,
    )

    # Sanity: the adversarial content genuinely made it into the real,
    # persisted `MailboxSweepRun.error_detail` row BEFORE the evaluator
    # ever runs — otherwise an absent marker downstream would prove
    # nothing (it could mean "never leaked" OR "never present in the
    # first place").
    persisted_runs = repos["sweep_run_repository"].list_runs(mailbox_id=mailbox.mailbox_id, limit=None)
    assert any(run.error_detail == _ADVERSARIAL_SWEEP_RUN_ERROR_DETAIL for run in persisted_runs)
    # And `connection_state` is genuinely `CONNECTED` (source 1 withheld)
    # — this incident can only exist via source 2's own read of the
    # adversarial row above.
    pre_run_mailbox = repos["mailbox_source_repository"].get_mailbox(mailbox.mailbox_id)
    assert pre_run_mailbox.connection_state == "CONNECTED"

    fake_pushover = FakePushoverClient(result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200))
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )
    assert result["incidents_created"] == 1

    # 1. The REAL, PERSISTED incident row's own metadata, read back from
    # the repository — never the in-process creation-time object alone.
    incident = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    )
    assert incident is not None
    incident_metadata_text = str(incident.metadata)
    for marker in _ALL_ADVERSARIAL_MARKERS:
        assert marker not in incident_metadata_text

    # 2. The Pushover outbound payload — at least one real send occurred
    # (the INITIAL notification for the incident just created), and NONE
    # of its title/message text carries any marker.
    assert len(fake_pushover.calls) >= 1
    for call in fake_pushover.calls:
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in call["title"]
            assert marker not in call["message"]

    # 3. Every AuditEvent recorded against this incident's own
    # correlation_id during this run (MAILBOX_HEALTH_INCIDENT_CREATED +
    # MAILBOX_HEALTH_NOTIFICATION_ATTEMPTED/ACCEPTED all share
    # `correlation_id == incident.incident_id` — see
    # `scripts/evaluate_mailbox_health.py`).
    audit_events = repos["audit_repository"].list_by_correlation(incident.incident_id)
    assert len(audit_events) >= 1
    for event in audit_events:
        payload_text = str(event.payload)
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in payload_text


# ---------------------------------------------------------------------
# WO-BAGMAN-118 Architect correction, 2026-10-23 (§12 criterion 16,
# Stage A acceptance gap) — the two companion adversarial-marker proofs
# the prior AMBER-closure round's own comment block (above) explicitly
# named as a real, then-unfixed tension and deliberately left untested:
# (1) `mailbox.last_error_detail` via a direct
# `mark_microsoft_auth_required(error_detail=...)` call, and (2)
# Pushover's own `PushoverSendResult.error_detail`/`provider_request_id`.
# Both fields are now dropped/validated rather than persisted verbatim
# (see `scripts/evaluate_mailbox_health.py`'s own
# `_validate_pushover_request_id` helper and the `metadata=` literals in
# `_evaluate_mailbox`/`_process_notifications`) — these tests prove the
# fix against real, persisted Postgres rows read back through the real
# repositories, never merely in-memory objects or code inspection.
# ---------------------------------------------------------------------


def test_adversarial_markers_in_mailbox_last_error_detail_never_leak_onto_any_new_surface():
    """§12 criterion 16 — the injection point the prior round's comment
    block (above) identified as a genuine, then-unfixed gap: a real
    `mark_microsoft_auth_required(error_detail=...)` call whose
    `error_detail` carries financial-evidence-, mailbox-content-, and
    OAuth-token-shaped adversarial content. Proves the markers never
    reach `mailbox_health_incidents.metadata`, `NeedsYouItem.metadata`,
    `mailbox_health_notifications.provider_response_metadata`, any
    `AuditEvent.payload` for this incident's correlation_id, or the
    outbound `FakePushoverClient` call's title/message — all read back
    from the REAL repositories, never the in-process creation-time
    objects alone. Also confirms the bounded replacement field
    (`last_error_detail_present`) is `True`, proving the presence signal
    still works even though the content itself is gone.
    """
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Adversarial Last Error Detail Test",
        email_address=f"adversarial-last-error-detail-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    # `begin_microsoft_connect` already lands on AUTH_REQUIRED (its own
    # NOT_CONFIGURED -> AUTH_REQUIRED step, with no error_detail); a
    # same-state `mark_microsoft_auth_required` call right after it would
    # be an idempotent no-op (`_connection_transition`'s own
    # already-in-that-state short-circuit) and would NOT apply
    # `error_detail` at all. Routing through a genuine `CONNECTED ->
    # AUTH_REQUIRED` transition first (mirrors
    # `test_decision_d_6_durable_evidence_sustains_auth_required_after_connected_until_real_sweep_succeeds`'s
    # own identical sequence) is what makes this a real transition that
    # actually persists `error_detail`.
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    adversarial_last_error_detail = (
        "AADSTS700082: token refresh failed; raw provider response captured "
        f"verbatim by the client's own error path -- {_OAUTH_TOKEN_MARKER} -- "
        f"embedded message excerpt: {_MAILBOX_CONTENT_MARKER} -- associated "
        f"billing context: {_FINANCIAL_MARKER}"
    )
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(
        mailbox.mailbox_id, error_detail=adversarial_last_error_detail
    )

    # Sanity: the adversarial content genuinely made it into the real,
    # persisted mailbox row's own `last_error_detail` BEFORE the
    # evaluator ever runs — otherwise an absent marker downstream would
    # prove nothing.
    pre_run_mailbox = repos["mailbox_source_repository"].get_mailbox(mailbox.mailbox_id)
    assert pre_run_mailbox.last_error_detail == adversarial_last_error_detail
    assert pre_run_mailbox.last_error_code == "AUTH_REQUIRED"

    fake_pushover = FakePushoverClient(
        result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200, provider_request_id="deadbeef")
    )
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )
    assert result["incidents_created"] == 1

    # 1. The REAL, PERSISTED incident row's own metadata, read back from
    # the repository.
    incident = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    )
    assert incident is not None
    incident_metadata_text = str(incident.metadata)
    for marker in _ALL_ADVERSARIAL_MARKERS:
        assert marker not in incident_metadata_text
    assert incident.metadata["last_error_code"] == "AUTH_REQUIRED"
    assert incident.metadata["last_error_detail_present"] is True

    # 2. The real, persisted `NeedsYouItem`'s own metadata.
    needs_you_item = repos["needs_you_repository"].find_by_dedupe_key(
        ITEM_TYPE_MAILBOX_HEALTH_INCIDENT, incident.incident_id
    )
    assert needs_you_item is not None
    needs_you_metadata_text = str(needs_you_item.metadata)
    for marker in _ALL_ADVERSARIAL_MARKERS:
        assert marker not in needs_you_metadata_text

    # 3. The real, persisted notification row's own
    # `provider_response_metadata`.
    notifications = repos["notification_repository"].list_by_incident(incident.incident_id)
    assert len(notifications) >= 1
    for notification in notifications:
        notification_metadata_text = str(notification.provider_response_metadata)
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in notification_metadata_text

    # 4. Every AuditEvent recorded against this incident's own
    # correlation_id during this run.
    audit_events = repos["audit_repository"].list_by_correlation(incident.incident_id)
    assert len(audit_events) >= 1
    for event in audit_events:
        payload_text = str(event.payload)
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in payload_text

    # 5. The Pushover outbound payload itself.
    assert len(fake_pushover.calls) >= 1
    for call in fake_pushover.calls:
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in call["title"]
            assert marker not in call["message"]


def test_adversarial_markers_in_pushover_error_detail_and_non_hex_request_id_never_leak_onto_any_new_surface():
    """§12 criterion 16 — the second injection point: an adversarial/
    malicious upstream Pushover response whose own `error_detail` AND
    `request` (`provider_request_id`) fields carry financial-evidence-,
    mailbox-content-, and OAuth-token-shaped content (simulating a
    compromised or malfunctioning upstream provider, not merely a
    well-formed failure). Proves the real, persisted
    `mailbox_health_notifications.provider_response_metadata` row
    carries none of it: `error_detail` is dropped from the metadata
    shape entirely (not merely emptied), and a non-hex-shaped
    `provider_request_id` (exactly this canary-shaped value) is dropped
    to `None` rather than persisted verbatim.
    """
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Adversarial Pushover Response Test",
        email_address=f"adversarial-pushover-response-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(
        mailbox.mailbox_id, error_detail="benign trigger for pushover-response adversarial test"
    )

    adversarial_provider_response_detail = (
        "Pushover reported a non-1 status; raw errors[] joined verbatim -- "
        f"{_OAUTH_TOKEN_MARKER} -- embedded message excerpt: {_MAILBOX_CONTENT_MARKER} "
        f"-- associated billing context: {_FINANCIAL_MARKER}"
    )
    fake_pushover = FakePushoverClient(
        result=PushoverSendResult(
            status=PushoverOutcomeStatus.PROVIDER_ERROR,
            http_status=500,
            provider_request_id=_OAUTH_TOKEN_MARKER,  # canary-shaped, genuinely non-hex
            error_detail=adversarial_provider_response_detail,
        )
    )
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )
    assert result["incidents_created"] == 1

    incident = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    )
    assert incident is not None

    notifications = repos["notification_repository"].list_by_incident(incident.incident_id)
    assert len(notifications) >= 1
    for notification in notifications:
        assert notification.provider_response_metadata is not None
        notification_metadata_text = str(notification.provider_response_metadata)
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in notification_metadata_text
        assert "error_detail" not in notification.provider_response_metadata
        # The canary-shaped, non-hex `provider_request_id` was dropped to
        # `None` — never persisted verbatim.
        assert notification.provider_response_metadata.get("provider_request_id") is None
        assert notification.provider_response_metadata.get("provider_status") == PushoverOutcomeStatus.PROVIDER_ERROR.value
        assert notification.provider_response_metadata.get("http_status") == 500

    audit_events = repos["audit_repository"].list_by_correlation(incident.incident_id)
    assert len(audit_events) >= 1
    for event in audit_events:
        payload_text = str(event.payload)
        for marker in _ALL_ADVERSARIAL_MARKERS:
            assert marker not in payload_text


def test_pushover_genuine_hex_shaped_provider_request_id_is_retained_on_acceptance():
    """§12 criterion 16's own validation boundary, the positive case: a
    genuinely hex-token-shaped `provider_request_id` (the known-safe
    shape Pushover's own `request` field actually takes) survives the
    new `_validate_pushover_request_id` guard and is persisted verbatim
    — the guard drops unsafe shapes, it does not blank the field
    unconditionally. Also closes the WO's own Test C note: an
    `ACCEPTED` notification still retains a valid, non-`None`
    `provider_response_metadata.provider_request_id` when Pushover
    genuinely returns one.
    """
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Genuine Hex Request Id Test",
        email_address=f"genuine-hex-request-id-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(
        mailbox.mailbox_id, error_detail="trigger for genuine-request-id test"
    )

    genuine_request_id = "4f9a0c2b7d8e"
    fake_pushover = FakePushoverClient(
        result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200, provider_request_id=genuine_request_id)
    )
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    result = evl.run_evaluation(
        engine=repos["engine"],
        mailbox_source_repository=repos["mailbox_source_repository"],
        sweep_run_repository=repos["sweep_run_repository"],
        incident_repository=repos["incident_repository"],
        notification_repository=repos["notification_repository"],
        needs_you_repository=repos["needs_you_repository"],
        audit_repository=repos["audit_repository"],
        pushover_client=fake_pushover,
        worker_id="test-worker",
        notification_limit=50,
        claim_timeout_seconds=900,
        credentials=fake_credentials,
    )
    assert result["incidents_created"] == 1

    incident = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    )
    assert incident is not None
    notifications = repos["notification_repository"].list_by_incident(incident.incident_id)
    accepted = [n for n in notifications if n.state == STATE_ACCEPTED]
    assert len(accepted) >= 1
    for notification in accepted:
        assert notification.provider_response_metadata.get("provider_request_id") == genuine_request_id
        assert notification.provider_response_metadata.get("provider_status") == PushoverOutcomeStatus.OK.value


# ---------------------------------------------------------------------
# WO-BAGMAN-118 final Independent Audit, AMBER item 2 (§12 criterion 10)
# — two concurrent, FULL `run_evaluation()` orchestration runs against
# the SAME new qualifying failure must still produce exactly one
# incident and exactly one INITIAL notification. Reuses
# `tests/persistence/test_mailbox_health_repository.py`'s own
# `test_partial_unique_index_prevents_duplicate_open_incident_under_real_race`
# threading convention verbatim (genuine OS threads + a
# `threading.Barrier`, never sequential calls — the GIL and this
# codebase's own fresh-`Session`-per-call discipline make sequential
# calls misleadingly "safe" even when the underlying constraint is what
# actually matters) — but drives the race through the REAL, FULL
# evaluator entry point (`evl.run_evaluation`), never the lower-level
# repository primitives those existing tests already prove race-safe in
# isolation. Closes exactly the gap the Independent Auditor named: the
# delivered suite proved the partial unique index and the
# `(incident_id, purpose)` unique constraint are race-safe individually
# — never that the full `_evaluate_mailbox` -> `create_incident` ->
# `create_notification` orchestration sequence, invoked twice
# concurrently via the real public entry point, still converges on
# exactly one of each.
# ---------------------------------------------------------------------


def test_two_concurrent_full_run_evaluation_calls_against_the_same_new_failure_produce_exactly_one_incident_and_one_initial_notification():
    repos = _repos()
    mailbox = repos["mailbox_source_repository"].create_mailbox(
        display_name="Concurrent Evaluator Race",
        email_address=f"concurrent-evaluator-race-{utc_now().timestamp()}@example.com",
        provider_kind=PROVIDER_GOOGLE_GMAIL,
    )
    mailbox = repos["mailbox_source_repository"].begin_microsoft_connect(mailbox.mailbox_id)
    # connection_state just became AUTH_REQUIRED for the first time (no
    # pending OAuth state at all -> source 1 fires immediately, the
    # "live signal" case); no incident exists yet for this mailbox.
    mailbox = repos["mailbox_source_repository"].mark_microsoft_auth_required(
        mailbox.mailbox_id, error_detail="concurrent evaluator race test token failure"
    )
    assert repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    ) is None

    # Two fully independent repository-set instances — never a shared
    # one — each constructed exactly as `main()` does in production, both
    # against the same real underlying database. This is what makes this
    # a genuine two-worker race rather than two calls sharing in-process
    # Python state.
    repos_a = _repos()
    repos_b = _repos()
    fake_pushover_a = FakePushoverClient(result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200))
    fake_pushover_b = FakePushoverClient(result=PushoverSendResult(status=PushoverOutcomeStatus.OK, http_status=200))
    fake_credentials = PushoverCredentials(api_token="fake-token", user_key="fake-user")

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _evaluate(repos_for_worker, pushover_for_worker, worker_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            evl.run_evaluation(
                engine=repos_for_worker["engine"],
                mailbox_source_repository=repos_for_worker["mailbox_source_repository"],
                sweep_run_repository=repos_for_worker["sweep_run_repository"],
                incident_repository=repos_for_worker["incident_repository"],
                notification_repository=repos_for_worker["notification_repository"],
                needs_you_repository=repos_for_worker["needs_you_repository"],
                audit_repository=repos_for_worker["audit_repository"],
                pushover_client=pushover_for_worker,
                worker_id=worker_id,
                notification_limit=50,
                claim_timeout_seconds=900,
                credentials=fake_credentials,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=_evaluate, args=(repos_a, fake_pushover_a, "worker-a")),
        threading.Thread(target=_evaluate, args=(repos_b, fake_pushover_b, "worker-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a concurrent run_evaluation() call hung"
    assert errors == [], f"a concurrent run_evaluation() call raised: {errors!r}"

    # Exactly one OPEN incident for this (mailbox_id, failure_class) —
    # proven both via the repository's own dedupe read AND a direct row
    # count against the real table (never trusting the dedupe read alone
    # to mask a genuine duplicate row the read happens not to surface).
    incident = repos["incident_repository"].get_open_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED
    )
    assert incident is not None
    with session_scope(repos["engine"]) as session:
        incident_count = (
            session.query(MailboxHealthIncidentRow)
            .filter(
                MailboxHealthIncidentRow.mailbox_id == mailbox.mailbox_id,
                MailboxHealthIncidentRow.failure_class == FAILURE_CLASS_AUTH_REQUIRED,
            )
            .count()
        )
    assert incident_count == 1

    # Exactly one INITIAL-purpose notification for that one incident —
    # not two, not zero.
    notifications = repos["notification_repository"].list_by_incident(incident.incident_id)
    initial_notifications = [n for n in notifications if n.purpose == PURPOSE_INITIAL]
    assert len(initial_notifications) == 1

