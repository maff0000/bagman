"""SQLAlchemy table definition for the durable, mailbox-specific
domain-policy registry (CD-6 architect amendment — two-stage mail
processing). Mirrors `persistence/postgres/mailbox_models.py`'s own
conventions.

`uq_mailbox_domain_rules_mailbox_domain` is the real, database-level
backstop for `services.mailbox.domain_rule`'s own documented canonical
uniqueness rule: (mailbox_id, sender_domain) — never a second row for a
domain an operator has changed their mind about.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class MailboxDomainRuleRow(Base):
    """Persisted form of `services.mailbox.domain_rule.MailboxDomainRule`."""

    __tablename__ = "mailbox_domain_rules"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "sender_domain", name="uq_mailbox_domain_rules_mailbox_domain"),
    )

    rule_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    sender_domain: Mapped[str] = mapped_column(String, nullable=False)
    match_mode: Mapped[str] = mapped_column(String, nullable=False)
    policy: Mapped[str] = mapped_column(String, nullable=False)
    # Never a foreign key — mirrors MailboxSourceRow.default_entity_id's
    # own "hint/approved routing, never a hard ownership constraint"
    # reasoning one layer up.
    destination_entity_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    destination_mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    processor_hint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
