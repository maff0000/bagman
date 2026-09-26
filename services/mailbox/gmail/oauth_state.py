"""``GmailOAuthState`` — BAGMAN's own server-side anti-CSRF/replay
`state` token for the Gmail OAuth2 Authorization Code flow (CD-6
GUI-operations-foundation follow-on WO — third mailbox provider), bound
to a `mailbox_id` exactly like
``services.mailbox.microsoft.oauth_state.MailboxOAuthState``.

Judgment call, documented (mirrors the reasoning
``services.mailbox.microsoft.oauth_state``'s own module docstring
already applies to Xero vs. Microsoft, and that its own docstring names
as the natural trigger for THIS module: "a future IMAP/Gmail adapter,
say" — this is that trigger arriving):

**DUPLICATED here, not generalised into a third consumer of a shared
abstraction.** Reasoning:

1. Microsoft's own `services.mailbox.microsoft.oauth_state` is
   closed-GREEN, live-deployed-shaped, load-bearing plumbing. A
   generalisation would need changing its own method signatures (from
   `mailbox_id` to something more generic) or introducing a
   "provider"/"subject_type" discriminator — either is a real,
   live-findable-regression-risk change to already-proven security
   plumbing, for the sake of a shared abstraction this module's own
   shape does not strictly need (this whole module stays well under 150
   lines, same as Microsoft's own — the duplication cost is genuinely
   low).
2. The exact race this module exists to close (`mark_consumed`'s "re-
   check under the lock, never trust a pre-lock read alone" discipline)
   is copied VERBATIM from the already-proven Microsoft implementation
   — this is not "write OAuth security code a third time and hope all
   three copies are equally correct", it is "reuse the identical,
   already-proven algorithm a third time, applied to the same
   `mailbox_id`-shaped foreign key Microsoft's own module already uses",
   which carries essentially none of generalisation's regression risk
   while still getting the exact same proven security properties.
3. TWO independent Gmail mailboxes (`mgs241171@gmail.com` and
   `matt.george.scott@gmail.com`) can each independently be mid-flow at
   once — this module's own `mailbox_id` binding is exactly what lets
   `app/api/routers/mailboxes_gmail.py`'s callback route the resulting
   tokens to the RIGHT mailbox (never "the one Gmail mailbox", the
   assumption Microsoft's own single-mailbox-in-production precedent
   could implicitly get away with) — see that router's own docstring.

Every other design choice mirrors
`services.mailbox.microsoft.oauth_state` exactly — see that module's own
docstring (and, transitively, `services.xero.oauth_state`'s) for the
full "why not a JSON-Schema contract" / `secrets.token_urlsafe` / TTL
reasoning, not repeated here.
"""
from __future__ import annotations

import abc
import secrets as _stdlib_secrets
import threading as _threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from core.errors import NotFoundError, OAuthStateError
from core.timestamps import utc_now

#: Identical value to Microsoft's/Xero's own `STATE_TTL_SECONDS` — see
#: those modules' own docstrings for the reasoning.
STATE_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class GmailOAuthState:
    state: str
    mailbox_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: Optional[datetime] = None

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None


def generate_state_value() -> str:
    return _stdlib_secrets.token_urlsafe(32)


class GmailOAuthStateRepository(abc.ABC):
    @abc.abstractmethod
    def create_state(self, *, mailbox_id: str) -> GmailOAuthState:
        raise NotImplementedError

    @abc.abstractmethod
    def get_state(self, state: str) -> Optional[GmailOAuthState]:
        raise NotImplementedError

    @abc.abstractmethod
    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> GmailOAuthState:
        """Same lock-guarded, re-check-under-the-lock discipline as
        `services.mailbox.microsoft.oauth_state
        .MailboxOAuthStateRepository.mark_consumed` — see that method's
        own docstring for the full TOCTOU reasoning this copies
        verbatim."""
        raise NotImplementedError


def consume_state(repository: GmailOAuthStateRepository, state: str, *, now: Optional[datetime] = None) -> GmailOAuthState:
    """Same three-check-then-lock-guarded-consume algorithm as
    `services.mailbox.microsoft.oauth_state.consume_state` — see that
    function's own docstring for the full reasoning, copied verbatim
    here (see this module's own top docstring for why it is copied
    rather than shared)."""
    resolved_now = now if now is not None else utc_now()

    existing = repository.get_state(state)
    if existing is None:
        raise OAuthStateError("Gmail OAuth state value is unknown — never minted by this BAGMAN instance")
    if existing.is_consumed:
        raise OAuthStateError(
            f"Gmail OAuth state value was already consumed at {existing.consumed_at.isoformat()} — "
            "refusing a replayed callback"
        )
    if resolved_now > existing.expires_at:
        raise OAuthStateError(
            f"Gmail OAuth state value expired at {existing.expires_at.isoformat()} "
            f"(now {resolved_now.isoformat()}) — refusing a stale callback"
        )

    return repository.mark_consumed(state, now=resolved_now)


class InMemoryGmailOAuthStateRepository(GmailOAuthStateRepository):
    def __init__(self) -> None:
        self._by_state: dict[str, GmailOAuthState] = {}
        self._lock = _threading.Lock()

    def create_state(self, *, mailbox_id: str) -> GmailOAuthState:
        now = utc_now()
        candidate = GmailOAuthState(
            state=generate_state_value(),
            mailbox_id=mailbox_id,
            created_at=now,
            expires_at=now + timedelta(seconds=STATE_TTL_SECONDS),
            consumed_at=None,
        )
        with self._lock:
            self._by_state[candidate.state] = candidate
        return candidate

    def get_state(self, state: str) -> Optional[GmailOAuthState]:
        with self._lock:
            return self._by_state.get(state)

    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> GmailOAuthState:
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            try:
                current = self._by_state[state]
            except KeyError:
                raise NotFoundError(f"no GmailOAuthState with state '{state}'") from None
            if current.is_consumed:
                raise OAuthStateError(
                    f"Gmail OAuth state value was already consumed at {current.consumed_at.isoformat()} — "
                    "refusing a replayed callback"
                )
            if resolved_now > current.expires_at:
                raise OAuthStateError(
                    f"Gmail OAuth state value expired at {current.expires_at.isoformat()} "
                    f"(now {resolved_now.isoformat()}) — refusing a stale callback"
                )
            updated = GmailOAuthState(
                state=current.state,
                mailbox_id=current.mailbox_id,
                created_at=current.created_at,
                expires_at=current.expires_at,
                consumed_at=resolved_now,
            )
            self._by_state[state] = updated
            return updated
