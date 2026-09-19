"""SQLAlchemy table definition for the durable, mailbox-specific
sender-policy registry (CD-6 architect amendment — two-stage mail
processing; extended by the CD-6 GUI-operations-foundation follow-on WO
into a three-state MUST_READ/GRAYLIST/BLACKLIST model plus a genuinely
new EXACT_ADDRESS match mode — see `services/mailbox/domain_rule.py`'s
own module docstring). Mirrors `persistence/postgres/mailbox_models.py`'s
own conventions.

Two PARTIAL unique indexes, not one plain UniqueConstraint
------------------------------------------------------------------------
A domain-level rule (`EXACT`/`INCLUDE_SUBDOMAINS`) is still uniquely
identified by (mailbox_id, sender_domain) — `uq_mailbox_domain_rules_mailbox_domain_scope`
enforces this, but now scoped with `WHERE match_mode != 'EXACT_ADDRESS'`
so it never collides with the SEPARATE `EXACT_ADDRESS` identity space
below. An `EXACT_ADDRESS` rule is uniquely identified by (mailbox_id,
sender_address) instead — `uq_mailbox_domain_rules_mailbox_address`
enforces THAT, scoped with `WHERE match_mode = 'EXACT_ADDRESS'`. Two
different addresses at the SAME domain (e.g. `alice@vendor.com` and
`bob@vendor.com`) must each get their own row — a plain
(mailbox_id, sender_domain) constraint alone could never allow that,
hence the second, address-scoped index rather than folding
`sender_address` into the first constraint (which would also break on
Postgres's own NULL-is-distinct semantics for the many domain-level
rows that never populate `sender_address` at all).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class MailboxDomainRuleRow(Base):
    """Persisted form of `services.mailbox.domain_rule.MailboxDomainRule`."""

    __tablename__ = "mailbox_domain_rules"
    __table_args__ = (
        Index(
            "uq_mailbox_domain_rules_mailbox_domain_scope",
            "mailbox_id",
            "sender_domain",
            unique=True,
            postgresql_where=sa.text("match_mode != 'EXACT_ADDRESS'"),
        ),
        Index(
            "uq_mailbox_domain_rules_mailbox_address",
            "mailbox_id",
            "sender_address",
            unique=True,
            postgresql_where=sa.text("match_mode = 'EXACT_ADDRESS'"),
        ),
    )

    rule_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    sender_domain: Mapped[str] = mapped_column(String, nullable=False)
    # CD-6 GUI-operations-foundation follow-on WO — populated ONLY for
    # an EXACT_ADDRESS rule; see this module's own docstring.
    sender_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
