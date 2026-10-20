"""Domain-level unit tests for `services.mailbox.health.*` (PID §118,
WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.2/§6.A.4/§12) — pure
trigger/recovery functions and the in-memory reference repositories.
Real, concurrency-proof tests against a disposable PostgreSQL instance
live in `tests/persistence/test_mailbox_health_repository.py`.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.errors import InvalidStateTransitionError
from core.timestamps import utc_now
from services.mailbox.health.incident import (
    FAILURE_CLASS_AUTH_REQUIRED,
    FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR,
    FAILURE_CLASS_STALE_INGESTION,
    InMemoryMailboxHealthIncidentRepository,
    detect_auth_required,
    detect_oauth_reconnect_incomplete,
    detect_persistent_provider_error,
    detect_recovery,
    detect_stale_ingestion,
)
from services.mailbox.health.needs_you_sync import (
    ALLOWED_ACTION_CONNECT_GMAIL_MAILBOX,
    ALLOWED_ACTION_RECONNECT_IMAP_MAILBOX,
    sync_needs_you_for_connection_state,
)
from services.mailbox.health.notification import (
    InMemoryMailboxHealthNotificationRepository,
    PURPOSE_INITIAL,
    PURPOSE_REMINDER_24H,
    STATE_ACCEPTED,
    STATE_RETRYABLE_FAILURE,
    STATE_TERMINAL_FAILURE,
    compute_next_attempt_at,
    reminder_is_due,
)
from services.mailbox.mailbox import InMemoryMailboxSourceRepository, PROVIDER_GOOGLE_GMAIL, PROVIDER_IMAP
from services.mailbox.sweep_run import InMemoryMailboxSweepRunRepository
from services.needs_you.needs_you import InMemoryNeedsYouRepository, ITEM_TYPE_MAILBOX_AUTH_REQUIRED


# ---------------------------------------------------------------------
# Trigger detection (PID §118.D)
# ---------------------------------------------------------------------


def test_stale_ingestion_detection_is_not_implemented():
    """WO §12 criterion 8: STALE_INGESTION is defined in the taxonomy
    but has no active detection logic — confirmed by test, not merely
    absence of code."""
    assert FAILURE_CLASS_STALE_INGESTION == "STALE_INGESTION"
    with pytest.raises(NotImplementedError):
        detect_stale_ingestion()


def test_detect_auth_required_excludes_non_active_mailboxes():
    repo = InMemoryMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="m", email_address="a@example.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox = repo.begin_microsoft_connect(mailbox.mailbox_id)
    assert detect_auth_required(mailbox) is True

    disabled = repo.disable_mailbox(mailbox.mailbox_id)
    assert detect_auth_required(disabled) is False


def test_detect_persistent_provider_error_requires_three_consecutive_failures():
    from core import identity

    sweep_repo = InMemoryMailboxSweepRunRepository()
    mailbox_id = identity.generate_id()

    def _run(status: str):
        run = sweep_repo.create_run(mailbox_id=mailbox_id, trigger="MANUAL")
        return sweep_repo.complete_run(
            run.sweep_run_id, new_status=status, folders_attempted=[], messages_seen=0, messages_new=0,
            evidence_created=0, duplicates=0, quarantined=0, failures=1 if status == "FAILED" else 0,
        )

    _run("FAILED")
    _run("SUCCEEDED")
    _run("FAILED")
    _run("FAILED")
    runs = sweep_repo.list_runs(mailbox_id=mailbox_id)
    # A SUCCEEDED run breaks the consecutive streak.
    assert detect_persistent_provider_error(runs, mailbox_status="ACTIVE") is False

    _run("FAILED")
    runs = sweep_repo.list_runs(mailbox_id=mailbox_id)
    assert detect_persistent_provider_error(runs, mailbox_status="ACTIVE") is True
    assert detect_persistent_provider_error(runs, mailbox_status="DISABLED") is False


def test_detect_oauth_reconnect_incomplete():
    repo = InMemoryMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="m", email_address="b@example.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox = repo.begin_microsoft_connect(mailbox.mailbox_id)
    assert detect_oauth_reconnect_incomplete(mailbox, pending_state_expired=True) is True
    assert detect_oauth_reconnect_incomplete(mailbox, pending_state_expired=False) is False


# ---------------------------------------------------------------------
# Recovery proof (PID §118.H)
# ---------------------------------------------------------------------


def test_recovery_never_closes_on_bare_connection_state_alone():
    repo = InMemoryMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="m", email_address="c@example.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox = repo.begin_microsoft_connect(mailbox.mailbox_id)
    incident_repo = InMemoryMailboxHealthIncidentRepository()
    incident = incident_repo.create_incident(mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_AUTH_REQUIRED).incident

    connected_mailbox = repo.mark_microsoft_connected(mailbox.mailbox_id)
    # connection_state flipped to CONNECTED, but NO successful sweep run
    # exists yet — must NOT be treated as recovered.
    assert detect_recovery(incident, connected_mailbox, []) is None

    sweep_repo = InMemoryMailboxSweepRunRepository()
    run = sweep_repo.create_run(mailbox_id=mailbox.mailbox_id, trigger="MANUAL")
    completed = sweep_repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
    )
    recovery_evidence = detect_recovery(incident, connected_mailbox, [completed])
    assert recovery_evidence is not None
    assert recovery_evidence["sweep_run_id"] == completed.sweep_run_id


def test_persistent_provider_error_recovery_needs_no_connection_state_change():
    repo = InMemoryMailboxSourceRepository()
    mailbox = repo.create_mailbox(display_name="m", email_address="d@example.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    incident_repo = InMemoryMailboxHealthIncidentRepository()
    incident = incident_repo.create_incident(
        mailbox_id=mailbox.mailbox_id, failure_class=FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR
    ).incident

    sweep_repo = InMemoryMailboxSweepRunRepository()
    run = sweep_repo.create_run(mailbox_id=mailbox.mailbox_id, trigger="MANUAL")
    completed = sweep_repo.complete_run(
        run.sweep_run_id, new_status="SUCCEEDED", folders_attempted=[], messages_seen=1, messages_new=1,
        evidence_created=1, duplicates=0, quarantined=0, failures=0,
    )
    # mailbox never left NOT_CONFIGURED/whatever connection_state it was
    # at — no connection_state requirement for this class.
    assert detect_recovery(incident, mailbox, [completed]) is not None


# ---------------------------------------------------------------------
# Episode mechanism (in-memory reference implementation)
# ---------------------------------------------------------------------


def test_episode_mechanism_in_memory():
    repo = InMemoryMailboxHealthIncidentRepository()
    first = repo.create_incident(mailbox_id="m1", failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert first.created is True
    duplicate = repo.create_incident(mailbox_id="m1", failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert duplicate.created is False
    assert duplicate.incident.incident_id == first.incident.incident_id

    repo.resolve_incident(first.incident.incident_id, recovery_evidence={"proof": "x"})
    with pytest.raises(InvalidStateTransitionError):
        repo.resolve_incident(first.incident.incident_id, recovery_evidence={"proof": "y"})

    second = repo.create_incident(mailbox_id="m1", failure_class=FAILURE_CLASS_AUTH_REQUIRED)
    assert second.created is True
    assert second.incident.incident_id != first.incident.incident_id


# ---------------------------------------------------------------------
# Notification claim/fencing/exhaustion/suppression (in-memory)
# ---------------------------------------------------------------------


def test_notification_claim_exhaustion_and_fencing_in_memory():
    repo = InMemoryMailboxHealthNotificationRepository()
    created = repo.create_notification(incident_id="inc-1", purpose=PURPOSE_INITIAL, max_attempts=2)
    assert created.created is True

    claim1 = repo.claim_due_or_expired(limit=10, claimed_by="worker-a", claim_timeout_seconds=900)[0]
    assert claim1.attempt_count == 1
    stale_write = repo.complete_claim(
        claim1.notification_id, claim_generation=99, outcome=STATE_ACCEPTED,
    )
    assert stale_write is None  # wrong generation — silent no-op

    updated = repo.complete_claim(
        claim1.notification_id, claim_generation=claim1.claim_generation, outcome=STATE_RETRYABLE_FAILURE,
        next_attempt_at=utc_now() - timedelta(seconds=1),
    )
    assert updated.state == STATE_RETRYABLE_FAILURE

    claim2 = repo.claim_due_or_expired(limit=10, claimed_by="worker-a", claim_timeout_seconds=900)[0]
    assert claim2.attempt_count == 2 == claim2.max_attempts
    final = repo.complete_claim(claim2.notification_id, claim_generation=claim2.claim_generation, outcome=STATE_TERMINAL_FAILURE)
    assert final.state == STATE_TERMINAL_FAILURE

    # no further claim is ever possible
    import dataclasses
    repo._by_id[claim2.notification_id] = dataclasses.replace(
        repo._by_id[claim2.notification_id], next_attempt_at=utc_now() - timedelta(seconds=1)
    )
    assert repo.claim_due_or_expired(limit=10, claimed_by="worker-b", claim_timeout_seconds=900) == []


def test_reminder_is_due_gating():
    assert reminder_is_due(None) is False

    repo = InMemoryMailboxHealthNotificationRepository()
    created = repo.create_notification(incident_id="inc-2", purpose=PURPOSE_INITIAL)
    claim = repo.claim_due_or_expired(limit=10, claimed_by="w", claim_timeout_seconds=900)[0]
    completed = repo.complete_claim(claim.notification_id, claim_generation=claim.claim_generation, outcome=STATE_ACCEPTED)

    assert reminder_is_due(completed, now=completed.completed_at + timedelta(hours=1)) is False
    assert reminder_is_due(completed, now=completed.completed_at + timedelta(hours=25)) is True


def test_reminder_purpose_at_most_once_in_memory():
    repo = InMemoryMailboxHealthNotificationRepository()
    first = repo.create_notification(incident_id="inc-3", purpose=PURPOSE_REMINDER_24H)
    second = repo.create_notification(incident_id="inc-3", purpose=PURPOSE_REMINDER_24H)
    assert first.created is True
    assert second.created is False
    assert first.notification.notification_id == second.notification.notification_id


def test_compute_next_attempt_at_is_bounded_and_increasing():
    now = utc_now()
    first = compute_next_attempt_at(1, now=now)
    second = compute_next_attempt_at(2, now=now)
    assert second > first > now


# ---------------------------------------------------------------------
# Provider-neutral Needs-You sync helper (WO §6.A.4, §12 criterion 7)
# ---------------------------------------------------------------------


def test_needs_you_sync_creates_and_resolves_for_gmail_and_imap():
    needs_you_repo = InMemoryNeedsYouRepository()
    mailbox_repo = InMemoryMailboxSourceRepository()

    gmail_mailbox = mailbox_repo.create_mailbox(display_name="Gmail", email_address="g@example.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    gmail_mailbox = mailbox_repo.begin_microsoft_connect(gmail_mailbox.mailbox_id)
    sync_needs_you_for_connection_state(needs_you_repo, gmail_mailbox, actor_type="SYSTEM", actor_id="test")
    item = needs_you_repo.find_by_dedupe_key(ITEM_TYPE_MAILBOX_AUTH_REQUIRED, gmail_mailbox.mailbox_id)
    assert item is not None
    assert item.status == "OPEN"
    assert item.allowed_action_type == ALLOWED_ACTION_CONNECT_GMAIL_MAILBOX
    assert "Gmail mailbox" in item.question

    connected = mailbox_repo.mark_microsoft_connected(gmail_mailbox.mailbox_id)
    sync_needs_you_for_connection_state(needs_you_repo, connected, actor_type="SYSTEM", actor_id="test")
    resolved_item = needs_you_repo.get_needs_you_item(item.item_id)
    assert resolved_item.status == "RESOLVED"

    imap_mailbox = mailbox_repo.create_mailbox(display_name="IMAP", email_address="i@example.com", provider_kind=PROVIDER_IMAP)
    imap_mailbox = mailbox_repo.mark_microsoft_auth_required(imap_mailbox.mailbox_id, error_detail="bad creds")
    sync_needs_you_for_connection_state(needs_you_repo, imap_mailbox, actor_type="SYSTEM", actor_id="test")
    imap_item = needs_you_repo.find_by_dedupe_key(ITEM_TYPE_MAILBOX_AUTH_REQUIRED, imap_mailbox.mailbox_id)
    assert imap_item is not None
    assert imap_item.allowed_action_type == ALLOWED_ACTION_RECONNECT_IMAP_MAILBOX


def test_needs_you_sync_never_raises_for_disabled_mailbox():
    needs_you_repo = InMemoryNeedsYouRepository()
    mailbox_repo = InMemoryMailboxSourceRepository()
    mailbox = mailbox_repo.create_mailbox(display_name="m", email_address="e@example.com", provider_kind=PROVIDER_GOOGLE_GMAIL)
    mailbox = mailbox_repo.begin_microsoft_connect(mailbox.mailbox_id)
    disabled = mailbox_repo.disable_mailbox(mailbox.mailbox_id)
    sync_needs_you_for_connection_state(needs_you_repo, disabled, actor_type="SYSTEM", actor_id="test")
    assert needs_you_repo.find_by_dedupe_key(ITEM_TYPE_MAILBOX_AUTH_REQUIRED, disabled.mailbox_id) is None
