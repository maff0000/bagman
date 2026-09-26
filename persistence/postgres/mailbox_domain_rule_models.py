"""SQLAlchemy table definition for the durable, mailbox-specific
sender-policy registry (CD-6 architect amendment — two-stage mail
processing; extended by the CD-6 GUI-operations-foundation follow-on WO
into a three-state MUST_READ/GRAYLIST/BLACKLIST model, a genuinely new
EXACT_ADDRESS match mode, and — this delivery — a genuinely new
EXACT_DOMAIN_SUBJECT match mode (deterministic subject-aware mailbox
domain policy) — see `services/mailbox/domain_rule.py`'s own module
docstring). Mirrors `persistence/postgres/mailbox_models.py`'s own
conventions.

THREE PARTIAL unique indexes, not one plain UniqueConstraint
------------------------------------------------------------------------
A domain-level rule (`EXACT`/`INCLUDE_SUBDOMAINS`) is uniquely
identified by (mailbox_id, sender_domain) —
`uq_mailbox_domain_rules_mailbox_domain_scope` enforces this, scoped
with `WHERE match_mode IN ('EXACT', 'INCLUDE_SUBDOMAINS')` (corrected by
this delivery from the previous, now-too-broad `!= 'EXACT_ADDRESS'`
scope — a new `EXACT_DOMAIN_SUBJECT` row would otherwise incorrectly
collide with it) so it never collides with either of the two other
identity spaces below. An `EXACT_ADDRESS` rule is uniquely identified by
(mailbox_id, sender_address) instead — `uq_mailbox_domain_rules_mailbox_address`
enforces THAT, scoped with `WHERE match_mode = 'EXACT_ADDRESS'`,
unchanged by this delivery. Two different addresses at the SAME domain
(e.g. `alice@vendor.com` and `bob@vendor.com`) must each get their own
row — a plain (mailbox_id, sender_domain) constraint alone could never
allow that, hence the second, address-scoped index rather than folding
`sender_address` into the first constraint (which would also break on
Postgres's own NULL-is-distinct semantics for the many domain-level rows
that never populate `sender_address` at all). An `EXACT_DOMAIN_SUBJECT`
rule (this delivery) is uniquely identified by (mailbox_id,
sender_domain, subject_predicate_type, subject_predicate_value) instead
— `uq_mailbox_domain_rules_mailbox_subject` enforces THAT, scoped with
`WHERE match_mode = 'EXACT_DOMAIN_SUBJECT'` — a THIRD, separate identity
space: one domain may simultaneously hold a domain-level fallback rule,
several EXACT_ADDRESS overrides, AND several EXACT_DOMAIN_SUBJECT rules,
each in its own uniqueness scope, never colliding with the others.
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
            postgresql_where=sa.text("match_mode IN ('EXACT', 'INCLUDE_SUBDOMAINS')"),
        ),
        Index(
            "uq_mailbox_domain_rules_mailbox_address",
            "mailbox_id",
            "sender_address",
            unique=True,
            postgresql_where=sa.text("match_mode = 'EXACT_ADDRESS'"),
        ),
        Index(
            "uq_mailbox_domain_rules_mailbox_subject",
            "mailbox_id",
            "sender_domain",
            "subject_predicate_type",
            "subject_predicate_value",
            unique=True,
            postgresql_where=sa.text("match_mode = 'EXACT_DOMAIN_SUBJECT'"),
        ),
    )

    rule_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    sender_domain: Mapped[str] = mapped_column(String, nullable=False)
    # CD-6 GUI-operations-foundation follow-on WO — populated ONLY for
    # an EXACT_ADDRESS rule; see this module's own docstring.
    sender_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Deterministic subject-aware mailbox domain policy (this delivery)
    # — populated ONLY for an EXACT_DOMAIN_SUBJECT rule; see this
    # module's own docstring.
    subject_predicate_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject_predicate_value: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
