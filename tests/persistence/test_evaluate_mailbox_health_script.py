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

from datetime import timedelta

import pytest

from core.timestamps import utc_now
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.mailbox_health_models import MailboxHealthNotificationRow
from persistence.postgres.mailbox_health_repository import (
    PostgresMailboxHealthIncidentRepository,
    PostgresMailboxHealthNotificationRepository,
)
from persistence.postgres.mailbox_microsoft_repository import PostgresMailboxSweepRunRepository
from persistence.postgres.mailbox_repository import PostgresMailboxSourceRepository
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.health.incident import FAILURE_CLASS_AUTH_REQUIRED, ITEM_TYPE_MAILBOX_HEALTH_INCIDENT
from services.mailbox.health.notification import (
    PRE_DISPATCH_SUPPRESSIBLE_STATES,
    PURPOSE_INITIAL,
    PURPOSE_RECOVERY,
    STATE_CANCELLED,
    STATE_RETRYABLE_FAILURE,
)
from services.mailbox.mailbox import PROVIDER_GOOGLE_GMAIL
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
