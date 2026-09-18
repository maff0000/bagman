"""SQLAlchemy table definition for the durable mailbox-message
projection (CD-6 Slice 4). Mirrors `persistence/postgres/mailbox_models.py`'s
own conventions.

`uq_mailbox_messages_mailbox_provider_id` is the real, database-level
backstop for `services.mailbox.message`'s own documented canonical
uniqueness rule: (mailbox_id, immutable_provider_message_id).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class MailboxMessageRow(Base):
    """Persisted form of `services.mailbox.message.MailboxMessage`."""

    __tablename__ = "mailbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_id", "immutable_provider_message_id", name="uq_mailbox_messages_mailbox_provider_id"
        ),
        Index("ix_mailbox_messages_mailbox_id", "mailbox_id"),
    )

    mailbox_message_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    provider_kind: Mapped[str] = mapped_column(String, nullable=False)
    immutable_provider_message_id: Mapped[str] = mapped_column(String, nullable=False)
    internet_message_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    observed_folder: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sender_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sender_display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # CD-6 architect amendment (two-stage mail processing) — additive,
    # nullable/defaulted columns; existing rows (the real 128-message
    # Slice 4A acceptance-test ingest) get NULL/[]/{} defaults, never a
    # silently-invented value.
    sender_domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attachment_metadata_: Mapped[list] = mapped_column("attachment_metadata", JSONB, nullable=False, default=list)
    auth_signals: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evidence_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    ingestion_status: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
