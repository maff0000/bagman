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

from core.timestamps import utc_now
from persistence.postgres.audit_repository import PostgresAuditRepository
from persistence.postgres.mailbox_health_repository import (
    PostgresMailboxHealthIncidentRepository,
    PostgresMailboxHealthNotificationRepository,
)
from persistence.postgres.mailbox_microsoft_repository import PostgresMailboxSweepRunRepository
from persistence.postgres.mailbox_repository import PostgresMailboxSourceRepository
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
from persistence.postgres.session import get_engine
from services.mailbox.health.incident import FAILURE_CLASS_AUTH_REQUIRED, ITEM_TYPE_MAILBOX_HEALTH_INCIDENT
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

    # Real recovery (PID §118.H): reconnect + a real successful sweep.
    mailbox = repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    run = repos["sweep_run_repository"].create_run(mailbox_id=mailbox.mailbox_id, trigger="MANUAL")
    repos["sweep_run_repository"].complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
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

    # recover
    repos["mailbox_source_repository"].mark_microsoft_connected(mailbox.mailbox_id)
    run = repos["sweep_run_repository"].create_run(mailbox_id=mailbox.mailbox_id, trigger="MANUAL")
    repos["sweep_run_repository"].complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
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
