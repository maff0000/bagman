"""SQLAlchemy table definition for BAGMAN's durable mailbox-definition
registry (CD-6 Slice 3: Mailbox Management). Mirrors
``persistence/postgres/xero_models.py``'s own conventions exactly.

One table, matching ``services/mailbox/mailbox.py``'s one domain
object:

* ``mailbox_sources`` — one row per operator-registered mailbox
  definition (``services.mailbox.mailbox.MailboxSource``).
  ``uq_mailbox_sources_email_address`` is a real, PLAIN (non-partial)
  unique constraint on ``email_address`` — GLOBAL uniqueness, never
  freed by retirement (see ``services/mailbox/mailbox.py``'s own
  module docstring "Uniqueness" section for the full, documented
  reasoning behind that choice). ``email_address`` is always stored
  already-lowercased by the repository layer before this constraint
  ever sees it (never relying on a database-level ``LOWER()``
  expression index — normalising once, at the application boundary,
  keeps the stored value and the comparison value identical, which is
  what the plain contract-shaped ``to_dict()`` round-trip already
  assumes everywhere else in this codebase).

  ``default_entity_id`` is deliberately a plain nullable UUID column,
  NOT a foreign key to ``governed_entities`` — exactly the same "hint,
  never ownership assertion" reasoning
  ``persistence/postgres/models.py``'s own
  ``SourceRow.governed_entity_hint`` documents for the identical
  design choice; a hard FK here would silently upgrade a display hint
  into a real ownership constraint (and would also block creating a
  mailbox with an entity-hint pointing at a value this table has no
  visibility into, since hint validity is checked at the HTTP layer,
  never here — see ``services/mailbox/mailbox.py``'s module docstring).

  No secret column exists on this table, and none may ever be added —
  see ``services/mailbox/mailbox.py``'s own "Secret doctrine" section.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class MailboxSourceRow(Base):
    """Persisted form of
    `services.mailbox.mailbox.MailboxSource` (CD-6 Slice 3)."""

    __tablename__ = "mailbox_sources"
    __table_args__ = (
        UniqueConstraint("email_address", name="uq_mailbox_sources_email_address"),
    )

    mailbox_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    email_address: Mapped[str] = mapped_column(String, nullable=False)
    provider_kind: Mapped[str] = mapped_column(String, nullable=False)
    # HINT ONLY (see module docstring) — deliberately never a foreign
    # key, mirrors SourceRow.governed_entity_hint exactly.
    default_entity_id: Mapped[Optional[str]] = mapped_column(_UUID, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    connection_state: Mapped[str] = mapped_column(String, nullable=False)
    last_connection_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_sweep_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_error_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
