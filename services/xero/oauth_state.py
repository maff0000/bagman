"""``OAuthState`` — BAGMAN's own server-side anti-CSRF/replay `state`
token for the Xero OAuth Authorization Code flow (CD-6 Slice 2,
architect spec §3: "generates a real, cryptographically random `state`
value, persists it server-side (tied to the `entity_id` and a short
expiry — a real anti-CSRF/replay-protection mechanism, not merely
appended to the redirect and trusted blindly on return"; PID §102.1's
own tenant-substitution-prevention doctrine relies on this holding).

Not a JSON-Schema-contract-backed domain object like `XeroConnection`/
`XeroAccount`/`XeroSyncRun` (a deliberate scope judgment call, recorded
here since it IS a decision): this is ephemeral security plumbing —
created, consumed exactly once, and discarded within one short-lived
OAuth round trip — never surfaced through a GET endpoint, never
displayed in the GUI, never referenced by another canonical object.
Every other domain object in this codebase that gets a full
`contracts/*.schema.json` treatment is a durable, API-visible business
record; this is closer in spirit to `IntakeRecord.idempotency_key`
(a plain string field on an existing contract) than to a
first-class contract of its own. Validation here is enforced directly
in Python (a real, small, closed set of checks — see
:func:`consume_state`), not via `jsonschema`.

The value itself (``state``) is generated with
:func:`secrets.token_urlsafe` (32 bytes of OS randomness, ~43
URL-safe characters) — cryptographically unguessable, per architect
spec §3's own "real, cryptographically random" requirement; never a
sequential id, a UUID (which is not designed to be unguessable), or a
value derived from any predictable input.
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

#: How long a `state` value remains valid after being minted (architect
#: spec §3's own "a short expiry"). 10 minutes is generous enough for a
#: real human to complete the Xero consent screen (including the
#: SSH-tunnel hop PID §102.1's own topology resolution requires —
#: `ssh -L 8200:localhost:8200 ...`) while still being "short" in the
#: sense that actually matters here: an intercepted/leaked `state`
#: value is worthless within a bounded, human-scale window, not
#: indefinitely.
STATE_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class OAuthState:
    state: str
    entity_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: Optional[datetime] = None

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None


def generate_state_value() -> str:
    """A fresh, cryptographically random, URL-safe `state` token."""
    return _stdlib_secrets.token_urlsafe(32)


class OAuthStateRepository(abc.ABC):
    """Repository abstraction for OAuthState."""

    @abc.abstractmethod
    def create_state(self, *, entity_id: str) -> OAuthState:
        """Mint and persist a fresh `state` value tied to `entity_id`,
        expiring :data:`STATE_TTL_SECONDS` from now."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_state(self, state: str) -> Optional[OAuthState]:
        """Read-only lookup — `None` if `state` was never minted (never
        `NotFoundError`; callers distinguish 'unknown' from 'expired'/
        'already consumed' themselves via :func:`consume_state`)."""
        raise NotImplementedError

    @abc.abstractmethod
    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> OAuthState:
        """Atomically stamp `consumed_at` on `state` — this is the
        AUTHORITATIVE, race-safe consumption gate, not merely a write
        the caller has already proven safe. :func:`consume_state`
        (below) performs its own checks first as a fast, informative
        pre-check (so a genuinely-unknown/expired/already-consumed
        state fails with a clear reason before any locking work), but
        a real, live-findable TOCTOU race exists if this method trusts
        that pre-check alone: two near-simultaneous callback requests
        presenting the SAME `state` value could both read
        `consumed_at IS NULL` before either has acquired a lock, then
        both proceed to call this method. Implementations MUST
        therefore re-validate under their own concurrency-safe read
        (e.g. `SELECT ... FOR UPDATE`) and raise
        `core.errors.OAuthStateError` themselves if the row is already
        consumed or has expired by the time the lock is held — never
        silently re-stamp an already-consumed row, and never rely on
        the caller's earlier, unlocked check as the sole guarantee.
        This mirrors the exact "re-check under the lock, never trust a
        pre-lock read alone" discipline this same delivery's own
        `ai.invocation._recover_if_stale` already establishes for an
        analogous concurrency hazard.

        Raises:
            core.errors.NotFoundError: `state` was never minted.
            core.errors.OAuthStateError: `state` is already consumed,
                or has expired, as re-checked under the lock.
        """
        raise NotImplementedError


