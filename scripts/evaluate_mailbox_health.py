#!/usr/bin/env python3
"""scripts/evaluate_mailbox_health.py — the bounded, launchd/operator-
invoked mailbox-health evaluator (PID §118, WO-BAGMAN-118-MAILBOX-
HEALTH-ALERTING §6.A.5).

Mirrors `scripts/process_evidence_classification_jobs.py`'s own shape
and JSON-per-run logging convention exactly: argument parsing, a bounded
single-invocation pass, a real JSON summary report. Genuinely different
in one respect: this script constructs its own repositories directly
against `persistence.postgres.session.get_engine()` rather than via
`app.api.composition.get_composition()` — `app/api/composition.py` is
not among the files this delivery's governing Work Order (§6.A)
authorises FORGE to touch, so wiring the two new repositories into it is
out of scope for Stage A; every repository this script needs already
has a standalone, directly-constructible Postgres implementation.

Never fetches email, advances a cursor, or invokes ingestion (PID
§118.G) — read-only against `MailboxSource`/`MailboxSweepRun`/the
existing OAuth-state tables, write-only against the two new tables,
`NeedsYouItem`, and `AuditEvent`.

`AUTH_REQUIRED` and `OAUTH_RECONNECT_INCOMPLETE` are independent,
never-mutually-exclusive conditions (WO-BAGMAN-118 §6.A' Decision D,
Architect amendment 2026-10-21, corrected 2026-10-21/2026-10-22)
------------------------------------------------------------------------
An earlier revision of this evaluator used a single `triggered_class`
variable that could hold only one value per mailbox per cycle — WRONG:
PID §118.D describes both classes independently, and the schema already
supports both being `OPEN` simultaneously for the same mailbox (the
partial unique index is scoped per `(mailbox_id, failure_class)`, never
per-mailbox alone). This evaluator now detects each independently and
may raise/sustain BOTH in the same cycle.

Both classes still key off the same, overloaded
`MailboxSource.connection_state == AUTH_REQUIRED` value — set both by a
genuine confirmed failure (`mark_microsoft_auth_required`/
`mark_microsoft_connection_error`-with-`AUTH_ERROR`, called with a real
`error_detail`) AND by the mere act of starting a fresh connect attempt
(`begin_microsoft_connect`'s own `NOT_CONFIGURED -> AUTH_REQUIRED` step,
before any login outcome is known). Disambiguating these two cases is
why `AUTH_REQUIRED` is evidenced by TWO fully independent, provider-
neutral sources, either of which is sufficient (the partial unique index
already prevents a duplicate row if both independently hold in the same
cycle):

1. **Source 1 — live signal** (`detect_auth_required`,
   `services.mailbox.health.incident`): `connection_state ==
   AUTH_REQUIRED` while `mailbox.status == "ACTIVE"`. Trusted as
   independent proof ONLY while `pending_expired is None` — i.e. NO
   OAuth-pending-state record exists for this mailbox at all (read-only
   against the existing, UNMODIFIED
   `mailbox_microsoft_oauth_states`/`mailbox_gmail_oauth_states` tables
   — IMAP has neither table, so `pending_expired` is always `None`
   there, matching its own real, non-interactive, synchronous
   authentication model). Gated by `is None`, NOT merely `is False`
   (Architect correction, 2026-10-22): once ANY pending-state record
   exists — unexpired (`pending_expired is False`, genuinely mid-flow)
   OR expired (`pending_expired is True`, abandoned) — `connection_state`
   alone can no longer be trusted as independent proof of a confirmed
   failure, since the SAME value is also produced by merely starting
   that very reconnect attempt. An abandoned-and-expired reconnect with
   no independent failure evidence must therefore produce `OAUTH_RECONNECT_INCOMPLETE`
   alone, never a speculative `AUTH_REQUIRED` from this source.
2. **Source 2 — durable signal** (`_has_durable_auth_required_evidence`,
   below; newly added by this correction): a qualifying
   `MailboxSweepRun` row with `error_code ==
   SweepFailureReason.TOKEN_REFRESH_FAILED`, dated at or after the
   mailbox's own last known-good checkpoint
   (`mailbox.last_successful_sweep_at`, or `mailbox.created_at` if the
   mailbox has never completed a successful sweep). Fires independently
   of `pending_expired`/`connection_state` ENTIRELY — present, unexpired,
   expired, or even after a completed reconnect reaches `CONNECTED` — so
   a later reconnect attempt (abandoned, still in flight, or already
   completed) never silences already-durably-evidenced proof of a real
   prior failure. This is intentional, not a defect: PID §118.H's own
   recovery-proof principle (`connection_state == CONNECTED` alone is
   never proof of recovery) applied consistently to incident
   creation/reaffirmation too — a mailbox is not treated as healthy until
   a real ingestion succeeds, not merely until a login screen is
   completed. A consciously accepted tradeoff: an operator who has just
   completed a visibly successful reconnect may still see this incident
   remain `OPEN` until the next sweep genuinely succeeds.

`OAUTH_RECONNECT_INCOMPLETE` (`detect_oauth_reconnect_incomplete`) is
unchanged: an unconsumed, expired pending OAuth state — only its
STRENGTH as a silencer of a separately-evidenced `AUTH_REQUIRED`
incident changed (it no longer can silence one at all).

No fabrication of historical error evidence, and no provider-
authentication-code rewrite: this reads existing, already-recorded
`mailbox_sweep_runs`/`MailboxSource.connection_state` history exactly as
it already exists; it does not backfill, synthesize, or alter any
historical row, and does not touch `services/mailbox/gmail/`,
`services/mailbox/microsoft/`, `services/mailbox/imap/`, or
`services/mailbox/sweep.py` in any way.

Legacy `MAILBOX_AUTH_REQUIRED` vs. episode-scoped
`MAILBOX_HEALTH_INCIDENT` coexistence (WO-BAGMAN-118 §6.A' Decision C)
------------------------------------------------------------------------
The pre-existing, legacy `MAILBOX_AUTH_REQUIRED` Needs-You item
(mailbox-scoped, one per mailbox, no episode concept, already wired for
all three providers via `services.mailbox.health.needs_you_sync` per
§6.A item 4) and this evaluator's own new, episode-scoped
`MAILBOX_HEALTH_INCIDENT` item (one per incident/episode, per §6.A item
2) are two SEPARATE, independently-lifecycled things — both may
legitimately be `OPEN` simultaneously for the same mailbox. Resolving or
auto-resolving the legacy item (e.g. via `connection_state` reaching
`CONNECTED`) is NOT proof that a coexisting `MAILBOX_HEALTH_INCIDENT` has
recovered — only this module's own `detect_recovery` (PID §118.H's own
per-class proof) ever closes an incident, and only this evaluator's own
canonical `mailbox_health_notifications` outbox ever sends an external
(Pushover) notification — the legacy item type has, and gains, no
notification-outbox interaction of any kind. Consolidated single-GUI-
surface presentation of both item types is explicitly deferred, not
addressed here.

Usage
-----
    python3 scripts/evaluate_mailbox_health.py \\
        --runtime-dir /opt/bagman/runtime/mailbox-health \\
        --notification-limit 50 --claim-timeout-seconds 900 \\
        --worker-id operator-manual-run-1

`--runtime-dir` may also be supplied via
`BAGMAN_MAILBOX_HEALTH_RUNTIME_DIR`. This process expects to run with
`BAGMAN_RUNTIME_ENV`/DB env vars already set, typically `docker exec
bagman-api python3 scripts/evaluate_mailbox_health.py ...` — exactly
like every other operator script in this directory. Never installed as
a live periodic job by this delivery — `deployment/launchd/com.bagman.mailbox-health-evaluator.plist`
exists in the repository only, inert, per Stage D's own separate gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import identity  # noqa: E402
from core.timestamps import to_contract_string, utc_now  # noqa: E402
from persistence.postgres.audit_repository import PostgresAuditRepository  # noqa: E402
from persistence.postgres.mailbox_gmail_models import MailboxGmailOAuthStateRow  # noqa: E402
from persistence.postgres.mailbox_health_repository import (  # noqa: E402
    PostgresMailboxHealthIncidentRepository,
    PostgresMailboxHealthNotificationRepository,
)
from persistence.postgres.mailbox_microsoft_models import MailboxMicrosoftOAuthStateRow  # noqa: E402
from persistence.postgres.mailbox_microsoft_repository import PostgresMailboxSweepRunRepository  # noqa: E402
from persistence.postgres.mailbox_repository import PostgresMailboxSourceRepository  # noqa: E402
from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository  # noqa: E402
from persistence.postgres.session import get_engine, session_scope  # noqa: E402
from services.mailbox.health.incident import (  # noqa: E402
    ALLOWED_ACTION_ACKNOWLEDGE_MAILBOX_HEALTH_INCIDENT,
    FAILURE_CLASS_AUTH_REQUIRED,
    FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE,
    FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR,
    ITEM_TYPE_MAILBOX_HEALTH_INCIDENT,
    PERSISTENT_PROVIDER_ERROR_CONSECUTIVE_THRESHOLD,
    detect_auth_required,
    detect_oauth_reconnect_incomplete,
    detect_persistent_provider_error,
    detect_recovery,
)
from services.mailbox.health.notification import (  # noqa: E402
    PURPOSE_INITIAL,
    PURPOSE_RECOVERY,
    PURPOSE_REMINDER_24H,
    STATE_ACCEPTED,
    STATE_RETRYABLE_FAILURE,
    STATE_TERMINAL_FAILURE,
    compute_next_attempt_at,
    reminder_is_due,
)
from services.mailbox.sweep_run import SweepFailureReason  # noqa: E402
from services.notifications.pushover import PushoverClient, PushoverOutcomeStatus, read_pushover_credentials

_ACTOR_TYPE = "SYSTEM"
_EVALUATOR_ACTOR_ID = "bagman-mailbox-health-evaluator"
_INCIDENT_FAILURE_CLASSES = (
    FAILURE_CLASS_AUTH_REQUIRED,
    FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR,
    FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE,
)

DEFAULT_CLAIM_TIMEOUT_SECONDS = 900.0  # 15 minutes
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_NOTIFICATION_LIMIT = 50
_MAX_NOTIFICATION_LIMIT = 200

_PUSHOVER_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{1,64}$")


def _validate_pushover_request_id(value: object) -> Optional[str]:
    """Pushover's own `request` field (`PushoverSendResult.provider_request_id`)
    is a bounded, hex-token-shaped identifier when the provider genuinely
    returns one — never trusted as arbitrary text (Architect correction,
    2026-10-23, WO-BAGMAN-118 §12 criterion 16). Anything not matching this
    known-safe shape is dropped, never persisted."""
    if isinstance(value, str) and _PUSHOVER_REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def _pending_oauth_state_status(session, mailbox_id: str, *, now: datetime) -> Optional[bool]:
    """`True` if the mailbox's own most recent unconsumed OAuth state has
    expired, `False` if one exists and has not yet expired, `None` if no
    unconsumed state exists at all (including every IMAP mailbox, which
    has no OAuth-state table). Read-only against the existing,
    UNMODIFIED `mailbox_microsoft_oauth_states`/`mailbox_gmail_oauth_states`
    tables — see module docstring."""
    for row_cls in (MailboxMicrosoftOAuthStateRow, MailboxGmailOAuthStateRow):
        row = (
            session.query(row_cls)
            .filter(row_cls.mailbox_id == mailbox_id, row_cls.consumed_at.is_(None))
            .order_by(row_cls.created_at.desc())
            .first()
        )
        if row is not None:
            return row.expires_at < now
    return None


def _has_durable_auth_required_evidence(sweep_run_repository, mailbox) -> bool:
    """`AUTH_REQUIRED` source 2 (durable signal — WO-BAGMAN-118 §6.A'
    Decision D, module docstring's "Source 2" section): `True` if a
    qualifying `MailboxSweepRun` row (`error_code ==
    SweepFailureReason.TOKEN_REFRESH_FAILED`) exists at or after
    ``mailbox``'s own last known-good checkpoint.

    The checkpoint is ``mailbox.last_successful_sweep_at`` — the one
    field already in the existing schema that unambiguously marks the
    mailbox's last known-good point (stamped only by a genuine
    successful sweep completion) — or, if the mailbox has never
    completed a successful sweep (``last_successful_sweep_at is None``),
    ``mailbox.created_at``. A qualifying row dated BEFORE this
    checkpoint belongs to a prior, already-recovered episode and must
    not be treated as evidence of a new, currently-unresolved problem
    (the "stale-prior-episode" regression test this bound exists for).

    Deliberately NOT a `limit=`-bounded `list_runs(...)` call (the
    `PERSISTENT_PROVIDER_ERROR` trigger's own convention, reused
    verbatim, does NOT satisfy this requirement — Independent Audit,
    2026-10-21) — the lookback must scan sweep-run history all the way
    back to the checkpoint, however far that is, never merely the most
    recent N rows. `MailboxSweepRunRepository.list_runs` already
    supports an unbounded scan via `limit=None`.

    Entirely independent of `connection_state`/any OAuth pending-state
    status — callers must NOT gate a call to this function on
    `pending_expired` the way source 1 (`detect_auth_required`) is
    gated; that independence is the whole point of this source (module
    docstring's own "Source 2" section).
    """
    checkpoint = mailbox.last_successful_sweep_at if mailbox.last_successful_sweep_at is not None else mailbox.created_at
    all_runs = sweep_run_repository.list_runs(mailbox_id=mailbox.mailbox_id, limit=None)
    return any(
        run.error_code == SweepFailureReason.TOKEN_REFRESH_FAILED
        and run.completed_at is not None
        and run.completed_at >= checkpoint
        for run in all_runs
    )


def _evaluate_mailbox(
    mailbox,
    *,
    sweep_run_repository,
    incident_repository,
    notification_repository,
    needs_you_repository,
    engine,
    now: datetime,
    audit_repository,
) -> dict:
    report = {"mailbox_id": mailbox.mailbox_id, "incidents_created": 0, "incidents_resolved": 0, "incidents_reaffirmed": 0}
    if mailbox.status != "ACTIVE":
        return report

    recent_runs = sweep_run_repository.list_runs(
        mailbox_id=mailbox.mailbox_id, limit=PERSISTENT_PROVIDER_ERROR_CONSECUTIVE_THRESHOLD
    )

    # -- Recovery check (PID §118.H) for every currently-OPEN incident
    # this mailbox has, of any class --
    for failure_class in _INCIDENT_FAILURE_CLASSES:
        existing = incident_repository.get_open_incident(mailbox_id=mailbox.mailbox_id, failure_class=failure_class)
        if existing is None:
            continue
        recovery_evidence = detect_recovery(existing, mailbox, recent_runs)
        if recovery_evidence is None:
            continue

        with session_scope(engine) as session:
            resolved = incident_repository.resolve_incident(
                existing.incident_id, recovery_evidence=recovery_evidence, now=now, session=session
            )
            needs_you_item = needs_you_repository.find_by_dedupe_key(
                ITEM_TYPE_MAILBOX_HEALTH_INCIDENT, resolved.incident_id
            )
            if needs_you_item is not None and needs_you_item.status == "OPEN":
                needs_you_repository.resolve_needs_you_item(
                    needs_you_item.item_id,
                    new_status="RESOLVED",
                    resolution={"incident_id": resolved.incident_id, "recovery_evidence": recovery_evidence},
                    actor_type=_ACTOR_TYPE,
                    actor_id=_EVALUATOR_ACTOR_ID,
                    session=session,
                )
            notification_repository.create_notification(
                incident_id=resolved.incident_id, purpose=PURPOSE_RECOVERY, max_attempts=DEFAULT_MAX_ATTEMPTS,
                now=now, session=session,
            )
        report["incidents_resolved"] += 1
        audit_repository.record_audit_event(
            event_type="MAILBOX_HEALTH_INCIDENT_RESOLVED",
            actor_type=_ACTOR_TYPE,
            actor_id=_EVALUATOR_ACTOR_ID,
            subject_type="MailboxHealthIncident",
            subject_id=resolved.incident_id,
            correlation_id=resolved.incident_id,
            causation_id=None,
            payload={"mailbox_id": mailbox.mailbox_id, "failure_class": failure_class},
        )

    # -- Trigger detection for a new/ongoing failure (PID §118.C/D,
    # corrected per WO-BAGMAN-118 §6.A' Decision D — AUTH_REQUIRED and
    # OAUTH_RECONNECT_INCOMPLETE are independently observable, never
    # mutually exclusive; see module docstring for the full two-source
    # AUTH_REQUIRED design) --
    triggered_classes: list[str] = []

    # Source 1 (live signal) and OAUTH_RECONNECT_INCOMPLETE both depend
    # on whether a pending OAuth-state record exists for this mailbox
    # and, if so, whether it has expired — computed once, only when
    # `connection_state == AUTH_REQUIRED` (the only case either check
    # can ever fire), exactly as before this correction.
    if detect_auth_required(mailbox):
        with session_scope(engine) as session:
            pending_expired = _pending_oauth_state_status(session, mailbox.mailbox_id, now=now)

        # Source 1 — gated by `pending_expired is None`, NOT merely
        # `pending_expired is False` (Architect correction, 2026-10-22):
        # see module docstring's own "Source 1" section for why
        # `connection_state == AUTH_REQUIRED` cannot be trusted as
        # independent proof while ANY pending-state record exists.
        if pending_expired is None:
            triggered_classes.append(FAILURE_CLASS_AUTH_REQUIRED)

        if pending_expired is True and detect_oauth_reconnect_incomplete(mailbox, pending_state_expired=True):
            triggered_classes.append(FAILURE_CLASS_OAUTH_RECONNECT_INCOMPLETE)
        # pending_expired is False -> still inside the connect-flow's own
        # TTL window; source 1 and OAUTH_RECONNECT_INCOMPLETE both
        # correctly withhold this cycle (source 2, below, is NOT gated by
        # this at all).

    # Source 2 (durable signal) — entirely independent of
    # connection_state/pending_expired (module docstring's own "Source
    # 2" section). Evaluated regardless of whether source 1 already
    # fired above — the partial unique index on `(mailbox_id,
    # failure_class)` already prevents a duplicate AUTH_REQUIRED row if
    # both sources independently hold in the same cycle.
    if FAILURE_CLASS_AUTH_REQUIRED not in triggered_classes and _has_durable_auth_required_evidence(
        sweep_run_repository, mailbox
    ):
        triggered_classes.append(FAILURE_CLASS_AUTH_REQUIRED)

    if not triggered_classes and detect_persistent_provider_error(recent_runs, mailbox_status=mailbox.status):
        triggered_classes.append(FAILURE_CLASS_PERSISTENT_PROVIDER_ERROR)

    for triggered_class in triggered_classes:
        with session_scope(engine) as session:
            creation = incident_repository.create_incident(
                mailbox_id=mailbox.mailbox_id,
                failure_class=triggered_class,
                # `mailbox.last_error_code` is closed-vocabulary and safe to
                # keep verbatim: it is always one of `None`, the fixed
                # literal `"AUTH_REQUIRED"` (set only by
                # `mark_microsoft_auth_required`), or a closed
                # `SweepFailureReason`/provider-status enum value (set only
                # by `mark_microsoft_connection_error`, always called with an
                # enum/closed-constant member — confirmed at every real call
                # site: `services/mailbox/sweep.py:1018,1130` (both pass
                # `SweepFailureReason.PERMISSION_ERROR`) and
                # `app/api/routers/mailboxes_imap.py:125-126` (passes
                # `ImapOutcomeStatus(...).value`, itself a closed `str`
                # enum). `mailbox
                # .last_error_detail`, by contrast, is always free text (raw
                # exception messages, raw provider response bodies) and must
                # never be persisted verbatim — replaced here with a bounded
                # presence boolean only (Architect correction, 2026-10-23,
                # WO-BAGMAN-118 §12 criterion 16): still gives an operator a
                # useful signal ("more detail existed") without ever storing
                # its content.
                metadata={
                    "last_error_code": mailbox.last_error_code,
                    "last_error_detail_present": mailbox.last_error_detail is not None,
                },
                now=now,
                session=session,
            )
            if creation.created:
                needs_you_repository.create_needs_you_item(
                    item_type=ITEM_TYPE_MAILBOX_HEALTH_INCIDENT,
                    domain="MAILBOX",
                    source_object_reference=creation.incident.incident_id,
                    question=(
                        f"Mailbox health incident ({triggered_class}) — "
                        f"{mailbox.display_name} ({mailbox.email_address})"
                    ),
                    allowed_action_type=ALLOWED_ACTION_ACKNOWLEDGE_MAILBOX_HEALTH_INCIDENT,
                    priority="HIGH",
                    metadata={
                        "mailbox_id": mailbox.mailbox_id,
                        "incident_id": creation.incident.incident_id,
                        "failure_class": triggered_class,
                    },
                    session=session,
                )
                notification_repository.create_notification(
                    incident_id=creation.incident.incident_id, purpose=PURPOSE_INITIAL,
                    max_attempts=DEFAULT_MAX_ATTEMPTS, now=now, session=session,
                )
                report["incidents_created"] += 1
            else:
                incident_repository.touch_observed(creation.incident.incident_id, observed_at=now, session=session)
                report["incidents_reaffirmed"] += 1
        if creation.created:
            audit_repository.record_audit_event(
                event_type="MAILBOX_HEALTH_INCIDENT_CREATED",
                actor_type=_ACTOR_TYPE,
                actor_id=_EVALUATOR_ACTOR_ID,
                subject_type="MailboxHealthIncident",
                subject_id=creation.incident.incident_id,
                correlation_id=creation.incident.incident_id,
                causation_id=None,
                payload={"mailbox_id": mailbox.mailbox_id, "failure_class": triggered_class},
            )

    return report


def _create_due_reminders(
    *, incident_repository, notification_repository, now: datetime
) -> int:
    """PID §118.E.2's own `REMINDER_24H` gating invariant, applied to
    every currently-OPEN incident. Idempotent via the real
    `(incident_id, purpose)` unique constraint — a reminder already
    created for an incident is never created twice (`created=False` is
    a correct, silent no-op here, never an error)."""
    created = 0
    for incident in incident_repository.list_open_incidents():
        latest_completed = notification_repository.get_latest_completed(incident.incident_id)
        if not reminder_is_due(latest_completed, now=now):
            continue
        result = notification_repository.create_notification(
            incident_id=incident.incident_id, purpose=PURPOSE_REMINDER_24H, max_attempts=DEFAULT_MAX_ATTEMPTS, now=now
        )
        if result.created:
            created += 1
    return created


def _process_notifications(
    *,
    incident_repository,
    notification_repository,
    audit_repository,
    pushover_client,
    credentials,
    claim_timeout_seconds: float,
    limit: int,
    worker_id: str,
    now: datetime,
) -> list[dict]:
    claimed = notification_repository.claim_due_or_expired(
        limit=limit, claimed_by=worker_id, claim_timeout_seconds=claim_timeout_seconds, now=now
    )
    records: list[dict] = []

    for notification in claimed:
        try:
            incident = incident_repository.get_incident(notification.incident_id)
        except Exception as exc:  # noqa: BLE001 - defensive only; FK makes this structurally near-impossible
            records.append({"notification_id": notification.notification_id, "outcome": "INCIDENT_NOT_FOUND", "error": str(exc)})
            continue

        # Pre-dispatch suppression (WO §6.A.1.C) — RECOVERY is never
        # subject to this check (PID §118.H/I: it exists specifically to
        # announce the resolution and must always be sent once created).
        if notification.purpose != PURPOSE_RECOVERY and incident.status == "RESOLVED":
            cancelled = notification_repository.cancel_claimed(
                notification.notification_id,
                claim_generation=notification.claim_generation,
                cancellation_reason={
                    "reason": "incident resolved before dispatch",
                    "incident_id": incident.incident_id,
                    "resolved_at": to_contract_string(incident.resolved_at) if incident.resolved_at is not None else None,
                },
                now=now,
            )
            if cancelled is not None:
                audit_repository.record_audit_event(
                    event_type="MAILBOX_HEALTH_NOTIFICATION_CANCELLED",
                    actor_type=_ACTOR_TYPE,
                    actor_id=worker_id,
                    subject_type="MailboxHealthNotification",
                    subject_id=notification.notification_id,
                    correlation_id=incident.incident_id,
                    causation_id=None,
                    payload={"incident_id": incident.incident_id, "purpose": notification.purpose},
                )
                records.append({"notification_id": notification.notification_id, "outcome": "CANCELLED"})
            else:
                records.append({"notification_id": notification.notification_id, "outcome": "SUPERSEDED_BY_LATER_CLAIM"})
            continue

        audit_repository.record_audit_event(
            event_type="MAILBOX_HEALTH_NOTIFICATION_ATTEMPTED",
            actor_type=_ACTOR_TYPE,
            actor_id=worker_id,
            subject_type="MailboxHealthNotification",
            subject_id=notification.notification_id,
            correlation_id=incident.incident_id,
            causation_id=None,
            payload={"incident_id": incident.incident_id, "purpose": notification.purpose, "attempt_count": notification.attempt_count},
        )

        if credentials is None:
            result_status = PushoverOutcomeStatus.CONFIG_ERROR
            result_detail = "Pushover is not configured yet (BAGMAN_PUSHOVER_API_TOKEN_FILE/BAGMAN_PUSHOVER_USER_KEY_FILE)"
            http_status = None
            provider_request_id = None
        else:
            title = f"BAGMAN mailbox alert — {notification.purpose}"
            message = (
                f"Mailbox: {incident.mailbox_id}\n"
                f"Failure class: {incident.failure_class}\n"
                f"First observed: {to_contract_string(incident.first_observed_at)}"
            )
            result = pushover_client.send(credentials=credentials, title=title, message=message)
            result_status = result.status
            result_detail = result.error_detail
            http_status = result.http_status
            provider_request_id = result.provider_request_id

        # `result_detail`/`PushoverSendResult.error_detail` is always free
        # text — the raw HTTP error body, a raw transport-exception message,
        # or the provider's own `errors[]` array joined into a string
        # (`services/notifications/pushover.py`'s own `send()` method) —
        # potentially adversary- or upstream-provider-controlled. It is
        # dropped entirely here, never persisted (Architect correction,
        # 2026-10-23, WO-BAGMAN-118 §12 criterion 16): `PushoverOutcomeStatus`
        # already has 8 closed, meaningful enum values that fully capture
        # the operationally-useful classification without any free text,
        # and `error_detail` is always present whenever `result_status !=
        # OK`, so a redundant presence flag would add nothing. `http_status`
        # is already a safe, narrow integer from the HTTP layer (defensively
        # re-checked here); `provider_request_id` is validated against a
        # known-safe shape rather than trusted blindly.
        provider_response_metadata = {
            "provider_status": result_status.value if hasattr(result_status, "value") else str(result_status),
            "http_status": http_status if isinstance(http_status, int) else None,
            "provider_request_id": _validate_pushover_request_id(provider_request_id),
        }

        if result_status == PushoverOutcomeStatus.OK:
            updated = notification_repository.complete_claim(
                notification.notification_id, claim_generation=notification.claim_generation,
                outcome=STATE_ACCEPTED, provider_response_metadata=provider_response_metadata, now=now,
            )
            event_type = "MAILBOX_HEALTH_NOTIFICATION_ACCEPTED"
            outcome_label = "ACCEPTED"
        else:
            # Exhaustion-aware completion (WO §6.A.1.B): this claim's
            # own attempt_count already reflects THIS attempt — if it has
            # reached max_attempts, this is the FINAL permitted attempt,
            # and completion-time failure handling transitions directly
            # to TERMINAL_FAILURE (never a 4th claim being refused later).
            if notification.attempt_count >= notification.max_attempts:
                updated = notification_repository.complete_claim(
                    notification.notification_id, claim_generation=notification.claim_generation,
                    outcome=STATE_TERMINAL_FAILURE, provider_response_metadata=provider_response_metadata, now=now,
                )
                event_type = "MAILBOX_HEALTH_NOTIFICATION_FAILED"
                outcome_label = "TERMINAL_FAILURE"
            else:
                next_attempt_at = compute_next_attempt_at(notification.attempt_count, now=now)
                updated = notification_repository.complete_claim(
                    notification.notification_id, claim_generation=notification.claim_generation,
                    outcome=STATE_RETRYABLE_FAILURE, next_attempt_at=next_attempt_at,
                    provider_response_metadata=provider_response_metadata, now=now,
                )
                event_type = "MAILBOX_HEALTH_NOTIFICATION_FAILED"
                outcome_label = "RETRYABLE_FAILURE"

        if updated is not None:
            audit_repository.record_audit_event(
                event_type=event_type,
                actor_type=_ACTOR_TYPE,
                actor_id=worker_id,
                subject_type="MailboxHealthNotification",
                subject_id=notification.notification_id,
                correlation_id=incident.incident_id,
                causation_id=None,
                payload={"incident_id": incident.incident_id, "purpose": notification.purpose, "outcome": outcome_label},
            )
            records.append({"notification_id": notification.notification_id, "outcome": outcome_label})
        else:
            # A stale/superseded claim's completion write affected zero
            # rows — a correct, silent no-op (WO §6.A.1.B); this worker
            # must not treat this as an error requiring its own further
            # action.
            records.append({"notification_id": notification.notification_id, "outcome": "SUPERSEDED_BY_LATER_CLAIM"})

    return records


def run_evaluation(
    *,
    engine,
    mailbox_source_repository,
    sweep_run_repository,
    incident_repository,
    notification_repository,
    needs_you_repository,
    audit_repository,
    pushover_client,
    worker_id: str,
    notification_limit: int,
    claim_timeout_seconds: float,
    now: Optional[datetime] = None,
    credentials=None,
) -> dict:
    """`credentials` defaults to `None`, meaning "resolve it via
    :func:`services.notifications.pushover.read_pushover_credentials`
    right now" — the real, production behaviour. Dependency-injectable
    (mirrors `scripts/process_evidence_classification_jobs.py`'s own
    `_run_worker` extraction) so tests can supply a real
    `PushoverCredentials` instance without needing real secret files on
    disk; `main()` itself never overrides this default."""
    resolved_now = now if now is not None else utc_now()
    if credentials is None:
        credentials = read_pushover_credentials()

    mailbox_reports = [
        _evaluate_mailbox(
            mailbox,
            sweep_run_repository=sweep_run_repository,
            incident_repository=incident_repository,
            notification_repository=notification_repository,
            needs_you_repository=needs_you_repository,
            engine=engine,
            now=resolved_now,
            audit_repository=audit_repository,
        )
        for mailbox in mailbox_source_repository.list_mailboxes()
    ]

    reminders_created = _create_due_reminders(
        incident_repository=incident_repository, notification_repository=notification_repository, now=resolved_now
    )

    notification_records = _process_notifications(
        incident_repository=incident_repository,
        notification_repository=notification_repository,
        audit_repository=audit_repository,
        pushover_client=pushover_client,
        credentials=credentials,
        claim_timeout_seconds=claim_timeout_seconds,
        limit=notification_limit,
        worker_id=worker_id,
        now=resolved_now,
    )

    return {
        "mailbox_reports": mailbox_reports,
        "incidents_created": sum(r["incidents_created"] for r in mailbox_reports),
        "incidents_resolved": sum(r["incidents_resolved"] for r in mailbox_reports),
        "reminders_created": reminders_created,
        "notifications_claimed": len(notification_records),
        "notification_records": notification_records,
        "pushover_configured": credentials is not None,
    }


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="directory to hold this run's own JSON report (required here, or via BAGMAN_MAILBOX_HEALTH_RUNTIME_DIR)",
    )
    parser.add_argument(
        "--notification-limit", type=int, default=DEFAULT_NOTIFICATION_LIMIT,
        help=f"bounded claim count for the notification outbox (default {DEFAULT_NOTIFICATION_LIMIT})",
    )
    parser.add_argument(
        "--claim-timeout-seconds", type=float, default=DEFAULT_CLAIM_TIMEOUT_SECONDS,
        help=f"an IN_PROGRESS notification claim older than this is reclaimable (default {DEFAULT_CLAIM_TIMEOUT_SECONDS})",
    )
    parser.add_argument("--worker-id", default=None, help="worker identity stamped on claimed notifications; defaults to a generated id")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if not (1 <= args.notification_limit <= _MAX_NOTIFICATION_LIMIT):
        parser.error(f"--notification-limit must be between 1 and {_MAX_NOTIFICATION_LIMIT} (got {args.notification_limit})")
    if args.claim_timeout_seconds <= 0:
        parser.error(f"--claim-timeout-seconds must be > 0 (got {args.claim_timeout_seconds})")

    runtime_dir = args.runtime_dir or os.environ.get("BAGMAN_MAILBOX_HEALTH_RUNTIME_DIR")
    if not runtime_dir:
        parser.error("--runtime-dir is required (or set BAGMAN_MAILBOX_HEALTH_RUNTIME_DIR) — no default path is ever assumed")
    args.runtime_dir = runtime_dir

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    mailbox_source_repository = PostgresMailboxSourceRepository(engine)
    sweep_run_repository = PostgresMailboxSweepRunRepository(engine)
    incident_repository = PostgresMailboxHealthIncidentRepository(engine)
    notification_repository = PostgresMailboxHealthNotificationRepository(engine)
    needs_you_repository = PostgresNeedsYouRepository(engine)
    audit_repository = PostgresAuditRepository(engine)
    pushover_client = PushoverClient()

    run_id = identity.generate_id()
    started_at = utc_now()
    worker_id = args.worker_id or f"mailbox-health-evaluator-{uuid.uuid4()}"

    result = run_evaluation(
        engine=engine,
        mailbox_source_repository=mailbox_source_repository,
        sweep_run_repository=sweep_run_repository,
        incident_repository=incident_repository,
        notification_repository=notification_repository,
        needs_you_repository=needs_you_repository,
        audit_repository=audit_repository,
        pushover_client=pushover_client,
        worker_id=worker_id,
        notification_limit=args.notification_limit,
        claim_timeout_seconds=args.claim_timeout_seconds,
        now=started_at,
    )

    report = {
        "run_id": run_id,
        "worker_id": worker_id,
        **result,
        "started_at": to_contract_string(started_at),
        "completed_at": to_contract_string(utc_now()),
    }

    report_path = runtime_dir / f"mailbox-health-evaluate-{run_id}-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"[evaluate_mailbox_health] report written to {report_path}", file=sys.stderr)

    has_failure = any(
        r.get("outcome") in ("TERMINAL_FAILURE",) for r in result.get("notification_records", [])
    )
    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
