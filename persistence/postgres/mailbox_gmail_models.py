"""SQLAlchemy table definition for the Gmail-adapter OAuth CSRF-state
plumbing (CD-6 GUI-operations-foundation follow-on WO — Gmail OAuth
state persistence correction).

Mirrors ``persistence/postgres/mailbox_microsoft_models.py``'s own
``MailboxMicrosoftOAuthStateRow`` shape and file-splitting convention
(models in one file, repository logic in the sibling
``mailbox_gmail_repository.py``) exactly — this pair did not exist
before this WO because the prior Gmail-adapter-foundation WO used only
`services.mailbox.gmail.oauth_state.InMemoryGmailOAuthStateRepository`,
even in production (the defect this WO exists to close — see that
module's own docstring history / `app/api/composition.py`'s
`_build_production` for the prior state).

This table stores ONLY the anti-CSRF/replay `state` token record
(`state`, `mailbox_id`, `created_at`, `expires_at`, `consumed_at`) —
never any Google OAuth access token, refresh token, client secret, or
authorization code. Those stay exactly where they already are:
`services/mailbox/gmail/secrets.py`'s file-based token storage, wholly
untouched by this WO.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from persistence.postgres.models import Base

_UUID = UUID(as_uuid=False)


class MailboxGmailOAuthStateRow(Base):
    """Persisted form of
    `services.mailbox.gmail.oauth_state.GmailOAuthState` —
    primary-keyed directly on the state token itself, mirrors
    `persistence/postgres/mailbox_microsoft_models.py::MailboxMicrosoftOAuthStateRow`
    exactly (see that class's own docstring, and
    `persistence/postgres/xero_models.py::XeroOAuthStateRow`'s, for the
    "the value's own identity IS the lookup key" reasoning — a plain
    `session.get()` by primary key can never ambiguously return more
    than one record)."""

    __tablename__ = "mailbox_gmail_oauth_states"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(_UUID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
