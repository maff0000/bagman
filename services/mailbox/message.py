"""``MailboxMessage`` — the durable, provider-neutral message projection
(CD-6 Slice 4: first real mailbox adapter + sweep engine).

Not evidence bytes, not an accounting fact
-------------------------------------------
This is a lightweight PROJECTION of "a message the sweep engine
observed" — enough for the operator GUI's minimal, non-classifying
recent-mail list (received time / sender / subject / folder /
ingestion state). The immutable raw MIME bytes live in the canonical
``services.evidence`` architecture (an ``EvidenceItem`` + provenance
record — see ``services/mailbox/microsoft/evidence_ingest.py``); this
object only carries a pointer (``evidence_id``) to that. No AI/
classification/invoice/account-coding field exists here, and none may
ever be added in this slice — that is Slice 5's scope entirely (see
``services/mailbox/sweep.py``'s own module docstring for the same hard
boundary at the orchestration layer).

Canonical uniqueness — (mailbox_id, immutable_provider_message_id)
--------------------------------------------------------------------
A message moving between the two in-scope folders (Inbox <-> Junk)
must never create a second row: :meth:`MailboxMessageRepository
.record_observation` is a resolve-or-create-then-update operation,
never a blind insert — see that method's own docstring. This is the
SAME identity discipline ``core.external_reference``'s composite-tuple
uniqueness already establishes elsewhere in this codebase, applied
here to a provider message id instead of a generic external reference,
because a mailbox message additionally needs ``observed_folder``/
``last_seen_at`` kept current across re-observations (an
``ExternalReference`` row is create-once and never updated).
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.mailbox.domain_rule import normalize_domain

_SCHEMA = "mailbox/bagman.mailbox_message.v1.schema.json"
SCHEMA_VERSION = "bagman.mailbox_message.v1"

#: CD-6 architect amendment (recursive Microsoft Graph folder discovery)
#: — `observed_folder` is now an OPEN, provider-defined folder
#: identifier (any real Graph folder id — Inbox/Junk Email/Deleted
#: Items/any custom/nested/hidden monitored folder), never a closed
#: two-value enum. `FOLDER_INBOX`/`FOLDER_JUNK` remain defined purely as
#: historical/example identifier VALUES (several tests still use them
#: as two arbitrary, distinct folder-identity strings) — they are no
#: longer a closed set the contract/schema enforces; see
#: `services/mailbox/sweep.py`'s own module docstring for how the real
#: monitored-folder set is now discovered per mailbox instead.
FOLDER_INBOX = "INBOX"
FOLDER_JUNK = "JUNK"

INGESTION_STATUS_INGESTED = "INGESTED"
INGESTION_STATUS_QUARANTINED = "QUARANTINED"
INGESTION_STATUS_FAILED = "FAILED"
INGESTION_STATUS_VANISHED = "VANISHED"
#: CD-6 architect amendment (two-stage mail processing) — "seen,
#: checked, no meaningful signal (or an IGNORED domain rule matched),
#: nothing further happens". See services/mailbox/sweep.py's own
#: module docstring for exactly which Stage-B outcomes produce this.
INGESTION_STATUS_CHECKED_NOT_CANDIDATE = "CHECKED_NOT_CANDIDATE"
INGESTION_STATUSES = frozenset(
    {
        INGESTION_STATUS_INGESTED,
        INGESTION_STATUS_QUARANTINED,
        INGESTION_STATUS_FAILED,
        INGESTION_STATUS_VANISHED,
        INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    }
)

#: A message in ANY of these statuses has already been FINALLY decided
#: — the sweep engine's own duplicate short-circuit (see
#: services/mailbox/sweep.py) never re-runs the Stage-B gate for a
#: message already in one of these states, even if the governing
#: MailboxDomainRule has since changed (a documented judgment call —
#: see sweep.py's own module docstring).
FINAL_INGESTION_STATUSES = frozenset(
    {
        INGESTION_STATUS_INGESTED,
        INGESTION_STATUS_QUARANTINED,
        INGESTION_STATUS_FAILED,
        INGESTION_STATUS_VANISHED,
        INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    }
)


@dataclass(frozen=True)
class MailboxMessage:
    """One provider message projection. Immutable once constructed —
    every re-observation produces a NEW snapshot via
    :meth:`MailboxMessageRepository.record_observation`, never an
    in-place mutation."""

    mailbox_message_id: str
    mailbox_id: str
    provider_kind: str
    immutable_provider_message_id: str
    internet_message_id: Optional[str]
    #: The provider's own real, immutable folder identifier this
    #: message was MOST RECENTLY observed in (CD-6 architect amendment —
    #: recursive folder discovery; for Microsoft Graph: the real Graph
    #: folder id, e.g. resolved `inbox`/`junkemail`/`deleteditems`, or a
    #: custom/nested folder's own id) — the CANONICAL, gate-relevant
    #: value. Never a display name (folder display names can be
    #: renamed/localized — see `observed_folder_display_name` below for
    #: the human-readable counterpart).
    observed_folder: str
    subject: Optional[str]
    sender_address: Optional[str]
    sender_display_name: Optional[str]
    received_at: datetime
    has_attachments: bool
    evidence_id: Optional[str]
    ingestion_status: str
    first_seen_at: datetime
    last_seen_at: datetime
    #: CD-6 architect amendment (Stage A discovery) — the domain
    #: portion of `sender_address`, lowercased; the actual Stage-B gate
    #: key. `None` only when `sender_address` itself is absent.
    sender_domain: Optional[str] = None
    #: CD-6 architect amendment (recursive folder discovery) — the
    #: HUMAN-READABLE label for `observed_folder`'s own real folder id,
    #: for GUI/operator display ONLY (e.g. "Inbox", "Deleted Items", a
    #: custom folder's own display name) — never used for identity,
    #: gating, or cursor lookup (that is `observed_folder`'s own job).
    #: `None` for a row created before this amendment (the real 128
    #: already-ingested Infosecurs messages — additive/nullable, never a
    #: silently-invented backfilled value) or when a caller genuinely has
    #: no display name to report.
    observed_folder_display_name: Optional[str] = None
    #: Bounded, discovery-only per-attachment metadata (filename/
    #: content_type/size_bytes — NEVER content). See contract's own
    #: field description.
    attachment_metadata: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    #: Provider-supplied authentication-domain signals (spf/dkim/dmarc),
    #: captured separately from `sender_domain` — see contract's own
    #: field description ("Authentication/spoofing" doctrine).
    auth_signals: Mapping[str, Optional[str]] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "mailbox_message_id": self.mailbox_message_id,
            "mailbox_id": self.mailbox_id,
            "provider_kind": self.provider_kind,
            "immutable_provider_message_id": self.immutable_provider_message_id,
            "internet_message_id": self.internet_message_id,
            "observed_folder": self.observed_folder,
            "observed_folder_display_name": self.observed_folder_display_name,
            "subject": self.subject,
            "sender_address": self.sender_address,
            "sender_display_name": self.sender_display_name,
            "sender_domain": self.sender_domain,
            "received_at": to_contract_string(self.received_at),
            "has_attachments": self.has_attachments,
            "attachment_metadata": [dict(a) for a in self.attachment_metadata],
            "auth_signals": dict(self.auth_signals),
            "evidence_id": self.evidence_id,
            "ingestion_status": self.ingestion_status,
            "first_seen_at": to_contract_string(self.first_seen_at),
            "last_seen_at": to_contract_string(self.last_seen_at),
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }


class MailboxMessageRepository(abc.ABC):
    """Repository abstraction for MailboxMessage."""

    @abc.abstractmethod
    def record_observation(
        self,
        *,
        mailbox_id: str,
        provider_kind: str,
        immutable_provider_message_id: str,
        internet_message_id: Optional[str],
        observed_folder: str,
        subject: Optional[str],
        sender_address: Optional[str],
        sender_display_name: Optional[str],
        received_at: datetime,
        has_attachments: bool,
        ingestion_status: str,
        evidence_id: Optional[str] = None,
        sender_domain: Optional[str] = None,
        observed_folder_display_name: Optional[str] = None,
        attachment_metadata: Optional[Sequence[Mapping[str, Any]]] = None,
        auth_signals: Optional[Mapping[str, Optional[str]]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[MailboxMessage, bool]:
        """Resolve-or-create by (mailbox_id, immutable_provider_message_id):

        * unseen tuple -> create a new row, ``first_seen_at ==
          last_seen_at == utcnow()``. Returns ``(message, True)``.
        * seen tuple -> update ``observed_folder``/``last_seen_at`` (and
          ``evidence_id``/``ingestion_status`` if the caller supplies a
          more advanced outcome than what is already stored — never
          regresses an already-``INGESTED`` row back to a lesser
          status on a benign re-observation). Returns ``(message,
          False)``.

        This single call is what makes the sweep engine's idempotency
        guarantee concrete at the persistence layer — see
        ``services/mailbox/sweep.py``'s own module docstring.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_message(self, mailbox_message_id: str) -> MailboxMessage:
        raise NotImplementedError

    @abc.abstractmethod
    def find_by_provider_id(self, mailbox_id: str, immutable_provider_message_id: str) -> Optional[MailboxMessage]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_messages(
        self, *, mailbox_id: str, limit: Optional[int] = None, offset: int = 0
    ) -> list[MailboxMessage]:
        """Most-recently-received first, scoped to ``mailbox_id``."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_candidate_messages_for_domain(self, *, mailbox_id: str, sender_domain: str) -> list[MailboxMessage]:
        """Operational addendum (ahead of the first real large historical
        sweep) — the query
        ``services.mailbox.sweep.reprocess_all_historical_candidates_for_domain``
        uses to back-process EVERY historical candidate for a domain an
        operator just approved, not only the one message that happened to
        trigger the ``MAILBOX_DOMAIN_REVIEW`` Needs You item.

        Returns every ``MailboxMessage`` for ``mailbox_id`` whose
        ``sender_domain`` (normalised via
        :func:`services.mailbox.domain_rule.normalize_domain`, the SAME
        normalisation a governing ``MailboxDomainRule`` uses) matches
        ``sender_domain`` (also normalised) AND whose
        ``ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE`` —
        this is deliberately the ONLY eligible status: every other
        member of :data:`FINAL_INGESTION_STATUSES`
        (``INGESTED``/``QUARANTINED``/``FAILED``/``VANISHED``) is
        already a genuinely final, already-decided outcome that must
        never be re-fetched/re-ingested a second time (see
        ``services/mailbox/sweep.py``'s own
        ``_reprocess_one_message``/former
        ``reprocess_message_after_domain_rule_approval`` idempotency
        check, which this filter mirrors exactly —
        ``CHECKED_NOT_CANDIDATE`` is deliberately excluded from "already
        final" there for exactly this reprocessing purpose).

        Ordered oldest-received-first (``received_at`` ascending, then
        ``mailbox_message_id`` for a stable tie-break) — a reasonable,
        deterministic processing order: the earliest candidate from a
        newly-approved supplier is the one most likely to matter for
        accounting-period completeness, and a stable order makes a
        bounded, sequential back-process reproducible/resumable."""
        raise NotImplementedError


def _terminal_rank(status: str) -> int:
    """INGESTED is the most 'advanced' outcome; never let a later,
    lesser re-observation (e.g. a stale replay that only knows
    QUARANTINED/FAILED) downgrade an already-INGESTED row.
    CHECKED_NOT_CANDIDATE (CD-6 architect amendment) is the WEAKEST
    real outcome — pure discovery, nothing happened — so it ranks
    below every other terminal status; any later, stronger outcome
    (e.g. a rule-approval-triggered reprocess that reaches INGESTED)
    can always supersede it."""
    return {
        INGESTION_STATUS_INGESTED: 4,
        INGESTION_STATUS_QUARANTINED: 3,
        INGESTION_STATUS_VANISHED: 2,
        INGESTION_STATUS_FAILED: 1,
        INGESTION_STATUS_CHECKED_NOT_CANDIDATE: 0,
    }.get(status, 0)


class InMemoryMailboxMessageRepository(MailboxMessageRepository):
    """Narrow in-memory reference implementation."""

    def __init__(self) -> None:
        self._by_id: dict[str, MailboxMessage] = {}
        self._id_by_tuple: dict[tuple[str, str], str] = {}

    def record_observation(
        self,
        *,
        mailbox_id: str,
        provider_kind: str,
        immutable_provider_message_id: str,
        internet_message_id: Optional[str],
        observed_folder: str,
        subject: Optional[str],
        sender_address: Optional[str],
        sender_display_name: Optional[str],
        received_at: datetime,
        has_attachments: bool,
        ingestion_status: str,
        evidence_id: Optional[str] = None,
        sender_domain: Optional[str] = None,
        observed_folder_display_name: Optional[str] = None,
        attachment_metadata: Optional[Sequence[Mapping[str, Any]]] = None,
        auth_signals: Optional[Mapping[str, Optional[str]]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[MailboxMessage, bool]:
        key = (mailbox_id, immutable_provider_message_id)
        existing_id = self._id_by_tuple.get(key)
        now = utc_now()

        if existing_id is None:
            try:
                candidate = MailboxMessage(
                    mailbox_message_id=identity.generate_id(),
                    mailbox_id=mailbox_id,
                    provider_kind=provider_kind,
                    immutable_provider_message_id=immutable_provider_message_id,
                    internet_message_id=internet_message_id,
                    observed_folder=observed_folder,
                    observed_folder_display_name=observed_folder_display_name,
                    subject=subject,
                    sender_address=sender_address,
                    sender_display_name=sender_display_name,
                    sender_domain=sender_domain,
                    received_at=received_at,
                    has_attachments=has_attachments,
                    attachment_metadata=tuple(attachment_metadata) if attachment_metadata is not None else (),
                    auth_signals=dict(auth_signals) if auth_signals is not None else {},
                    evidence_id=evidence_id,
                    ingestion_status=ingestion_status,
                    first_seen_at=now,
                    last_seen_at=now,
                    metadata=dict(metadata) if metadata is not None else {},
                )
                validate_against_contract(candidate.to_dict(), _SCHEMA)
            except ValidationError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a raw exception
                raise ValidationError(f"could not record MailboxMessage observation: {exc}") from exc
            self._by_id[candidate.mailbox_message_id] = candidate
            self._id_by_tuple[key] = candidate.mailbox_message_id
            return candidate, True

        current = self._by_id[existing_id]
        new_evidence_id = current.evidence_id
        new_status = current.ingestion_status
        new_metadata = dict(current.metadata)
        if _terminal_rank(ingestion_status) >= _terminal_rank(current.ingestion_status):
            new_status = ingestion_status
            new_evidence_id = evidence_id if evidence_id is not None else current.evidence_id
            if metadata is not None:
                new_metadata.update(dict(metadata))

        try:
            updated = dataclasses.replace(
                current,
                observed_folder=observed_folder,
                observed_folder_display_name=(
                    observed_folder_display_name
                    if observed_folder_display_name is not None
                    else current.observed_folder_display_name
                ),
                last_seen_at=now,
                evidence_id=new_evidence_id,
                ingestion_status=new_status,
                sender_domain=sender_domain if sender_domain is not None else current.sender_domain,
                attachment_metadata=(
                    tuple(attachment_metadata) if attachment_metadata is not None else current.attachment_metadata
                ),
                auth_signals=dict(auth_signals) if auth_signals is not None else current.auth_signals,
                metadata=new_metadata,
            )
            validate_against_contract(updated.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not update MailboxMessage observation: {exc}") from exc
        self._by_id[existing_id] = updated
        return updated, False

    def get_message(self, mailbox_message_id: str) -> MailboxMessage:
        try:
            return self._by_id[mailbox_message_id]
        except KeyError:
            raise NotFoundError(f"no MailboxMessage with mailbox_message_id '{mailbox_message_id}'") from None

    def find_by_provider_id(self, mailbox_id: str, immutable_provider_message_id: str) -> Optional[MailboxMessage]:
        existing_id = self._id_by_tuple.get((mailbox_id, immutable_provider_message_id))
        return self._by_id[existing_id] if existing_id is not None else None

    def list_messages(
        self, *, mailbox_id: str, limit: Optional[int] = None, offset: int = 0
    ) -> list[MailboxMessage]:
        items = [m for m in self._by_id.values() if m.mailbox_id == mailbox_id]
        items.sort(key=lambda m: (m.received_at, m.mailbox_message_id), reverse=True)
        if limit is None:
            return items[offset:]
        return items[offset : offset + limit]

    def list_candidate_messages_for_domain(self, *, mailbox_id: str, sender_domain: str) -> list[MailboxMessage]:
        normalized_domain = normalize_domain(sender_domain)
        items = [
            m
            for m in self._by_id.values()
            if m.mailbox_id == mailbox_id
            and m.sender_domain is not None
            and normalize_domain(m.sender_domain) == normalized_domain
            and m.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE
        ]
        items.sort(key=lambda m: (m.received_at, m.mailbox_message_id))
        return items
