"""``run_sweep`` — the provider-neutral sweep orchestration engine (CD-6
Slice 4).

Provider-neutral by construction
------------------------------------
This module never imports anything from ``services/mailbox/microsoft/``
except the ``MicrosoftGraphMailboxAdapter`` TYPE it is handed (and even
that only for a type hint — every call site uses the adapter through
its own three narrow methods:
:meth:`~services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter.fetch_folder_delta`/
:meth:`~....fetch_message_content`/:meth:`~....report_connection_error`).
A future IMAP/Gmail adapter implementing the same narrow surface could
be swapped in without touching a single line here — this is the
"provider-neutral sweep orchestration" half of the architect's own
required separation (see that adapter module's own docstring for the
other half).

Hard scope boundary — read before extending
--------------------------------------------------
This module (and everything it calls) NEVER classifies a message,
proposes an account/entity, or makes any accounting decision. A
``MailboxMessage``'s own fields are the ONLY thing this module ever
writes about a message's CONTENT — subject/sender/received time/folder/
ingestion outcome. No "relevant/irrelevant", no "invoice/not-invoice",
no "What/Why" field exists anywhere in this call chain, and none may be
added here — that is Slice 5's entire scope. If a future change to this
file introduces anything resembling classification, it does not belong
here.

Idempotency & the cursor-advance rule — read before changing anything
about failure handling
--------------------------------------------------------------------------
Idempotency is enforced by ``services.mailbox.message
.MailboxMessageRepository.record_observation``'s own (mailbox_id,
immutable_provider_message_id) resolve-or-create semantics and by
``services.evidence.evidence.EvidenceRepository.register_evidence``'s
own ``external_reference`` replay detection — NEVER by the cursor
itself (architect spec, verbatim: "idempotency, not the cursor,
prevents duplicate evidence — correctness over minimizing re-reads"). A
folder's durable ``delta_link`` cursor is therefore only ever advanced
via :meth:`~services.mailbox.cursor.MailboxFolderCursorRepository
.advance_cursor` AFTER that folder's ENTIRE delta round (every page,
every message) has been safely, durably handled — see
:data:`_DURABLY_HANDLED_OUTCOMES` below for the precise, documented
distinction between a durably-handled terminal outcome (quarantine,
oversize, vanished — all of which advance the cursor past that
message) and a genuinely transient failure (network/5xx/429-exhausted/
malformed-response — none of which advance the folder's cursor at all
this round, so the exact same message is safely re-seen and re-
attempted on the NEXT sweep).

Concurrency — one sweep per mailbox at a time
--------------------------------------------------
:func:`run_sweep` acquires ``services.mailbox.lock.MailboxSweepLock``
for the WHOLE duration of the sweep (see that module's own docstring
for why a dedicated lease, not a database row lock). A second
concurrent sweep attempt for the SAME mailbox raises
:class:`services.mailbox.lock.MailboxSweepLockError` immediately,
before any `MailboxSweepRun` row is even created — there is nothing
for THIS declined attempt to durably record; the mailbox is simply
busy (the caller — the HTTP router — maps this to an honest 409).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from core.errors import ConflictError
from core.timestamps import utc_now
from services.evidence.intake.scanner import EvidenceSafetyScanner
from services.mailbox.cursor import MailboxFolderCursorRepository
from services.mailbox.lock import MailboxSweepLock
from services.mailbox.mailbox import (
    CONNECTION_STATE_CONNECTED,
    MailboxSource,
    MailboxSourceRepository,
)
from services.mailbox.message import (
    FOLDER_INBOX,
    FOLDER_JUNK,
    INGESTION_STATUS_FAILED,
    INGESTION_STATUS_INGESTED,
    INGESTION_STATUS_QUARANTINED,
    INGESTION_STATUS_VANISHED,
    MailboxMessageRepository,
)
from services.mailbox.microsoft.evidence_ingest import (
    INGEST_STATUS_FAILED,
    INGEST_STATUS_INGESTED,
    INGEST_STATUS_QUARANTINED,
    ingest_email_evidence,
)
from services.mailbox.microsoft.graph_client import GraphOutcomeStatus
from services.mailbox.sweep_run import MailboxSweepRunRepository, SweepFailureReason

#: Architect spec — a bounded, explicit first-sync boundary (never a
#: silent full-history ingest).
DEFAULT_BOOTSTRAP_DAYS = 7

#: The two, and only two, folders this slice ever sweeps (architect
#: spec — never Sent Items/Deleted Items/Archive/custom folders).
SWEPT_FOLDERS = (FOLDER_INBOX, FOLDER_JUNK)

#: Bounded worst-case backoff for one rate-limited delta page — mirrors
#: `services.xero.sync._MAX_RATE_LIMIT_BACKOFF_SECONDS`'s own reasoning.
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 30.0

#: A message outcome this durably records (a genuine, non-lost terminal
#: record) — counts as PROCESSED for cursor-advance purposes even
#: though it is not a plain success (architect spec's own explicit,
#: subtle distinction — see module docstring).
_DURABLY_HANDLED_INGEST_OUTCOMES = frozenset(
    {INGEST_STATUS_QUARANTINED, INGEST_STATUS_FAILED}  # FAILED here == oversize, a governed terminal outcome
)


class _AdapterProtocol(Protocol):
    def fetch_folder_delta(
        self,
        *,
        mailbox_id: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ): ...

    def fetch_message_content(self, *, mailbox_id: str, immutable_message_id: str): ...

    def report_connection_error(self, mailbox_id: str, *, error_code: str, error_detail: str) -> None: ...


class _EvidenceAPIProtocol(Protocol):
    def register_evidence(self, **kwargs): ...

    def record_provenance(self, **kwargs): ...

    def record_audit_event(self, **kwargs): ...


class _ObjectStoreProtocol(Protocol):
    def put(self, object_id, content_hash, data): ...

    def put_prefixed(self, prefix, object_id, content_hash, data): ...


class _SweepStopped(Exception):
    """Internal-only signal: a whole-sweep-stopping condition (a
    reconnect-required auth failure, or BAGMAN's own configuration
    being broken) was hit — raised, caught once at the top of
    :func:`run_sweep`, never escapes this module."""

    def __init__(self, *, error_code: str, error_detail: str) -> None:
        super().__init__(error_detail)
        self.error_code = error_code
        self.error_detail = error_detail


def run_sweep(
    *,
    mailbox: MailboxSource,
    mailbox_source_id: str,
    trigger: str,
    adapter: _AdapterProtocol,
    message_repository: MailboxMessageRepository,
    sweep_run_repository: MailboxSweepRunRepository,
    cursor_repository: MailboxFolderCursorRepository,
    sweep_lock: MailboxSweepLock,
    mailbox_repository: MailboxSourceRepository,
    api: _EvidenceAPIProtocol,
    object_store: _ObjectStoreProtocol,
    scanner: EvidenceSafetyScanner,
    actor_type: str,
    actor_id: str,
    bootstrap_days: int = DEFAULT_BOOTSTRAP_DAYS,
    now: Optional[datetime] = None,
    sleep_fn=time.sleep,
):
    """Run exactly one sweep attempt for `mailbox`. Always returns a
    terminal `MailboxSweepRun` — never raises for an ordinary provider/
    auth/rate-limit/malformed-response failure (all of those are
    PARTIAL/FAILED runs). Raises:

    * `core.errors.ConflictError` — `mailbox` is not
      ACTIVE+CONNECTED (a defensive backstop; the caller/GUI is
      expected to never offer 'Sweep now' otherwise — mirrors
      `services.xero.sync.run_sync`'s identical precondition-raise
      shape).
    * `services.mailbox.lock.MailboxSweepLockError` — a concurrent
      sweep of the SAME mailbox is already in progress (see module
      docstring's "Concurrency" section).
    """
    if mailbox.status != "ACTIVE" or mailbox.connection_state != CONNECTION_STATE_CONNECTED:
        raise ConflictError(
            f"mailbox '{mailbox.mailbox_id}' is not ACTIVE+CONNECTED — cannot sweep "
            "(the caller should not offer 'Sweep now' in this state)"
        )

    resolved_now = now if now is not None else utc_now()
    bootstrap_timestamp = resolved_now - timedelta(days=bootstrap_days)

    with sweep_lock.held(mailbox.mailbox_id):
        run = sweep_run_repository.create_run(mailbox_id=mailbox.mailbox_id, trigger=trigger)

        folders_attempted: list[str] = []
        messages_seen = 0
        messages_new = 0
        evidence_created = 0
        duplicates = 0
        quarantined = 0
        failures = 0
        any_folder_had_transient_failure = False
        any_folder_fully_succeeded = False

        try:
            for folder in SWEPT_FOLDERS:
                folders_attempted.append(folder)
                cursor = cursor_repository.get_or_bootstrap(
                    mailbox_id=mailbox.mailbox_id,
                    provider_kind=mailbox.provider_kind,
                    folder=folder,
                    bootstrap_timestamp=bootstrap_timestamp,
                )
                is_bootstrap_round = cursor.delta_link is None

                folder_had_transient_failure = False
                final_delta_link: Optional[str] = None
                next_link: Optional[str] = None
                first_page = True

                while True:
                    page = adapter.fetch_folder_delta(
                        mailbox_id=mailbox.mailbox_id,
                        folder=folder,
                        delta_link=cursor.delta_link if (first_page and not is_bootstrap_round) else None,
                        next_link=next_link if not first_page else None,
                        bootstrap_timestamp=cursor.bootstrap_timestamp if (first_page and is_bootstrap_round) else None,
                    )
                    first_page = False

                    if page.status == GraphOutcomeStatus.RATE_LIMITED:
                        backoff = min(page.retry_after_seconds or 5.0, _MAX_RATE_LIMIT_BACKOFF_SECONDS)
                        sleep_fn(backoff)
                        page = adapter.fetch_folder_delta(
                            mailbox_id=mailbox.mailbox_id,
                            folder=folder,
                            delta_link=None,
                            next_link=next_link,
                            bootstrap_timestamp=None,
                        )
                        if page.status == GraphOutcomeStatus.RATE_LIMITED:
                            folder_had_transient_failure = True
                            failures += 1
                            break

                    if page.status in (GraphOutcomeStatus.AUTH_ERROR,):
                        raise _SweepStopped(
                            error_code=SweepFailureReason.TOKEN_REFRESH_FAILED,
                            error_detail=page.error_detail or "Microsoft OAuth token invalid/expired; reconnect required",
                        )
                    if page.status == GraphOutcomeStatus.CONFIG_ERROR:
                        raise _SweepStopped(
                            error_code=SweepFailureReason.CONFIG_ERROR,
                            error_detail=page.error_detail or "Microsoft mail is not configured",
                        )
                    if page.status == GraphOutcomeStatus.PERMISSION_ERROR:
                        adapter.report_connection_error(
                            mailbox.mailbox_id,
                            error_code=SweepFailureReason.PERMISSION_ERROR,
                            error_detail=page.error_detail or "permission error",
                        )
                        folder_had_transient_failure = True
                        failures += 1
                        break
                    if page.status == GraphOutcomeStatus.RESYNC_REQUIRED:
                        folder_had_transient_failure = True
                        failures += 1
                        break
                    if page.status in (
                        GraphOutcomeStatus.TRANSPORT_ERROR,
                        GraphOutcomeStatus.TIMEOUT,
                        GraphOutcomeStatus.MALFORMED_RESPONSE,
                        GraphOutcomeStatus.PROVIDER_ERROR,
                    ):
                        folder_had_transient_failure = True
                        failures += 1
                        break

                    # OK — process this page's messages.
                    for msg in page.messages:
                        if msg.removed:
                            continue
                        messages_seen += 1
                        existing = message_repository.find_by_provider_id(mailbox.mailbox_id, msg.immutable_id)
                        if existing is not None and existing.ingestion_status in (
                            INGESTION_STATUS_INGESTED,
                            INGESTION_STATUS_QUARANTINED,
                            INGESTION_STATUS_FAILED,
                            INGESTION_STATUS_VANISHED,
                        ):
                            duplicates += 1
                            message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=existing.ingestion_status,
                                evidence_id=existing.evidence_id,
                            )
                            continue

                        content_result = adapter.fetch_message_content(
                            mailbox_id=mailbox.mailbox_id, immutable_message_id=msg.immutable_id
                        )

                        if content_result.status == GraphOutcomeStatus.NOT_FOUND:
                            messages_new += 1
                            message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_VANISHED,
                            )
                            continue

                        if content_result.status == GraphOutcomeStatus.AUTH_ERROR:
                            raise _SweepStopped(
                                error_code=SweepFailureReason.TOKEN_REFRESH_FAILED,
                                error_detail=content_result.error_detail or "Microsoft OAuth token invalid/expired",
                            )

                        if content_result.status != GraphOutcomeStatus.OK:
                            # Transient content-fetch failure: leave NO
                            # message row at all (see module docstring)
                            # so the next sweep re-sees this message as
                            # NEW and retries. Deliberately NOT counted
                            # in `messages_new` (that field means "a new
                            # MailboxMessage row was created this run" —
                            # see the contract's own field description)
                            # — only `failures` reflects this attempt.
                            failures += 1
                            folder_had_transient_failure = True
                            continue

                        messages_new += 1
                        outcome = ingest_email_evidence(
                            raw_mime_bytes=content_result.content or b"",
                            mailbox_id=mailbox.mailbox_id,
                            mailbox_source_id=mailbox_source_id,
                            immutable_provider_message_id=msg.immutable_id,
                            observed_at=msg.received_at or resolved_now,
                            received_at=msg.received_at or resolved_now,
                            sender_address=msg.sender_address,
                            subject=msg.subject,
                            api=api,
                            object_store=object_store,
                            scanner=scanner,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            correlation_id=run.sweep_run_id,
                        )

                        if outcome.status == INGEST_STATUS_INGESTED:
                            evidence_created += 1
                            message, _ = message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_INGESTED,
                                evidence_id=outcome.evidence.evidence_id if outcome.evidence is not None else None,
                            )
                            if outcome.evidence is not None:
                                api.record_provenance(
                                    subject_type="MailboxMessage",
                                    subject_id=message.mailbox_message_id,
                                    evidence_id=outcome.evidence.evidence_id,
                                    # Closed vocabulary (`core.provenance`'s own contract enum) —
                                    # this message projection was directly OBSERVED FROM its
                                    # evidence (the raw MIME), not extracted/derived/generated.
                                    relationship="OBSERVED_FROM",
                                    actor_type=actor_type,
                                    actor_id=actor_id,
                                    correlation_id=run.sweep_run_id,
                                )
                                # Architect spec's exact audit event name — canonical
                                # IDs only in the payload (never subject/sender text).
                                api.record_audit_event(
                                    event_type="EMAIL_EVIDENCE_INGESTED",
                                    actor_type=actor_type,
                                    actor_id=actor_id,
                                    subject_type="EvidenceItem",
                                    subject_id=outcome.evidence.evidence_id,
                                    correlation_id=run.sweep_run_id,
                                    causation_id=None,
                                    payload={
                                        "mailbox_id": mailbox.mailbox_id,
                                        "mailbox_message_id": message.mailbox_message_id,
                                        "sweep_run_id": run.sweep_run_id,
                                    },
                                )
                        elif outcome.status == INGEST_STATUS_QUARANTINED:
                            quarantined += 1
                            quarantined_message, _ = message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_QUARANTINED,
                            )
                            api.record_audit_event(
                                event_type="EMAIL_EVIDENCE_QUARANTINED",
                                actor_type=actor_type,
                                actor_id=actor_id,
                                subject_type="MailboxMessage",
                                subject_id=quarantined_message.mailbox_message_id,
                                correlation_id=run.sweep_run_id,
                                causation_id=None,
                                payload={"mailbox_id": mailbox.mailbox_id, "sweep_run_id": run.sweep_run_id},
                            )
                        else:  # INGEST_STATUS_FAILED — oversized message, a governed terminal outcome
                            message_repository.record_observation(
                                mailbox_id=mailbox.mailbox_id,
                                provider_kind=mailbox.provider_kind,
                                immutable_provider_message_id=msg.immutable_id,
                                internet_message_id=msg.internet_message_id,
                                observed_folder=folder,
                                subject=msg.subject,
                                sender_address=msg.sender_address,
                                sender_display_name=msg.sender_display_name,
                                received_at=msg.received_at or resolved_now,
                                has_attachments=msg.has_attachments,
                                ingestion_status=INGESTION_STATUS_FAILED,
                            )

                    if page.next_link:
                        next_link = page.next_link
                        continue

                    final_delta_link = page.delta_link
                    break

                any_folder_had_transient_failure = any_folder_had_transient_failure or folder_had_transient_failure
                if not folder_had_transient_failure and final_delta_link:
                    cursor_repository.advance_cursor(
                        mailbox_id=mailbox.mailbox_id,
                        provider_kind=mailbox.provider_kind,
                        folder=folder,
                        delta_link=final_delta_link,
                    )
                    any_folder_fully_succeeded = True

        except _SweepStopped as stopped:
            completed = sweep_run_repository.complete_run(
                run.sweep_run_id,
                new_status="FAILED",
                folders_attempted=folders_attempted,
                messages_seen=messages_seen,
                messages_new=messages_new,
                evidence_created=evidence_created,
                duplicates=duplicates,
                quarantined=quarantined,
                failures=failures,
                error_code=stopped.error_code,
                error_detail=stopped.error_detail,
            )
            return completed

        if any_folder_had_transient_failure:
            # PARTIAL when SOMETHING durable was accomplished this run
            # (a whole folder's round completed and its cursor
            # advanced, or at least one message was durably ingested/
            # quarantined) — FAILED only when nothing durable happened
            # at all (architect spec: "PARTIAL — at least one folder/
            # message hit a transient failure but at least one other
            # folder/message succeeded").
            new_status = (
                "PARTIAL"
                if (any_folder_fully_succeeded or messages_new > 0 or evidence_created > 0 or quarantined > 0)
                else "FAILED"
            )
            error_code = SweepFailureReason.PARTIAL_FAILURES if new_status == "PARTIAL" else SweepFailureReason.PROVIDER_ERROR
            completed = sweep_run_repository.complete_run(
                run.sweep_run_id,
                new_status=new_status,
                folders_attempted=folders_attempted,
                messages_seen=messages_seen,
                messages_new=messages_new,
                evidence_created=evidence_created,
                duplicates=duplicates,
                quarantined=quarantined,
                failures=failures,
                error_code=error_code,
                error_detail=f"{failures} message(s)/folder page(s) hit a transient failure this run",
            )
            return completed

        completed = sweep_run_repository.complete_run(
            run.sweep_run_id,
            new_status="SUCCEEDED",
            folders_attempted=folders_attempted,
            messages_seen=messages_seen,
            messages_new=messages_new,
            evidence_created=evidence_created,
            duplicates=duplicates,
            quarantined=quarantined,
            failures=failures,
        )
        mailbox_repository.record_microsoft_sweep_success(mailbox.mailbox_id, swept_at=resolved_now)
        return completed
