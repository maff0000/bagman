"""``MailboxOAuthState`` — BAGMAN's own server-side anti-CSRF/replay
`state` token for the Microsoft OAuth Authorization Code flow (CD-6
Slice 4), bound to a `mailbox_id` instead of Xero's `entity_id`.

Judgment call, documented (architect spec's own explicit "decide
explicitly and document: either generalize services.xero.oauth_state
... or create an analogous module ... document your reasoning. Either
is acceptable — pick the one that's less risky given Xero's OAuthState
is already used/tested/deployed"):

**DUPLICATED here, not generalised.** Reasoning:

1. Xero's `services.xero.oauth_state` is closed-GREEN, live-deployed,
   and load-bearing for Slice 2's own already-working OAuth flow. A
   generalisation would need EITHER changing `OAuthStateRepository`'s
   method signatures (from `entity_id` to a generic `subject_id`,
   touching every existing Xero call site and test) OR introducing a
   parallel "subject_type" discriminator column/field on the existing
   table — either is a real, live-findable-regression-risk change to
   working, deployed code, for the sake of a shared abstraction this
   module's own shape does not strictly need (a `state` token bound to
   ONE opaque foreign id is a tiny amount of logic — the entire module
   is well under 150 lines — so the duplication cost is genuinely low).
2. The exact race this module exists to close (`mark_consumed`'s "re-
   check under the lock, never trust a pre-lock read alone" discipline)
   is copied VERBATIM from the already-audited, already-tested Xero
   implementation — this is not "write OAuth security code twice and
   hope both copies are equally correct", it is "reuse the identical,
   already-proven algorithm, applied to a differently-shaped foreign
   key", which carries essentially none of generalisation's regression
   risk while still getting the exact same proven security properties.
3. A later delivery that finds a THIRD OAuth-state consumer (a future
   IMAP/Gmail adapter, say) is the natural trigger to revisit this
   as a real shared component — two data points do not yet justify an
   abstraction, and forcing one prematurely here would make the highest
   -risk part of this whole slice (anti-CSRF/replay security plumbing)
   the place that abstraction gets proven for the first time.

Every other design choice mirrors `services.xero.oauth_state` exactly —
see that module's own docstring for the full "why not a JSON-Schema
contract" / `secrets.token_urlsafe` / TTL reasoning, not repeated here.
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

#: Identical value to Xero's own `STATE_TTL_SECONDS` — see that
#: module's docstring for the reasoning (generous enough for a real
#: human OAuth consent round-trip, short enough to bound exposure).
STATE_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class MailboxOAuthState:
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


class MailboxOAuthStateRepository(abc.ABC):
    @abc.abstractmethod
    def create_state(self, *, mailbox_id: str) -> MailboxOAuthState:
        raise NotImplementedError

    @abc.abstractmethod
    def get_state(self, state: str) -> Optional[MailboxOAuthState]:
        raise NotImplementedError

    @abc.abstractmethod
    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> MailboxOAuthState:
        """Same lock-guarded, re-check-under-the-lock discipline as
        `services.xero.oauth_state.OAuthStateRepository.mark_consumed`
        — see that method's own docstring for the full TOCTOU reasoning
        this copies verbatim."""
        raise NotImplementedError


def consume_state(
    repository: MailboxOAuthStateRepository, state: str, *, now: Optional[datetime] = None
) -> MailboxOAuthState:
    """Same three-check-then-lock-guarded-consume algorithm as
    `services.xero.oauth_state.consume_state` — see that function's own
    docstring for the full reasoning, copied verbatim here (see this
    module's own top docstring for why it is copied rather than
    shared)."""
    resolved_now = now if now is not None else utc_now()

    existing = repository.get_state(state)
    if existing is None:
        raise OAuthStateError("Microsoft OAuth state value is unknown — never minted by this BAGMAN instance")
    if existing.is_consumed:
        raise OAuthStateError(
            f"Microsoft OAuth state value was already consumed at {existing.consumed_at.isoformat()} — "
            "refusing a replayed callback"
        )
    if resolved_now > existing.expires_at:
        raise OAuthStateError(
            f"Microsoft OAuth state value expired at {existing.expires_at.isoformat()} "
            f"(now {resolved_now.isoformat()}) — refusing a stale callback"
        )

    return repository.mark_consumed(state, now=resolved_now)


class InMemoryMailboxOAuthStateRepository(MailboxOAuthStateRepository):
    def __init__(self) -> None:
        self._by_state: dict[str, MailboxOAuthState] = {}
        self._lock = _threading.Lock()

    def create_state(self, *, mailbox_id: str) -> MailboxOAuthState:
        now = utc_now()
        candidate = MailboxOAuthState(
            state=generate_state_value(),
            mailbox_id=mailbox_id,
            created_at=now,
            expires_at=now + timedelta(seconds=STATE_TTL_SECONDS),
            consumed_at=None,
        )
        with self._lock:
            self._by_state[candidate.state] = candidate
        return candidate

    def get_state(self, state: str) -> Optional[MailboxOAuthState]:
        with self._lock:
            return self._by_state.get(state)

    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> MailboxOAuthState:
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            try:
                current = self._by_state[state]
            except KeyError:
                raise NotFoundError(f"no MailboxOAuthState with state '{state}'") from None
            if current.is_consumed:
                raise OAuthStateError(
                    f"Microsoft OAuth state value was already consumed at {current.consumed_at.isoformat()} — "
                    "refusing a replayed callback"
                )
            if resolved_now > current.expires_at:
                raise OAuthStateError(
                    f"Microsoft OAuth state value expired at {current.expires_at.isoformat()} "
                    f"(now {resolved_now.isoformat()}) — refusing a stale callback"
                )
            updated = MailboxOAuthState(
                state=current.state,
                mailbox_id=current.mailbox_id,
                created_at=current.created_at,
                expires_at=current.expires_at,
                consumed_at=resolved_now,
            )
            self._by_state[state] = updated
            return updated
