"""SQLAlchemy table definitions for CD-6 Slice 4's Microsoft-adapter
plumbing: the sweep-run ledger, the per-folder delta cursor, the
per-mailbox sweep lease, and the mailbox-scoped OAuth state. Mirrors
``persistence/postgres/xero_models.py``'s own conventions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class MailboxSweepRunRow(Base):
    """Persisted form of `services.mailbox.sweep_run.MailboxSweepRun`."""

    __tablename__ = "mailbox_sweep_runs"

    sweep_run_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    folders_attempted: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    messages_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class MailboxFolderCursorRow(Base):
    """Persisted form of `services.mailbox.cursor.MailboxFolderCursor`
    — composite-primary-keyed on (mailbox_id, provider_kind, folder),
    the same natural key the domain module itself uses (never a
    separate synthetic id — mirrors
    `persistence/postgres/xero_models.py::XeroOAuthStateRow`'s own
    "the value's own identity IS the lookup key" reasoning)."""

    __tablename__ = "mailbox_folder_cursors"
    __table_args__ = (PrimaryKeyConstraint("mailbox_id", "provider_kind", "folder"),)

    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    provider_kind: Mapped[str] = mapped_column(String, nullable=False)
    folder: Mapped[str] = mapped_column(String, nullable=False)
    delta_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bootstrap_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MailboxSweepLockRow(Base):
    """Persisted form of the per-mailbox sweep lease
    (`services.mailbox.lock.MailboxSweepLock`) — see that module's own
    docstring for why this is a DEDICATED lease table rather than a
    `with_for_update()` row lock on the mailbox's own row."""

    __tablename__ = "mailbox_sweep_locks"

    mailbox_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    owner_token: Mapped[str] = mapped_column(String, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MailboxMicrosoftOAuthStateRow(Base):
    """Persisted form of
    `services.mailbox.microsoft.oauth_state.MailboxOAuthState` —
    primary-keyed directly on the state token itself, mirrors
    `persistence/postgres/xero_models.py::XeroOAuthStateRow` exactly
    (see that class's own docstring for the reasoning)."""

    __tablename__ = "mailbox_microsoft_oauth_states"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
