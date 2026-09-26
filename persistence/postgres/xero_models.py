"""SQLAlchemy table definitions for BAGMAN's durable Xero domain (CD-6
Slice 2, PID §98.4, architect spec §2/§4/§17). Mirrors
``persistence/postgres/needs_you_models.py``'s own conventions and
documented partial-unique-index discipline exactly.

Four tables, matching ``services/xero/*.py``'s four domain objects:

* ``xero_connections`` — one row per BAGMAN entity that has ever
  attempted a Xero connection (``services.xero.connection.XeroConnection``).
  Real, database-level uniqueness backs BOTH directions of architect
  spec §17's anti-duplicate-mapping requirement:

  * ``uq_xero_connections_entity_id`` — a PLAIN (non-partial) unique
    constraint on ``entity_id``: `entity_id` is always present on
    every row (unlike `tenant_id`), so this needs no partial
    qualifier — a company maps to AT MOST ONE `XeroConnection` row,
    period.
  * ``uq_xero_connections_tenant_id`` — a PARTIAL unique index on
    ``tenant_id`` (``WHERE tenant_id IS NOT NULL``) — the same
    technique ``NeedsYouItemRow.uq_needs_you_items_type_source_ref``
    already uses for the identical reason (a plain
    ``UniqueConstraint`` cannot itself be conditioned on ``IS NOT
    NULL``, and `tenant_id` is legitimately `NULL` for every
    connection still `PENDING`/cleared back to `PENDING` after a
    disconnect — see ``services/xero/connection.py``'s own module
    docstring for exactly when it is cleared).

* ``xero_accounts`` — the synced Chart-of-Accounts projection
  (``services.xero.account.XeroAccount``). ``uq_xero_accounts_tenant_account``
  is a real unique constraint on ``(tenant_id, account_id)`` — the
  durable external identity architect spec §4/§29 requires ("a real
  Postgres unique constraint, never merely application-checked").
  ``ix_xero_accounts_entity`` backs every `list_accounts(entity_id=...)`
  query (the cross-company-isolation guarantee's own index — see
  ``services/xero/account.py``'s module docstring).

* ``xero_sync_runs`` — the sync-attempt ledger
  (``services.xero.sync.XeroSyncRun``). No special uniqueness beyond
  its primary key — many runs legitimately exist for the same entity
  over time.

* ``xero_oauth_states`` — the ephemeral anti-CSRF/replay `state` token
  (``services.xero.oauth_state.OAuthState``). Primary-keyed directly on
  the (cryptographically random, already-unguessable) `state` string
  itself — there is no separate synthetic id, since the value's own
  unguessability IS the security property this table exists to check,
  and a caller always already has the exact `state` string in hand
  (from the OAuth callback's own query string) rather than any other
  identifier.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

#: Matches `persistence/postgres/models.py`'s own `_UUID` — a
#: native-UUID column typed to hand back plain Python `str`.
_UUID = UUID(as_uuid=False)


class XeroConnectionRow(Base):
    """Persisted form of
    `services.xero.connection.XeroConnection` (CD-6 Slice 2)."""

    __tablename__ = "xero_connections"
    __table_args__ = (
        UniqueConstraint("entity_id", name="uq_xero_connections_entity_id"),
        Index(
            "uq_xero_connections_tenant_id",
            "tenant_id",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
        Index("ix_xero_connections_status", "status"),
    )

    xero_connection_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    entity_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tenant_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempted_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class XeroAccountRow(Base):
    """Persisted form of `services.xero.account.XeroAccount` (CD-6
    Slice 2)."""

    __tablename__ = "xero_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_xero_accounts_tenant_account"),
        Index("ix_xero_accounts_entity", "entity_id"),
    )

    xero_account_row_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    entity_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    account_class: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tax_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    show_in_expense_claims: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    reporting_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reporting_code_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_updated_date_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sync_run_id: Mapped[str] = mapped_column(_UUID, nullable=False)


class XeroSyncRunRow(Base):
    """Persisted form of `services.xero.sync.XeroSyncRun` (CD-6 Slice
    2)."""

    __tablename__ = "xero_sync_runs"
    __table_args__ = (
        Index("ix_xero_sync_runs_entity", "entity_id"),
    )

    sync_run_id: Mapped[str] = mapped_column(_UUID, primary_key=True)
    entity_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    accounts_seen_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    accounts_created_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    accounts_updated_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class XeroOAuthStateRow(Base):
    """Persisted form of `services.xero.oauth_state.OAuthState` (CD-6
    Slice 2) — primary-keyed on the state token itself, see module
    docstring."""

    __tablename__ = "xero_oauth_states"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    entity_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