def consume_state(repository: OAuthStateRepository, state: str, *, now: Optional[datetime] = None) -> OAuthState:
    """The one real validation entrypoint every OAuth callback handler
    must call (architect spec §3: "validates `state` server-side
    (reject/audit a mismatched or expired state — a real security
    control, test it adversarially)").

    Checks, in order, each raising :class:`core.errors.OAuthStateError`
    on failure (the caller — `app/api/routers/xero.py` — is responsible
    for auditing every rejection, per architect spec §22):

    1. `state` is a known value at all (never minted, or a value an
       attacker simply guessed/fabricated).
    2. It has not already expired (`STATE_TTL_SECONDS` since creation).
    3. It has not already been consumed (a REPLAY — the same `state`
       value presented to the callback a second time; Xero itself
       should never do this for one real consent flow, so a second
       presentation is either a genuine attack or a broken client, and
       is rejected identically either way).

    These three checks run here FIRST as a fast, unlocked pre-check —
    purely so an obviously-invalid `state` (unknown/expired/consumed
    well outside any race window) fails with a clear, specific reason
    without paying for a row lock. They are NOT the sole guarantee: a
    genuine concurrent replay (two near-simultaneous callbacks
    presenting the same `state`) could pass this unlocked pre-check
    twice before either caller reaches :meth:`OAuthStateRepository
    .mark_consumed`. That method is therefore the real, authoritative,
    lock-guarded gate — it re-validates under its own concurrency-safe
    read and raises `OAuthStateError` itself if the row is already
    consumed/expired by the time it holds the lock (see its own
    docstring). This function's own three checks above are useful,
    real, but deliberately NOT trusted as sufficient on their own — the
    same "re-check under the lock, never trust a pre-lock read alone"
    discipline this delivery's own `ai.invocation._recover_if_stale`
    already establishes.

    On success, marks the row consumed (so a genuine second presentation
    of the SAME value — even a legitimate double page-load — is treated
    as a replay, never silently accepted twice) and returns the
    now-consumed :class:`OAuthState`, whose `entity_id` is the ONLY
    thing the OAuth callback handler trusts to know which BAGMAN entity
    this flow was for — never a `entity_id` read from the callback's own
    query string (which architect spec §3 does not even define as a
    parameter, precisely to remove that temptation)."""
    resolved_now = now if now is not None else utc_now()

    existing = repository.get_state(state)
    if existing is None:
        raise OAuthStateError("OAuth state value is unknown — never minted by this BAGMAN instance")
    if existing.is_consumed:
        raise OAuthStateError(
            f"OAuth state value was already consumed at {existing.consumed_at.isoformat()} — "
            "refusing a replayed callback"
        )
    if resolved_now > existing.expires_at:
        raise OAuthStateError(
            f"OAuth state value expired at {existing.expires_at.isoformat()} "
            f"(now {resolved_now.isoformat()}) — refusing a stale callback"
        )

    # The authoritative, lock-guarded gate — see this function's own
    # docstring and mark_consumed's above for why the checks just
    # performed are a fast pre-check, not the actual race-safety
    # guarantee.
    return repository.mark_consumed(state, now=resolved_now)


class InMemoryOAuthStateRepository(OAuthStateRepository):
    """Narrow in-memory reference implementation (PID §21 Option A).

    Guarded by a real ``threading.Lock`` around ``mark_consumed`` —
    unlike most other `InMemory*Repository` implementations in this
    codebase (which document themselves as single-process/GIL-
    serialised with no real concurrency to defend against), this one's
    single real caller is an HTTP handler this delivery's own
    `run_in_threadpool` precedent (the CD-6 reliability delta) may run
    across genuine OS threads even within one process — the exact
    concurrency hazard :meth:`mark_consumed`'s own abstract docstring
    requires implementations to close, not merely the Postgres one.
    """

    def __init__(self) -> None:
        self._by_state: dict[str, OAuthState] = {}
        self._lock = _threading.Lock()

    def create_state(self, *, entity_id: str) -> OAuthState:
        now = utc_now()
        candidate = OAuthState(
            state=generate_state_value(),
            entity_id=entity_id,
            created_at=now,
            expires_at=now + timedelta(seconds=STATE_TTL_SECONDS),
            consumed_at=None,
        )
        with self._lock:
            self._by_state[candidate.state] = candidate
        return candidate

    def get_state(self, state: str) -> Optional[OAuthState]:
        with self._lock:
            return self._by_state.get(state)

    def mark_consumed(self, state: str, *, now: Optional[datetime] = None) -> OAuthState:
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            try:
                current = self._by_state[state]
            except KeyError:
                raise NotFoundError(f"no OAuthState with state '{state}'") from None
            # Re-validated INSIDE the lock — see this method's own
            # abstract docstring for why the caller's earlier, unlocked
            # check in consume_state() is not sufficient on its own.
            if current.is_consumed:
                raise OAuthStateError(
                    f"OAuth state value was already consumed at {current.consumed_at.isoformat()} — "
                    "refusing a replayed callback"
                )
            if resolved_now > current.expires_at:
                raise OAuthStateError(
                    f"OAuth state value expired at {current.expires_at.isoformat()} "
                    f"(now {resolved_now.isoformat()}) — refusing a stale callback"
                )
            updated = OAuthState(
                state=current.state,
                entity_id=current.entity_id,
                created_at=current.created_at,
                expires_at=current.expires_at,
                consumed_at=resolved_now,
            )
            self._by_state[state] = updated
            return updated
