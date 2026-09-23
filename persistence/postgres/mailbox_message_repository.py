"""PostgreSQL-backed implementation of
``services.mailbox.message.MailboxMessageRepository`` (CD-6 Slice 4).
Mirrors ``persistence/postgres/mailbox_repository.py``'s own
"stateless, fresh Session per call, with_for_update row-locking, never
let a raw SQLAlchemy exception escape" discipline.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import exists, func
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core import identity
from core.errors import NotFoundError, PersistenceError, ValidationError
from core.contract_validation import validate_against_contract
from core.timestamps import utc_now
from persistence.postgres.mailbox_message_models import MailboxMessageRow
from persistence.postgres.session import get_engine, session_scope
from services.mailbox.domain_rule import domain_in_scope, normalize_domain
from services.mailbox.message import (
    INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
    MailboxMessage,
    MailboxMessageRepository,
    _terminal_rank,
)

_SCHEMA = "mailbox/bagman.mailbox_message.v1.schema.json"


def _row_to_domain(row: MailboxMessageRow) -> MailboxMessage:
    return MailboxMessage(
        mailbox_message_id=row.mailbox_message_id,
        mailbox_id=row.mailbox_id,
        provider_kind=row.provider_kind,
        immutable_provider_message_id=row.immutable_provider_message_id,
        internet_message_id=row.internet_message_id,
        observed_folder=row.observed_folder,
        observed_folder_display_name=row.observed_folder_display_name,
        subject=row.subject,
        sender_address=row.sender_address,
        sender_display_name=row.sender_display_name,
        sender_domain=row.sender_domain,
        received_at=row.received_at,
        has_attachments=row.has_attachments,
        attachment_metadata=tuple(row.attachment_metadata_ or []),
        auth_signals=dict(row.auth_signals or {}),
        evidence_id=row.evidence_id,
        ingestion_status=row.ingestion_status,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        metadata=dict(row.metadata_),
        discovery_candidate=row.discovery_candidate,
        discovery_reason=row.discovery_reason,
        discovery_checked_at=row.discovery_checked_at,
    )


class PostgresMailboxMessageRepository(MailboxMessageRepository):
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or get_engine()

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
        received_at,
        has_attachments: bool,
        ingestion_status: str,
        evidence_id: Optional[str] = None,
        sender_domain: Optional[str] = None,
        observed_folder_display_name: Optional[str] = None,
        attachment_metadata=None,
        auth_signals: Optional[Mapping[str, Optional[str]]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        discovery_candidate: Optional[bool] = None,
        discovery_reason: Optional[str] = None,
        discovery_checked_at=None,
    ) -> tuple[MailboxMessage, bool]:
        now = utc_now()
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(MailboxMessageRow)
                    .filter_by(mailbox_id=mailbox_id, immutable_provider_message_id=immutable_provider_message_id)
                    .with_for_update()
                    .one_or_none()
                )
                if row is None:
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
                        discovery_candidate=discovery_candidate,
                        discovery_reason=discovery_reason,
                        discovery_checked_at=discovery_checked_at,
                    )
                    validate_against_contract(candidate.to_dict(), _SCHEMA)
                    new_row = MailboxMessageRow(
                        mailbox_message_id=candidate.mailbox_message_id,
                        mailbox_id=candidate.mailbox_id,
                        provider_kind=candidate.provider_kind,
                        immutable_provider_message_id=candidate.immutable_provider_message_id,
                        internet_message_id=candidate.internet_message_id,
                        observed_folder=candidate.observed_folder,
                        observed_folder_display_name=candidate.observed_folder_display_name,
                        subject=candidate.subject,
                        sender_address=candidate.sender_address,
                        sender_display_name=candidate.sender_display_name,
                        sender_domain=candidate.sender_domain,
                        received_at=candidate.received_at,
                        has_attachments=candidate.has_attachments,
                        attachment_metadata_=[dict(a) for a in candidate.attachment_metadata],
                        auth_signals=dict(candidate.auth_signals),
                        evidence_id=candidate.evidence_id,
                        ingestion_status=candidate.ingestion_status,
                        first_seen_at=candidate.first_seen_at,
                        last_seen_at=candidate.last_seen_at,
                        metadata_={},
                        discovery_candidate=candidate.discovery_candidate,
                        discovery_reason=candidate.discovery_reason,
                        discovery_checked_at=candidate.discovery_checked_at,
                    )
                    session.add(new_row)
                    return candidate, True

                current = _row_to_domain(row)
                new_evidence_id = current.evidence_id
                new_status = current.ingestion_status
                new_metadata = dict(current.metadata)
                if _terminal_rank(ingestion_status) >= _terminal_rank(current.ingestion_status):
                    new_status = ingestion_status
                    new_evidence_id = evidence_id if evidence_id is not None else current.evidence_id
                    if metadata is not None:
                        new_metadata.update(dict(metadata))

                row.observed_folder = observed_folder
                if observed_folder_display_name is not None:
                    row.observed_folder_display_name = observed_folder_display_name
                row.last_seen_at = now
                row.evidence_id = new_evidence_id
                row.ingestion_status = new_status
                row.sender_domain = sender_domain if sender_domain is not None else current.sender_domain
                if attachment_metadata is not None:
                    row.attachment_metadata_ = [dict(a) for a in attachment_metadata]
                if auth_signals is not None:
                    row.auth_signals = dict(auth_signals)
                row.metadata_ = new_metadata
                # Never silently blank out an already-set discovery
                # decision on a benign re-observation that omits these
                # params — see `services.mailbox.message
                # .MailboxMessageRepository.record_observation`'s own
                # abstract docstring for the full "preserve unless
                # explicitly overwritten" discipline this mirrors.
                if discovery_candidate is not None:
                    row.discovery_candidate = discovery_candidate
                if discovery_reason is not None:
                    row.discovery_reason = discovery_reason
                if discovery_checked_at is not None:
                    row.discovery_checked_at = discovery_checked_at
                updated = _row_to_domain(row)
                validate_against_contract(updated.to_dict(), _SCHEMA)
                return updated, False
        except (ValidationError, NotFoundError):
            raise
        except IntegrityError as exc:
            raise PersistenceError(f"could not record MailboxMessage observation: {exc}") from exc
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not record MailboxMessage observation: {exc}") from exc

    def get_message(self, mailbox_message_id: str) -> MailboxMessage:
        try:
            with session_scope(self._engine) as session:
                row = session.get(MailboxMessageRow, mailbox_message_id)
                if row is None:
                    raise NotFoundError(f"no MailboxMessage with mailbox_message_id '{mailbox_message_id}'")
                return _row_to_domain(row)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not read MailboxMessage: {exc}") from exc

    def find_by_provider_id(self, mailbox_id: str, immutable_provider_message_id: str) -> Optional[MailboxMessage]:
        try:
            with session_scope(self._engine) as session:
                row = (
                    session.query(MailboxMessageRow)
                    .filter_by(mailbox_id=mailbox_id, immutable_provider_message_id=immutable_provider_message_id)
                    .one_or_none()
                )
                return _row_to_domain(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not look up MailboxMessage: {exc}") from exc

    def sender_address_observed(self, mailbox_id: str, sender_address: str) -> bool:
        normalized = sender_address.strip().lower()
        try:
            with session_scope(self._engine) as session:
                return session.query(
                    exists().where(
                        MailboxMessageRow.mailbox_id == mailbox_id,
                        func.lower(MailboxMessageRow.sender_address) == normalized,
                    )
                ).scalar()
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not check sender_address_observed for MailboxMessage: {exc}") from exc

    def list_messages(self, *, mailbox_id: str, limit: Optional[int] = None, offset: int = 0):
        try:
            with session_scope(self._engine) as session:
                query = (
                    session.query(MailboxMessageRow)
                    .filter_by(mailbox_id=mailbox_id)
                    .order_by(MailboxMessageRow.received_at.desc(), MailboxMessageRow.mailbox_message_id.desc())
                    .offset(offset)
                )
                if limit is not None:
                    query = query.limit(limit)
                return [_row_to_domain(r) for r in query.all()]
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list MailboxMessage rows: {exc}") from exc

    def list_candidate_messages_for_domain(
        self, *, mailbox_id: str, sender_domain: str, include_subdomains: bool = False
    ) -> list[MailboxMessage]:
        normalized_domain = normalize_domain(sender_domain)
        try:
            with session_scope(self._engine) as session:
                rows = (
                    session.query(MailboxMessageRow)
                    .filter(
                        MailboxMessageRow.mailbox_id == mailbox_id,
                        MailboxMessageRow.ingestion_status == INGESTION_STATUS_CHECKED_NOT_CANDIDATE,
                        # Second CD-6 architect amendment (persisted
                        # discovery decision) — the real "was actually
                        # identified as a credible financial candidate"
                        # gate. Unlike `sender_domain` normalisation
                        # below (a legitimate Python-side re-check, since
                        # stored casing is only a convention, never
                        # enforced at the DB layer), this is a plain
                        # boolean equality with no such ambiguity, so it
                        # IS filtered in SQL — never merely "not None"/
                        # "not False", the literal boolean `True`.
                        MailboxMessageRow.discovery_candidate.is_(True),
                        # `sender_domain` is stored lowercased already (see
                        # `services/mailbox/sweep.py::_extract_sender_domain`),
                        # but this repository re-normalises defensively
                        # here too, in Python, rather than trusting that
                        # invariant at the SQL layer — mirrors this
                        # module's own "never trust a stored value's
                        # normalisation blindly" caution elsewhere.
                        MailboxMessageRow.sender_domain.isnot(None),
                    )
                    .order_by(MailboxMessageRow.received_at.asc(), MailboxMessageRow.mailbox_message_id.asc())
                    .all()
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(f"could not list candidate MailboxMessage rows for domain: {exc}") from exc
        return [
            _row_to_domain(row)
            for row in rows
            if domain_in_scope(
                normalize_domain(row.sender_domain), normalized_domain, include_subdomains=include_subdomains
            )
        ]
