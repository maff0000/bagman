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
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "mailbox/bagman.mailbox_message.v1.schema.json"
SCHEMA_VERSION = "bagman.mailbox_message.v1"

FOLDER_INBOX = "INBOX"
FOLDER_JUNK = "JUNK"
FOLDERS = frozenset({FOLDER_INBOX, FOLDER_JUNK})

INGESTION_STATUS_INGESTED = "INGESTED"
INGESTION_STATUS_QUARANTINED = "QUARANTINED"
INGESTION_STATUS_FAILED = "FAILED"
INGESTION_STATUS_VANISHED = "VANISHED"
INGESTION_STATUSES = frozenset(
    {INGESTION_STATUS_INGESTED, INGESTION_STATUS_QUARANTINED, INGESTION_STATUS_FAILED, INGESTION_STATUS_VANISHED}
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
            "subject": self.subject,
            "sender_address": self.sender_address,
            "sender_display_name": self.sender_display_name,
            "received_at": to_contract_string(self.received_at),
            "has_attachments": self.has_attachments,
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


def _terminal_rank(status: str) -> int:
    """INGESTED is the most 'advanced' outcome; never let a later,
    lesser re-observation (e.g. a stale replay that only knows
    QUARANTINED/FAILED) downgrade an already-INGESTED row."""
    return {INGESTION_STATUS_INGESTED: 3, INGESTION_STATUS_QUARANTINED: 2, INGESTION_STATUS_VANISHED: 1,
            INGESTION_STATUS_FAILED: 0}.get(status, 0)


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
                    subject=subject,
                    sender_address=sender_address,
                    sender_display_name=sender_display_name,
                    received_at=received_at,
                    has_attachments=has_attachments,
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
        if _terminal_rank(ingestion_status) >= _terminal_rank(current.ingestion_status):
            new_status = ingestion_status
            new_evidence_id = evidence_id if evidence_id is not None else current.evidence_id

        try:
            updated = dataclasses.replace(
                current,
                observed_folder=observed_folder,
                last_seen_at=now,
                evidence_id=new_evidence_id,
                ingestion_status=new_status,
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
