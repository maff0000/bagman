"""``PendingTenantSelection`` — the short-lived, server-side governed
broker for a real defect found during CD-6 Slice 2's live acceptance
run (architect finding, Infosecurs + NoustAI): after a real OAuth
consent, Xero's own ``GET /connections`` can return MORE than one
authorised organisation in a single grant, and array order carries no
identity meaning whatsoever. The original implementation took
``connections[0]`` unconditionally — with Infosecurs already connected
and NoustAI's own consent grant also authorising Infosecurs (the same
Xero login has access to both), this meant a NoustAI connect attempt
could have silently attempted to remap Infosecurs's own already-bound
tenant onto NoustAI. The existing tenant-uniqueness constraint
correctly REJECTED that outcome (never actually corrupted anything —
see ``services.xero.connection``'s own "Uniqueness" doctrine) but left
the operator with an opaque conflict error instead of a real,
governed resolution.

Three-way resolution this module implements (architect spec, this
finding)
------------------------------------------------------------------------
Given the full set of tenants Xero authorised in one grant, first
narrow to those NOT already mapped to a DIFFERENT BAGMAN entity (never
choose a tenant that is already someone else's):

* **exactly one** eligible candidate → no ambiguity at all; the caller
  (``app/api/routers/xero.py::oauth_callback``) completes the
  connection directly, without ever constructing a
  :class:`PendingTenantSelection` — this module is not even touched on
  the ordinary, common path.
* **zero** eligible candidates → an honest failure (nothing new was
  available to connect); no connection is mutated, no token is written
  anywhere.
* **more than one** eligible candidate → genuinely ambiguous; THIS is
  the case this module exists for. A :class:`PendingTenantSelection` is
  created, the operator is shown the real candidate tenant names (Xero
  already showed them the same names on its own consent screen moments
  earlier), and their choice is validated server-side against the
  EXACT candidate set — never trusted as an arbitrary browser-supplied
  tenant_id (architect requirement: "the browser must not be able to
  substitute an arbitrary tenant ID").

Where the freshly-exchanged tokens live while a human is choosing
------------------------------------------------------------------------
Held ONLY as a field on this in-process, in-memory record — never
Postgres, and never a file either:

* Not Postgres: ``services.xero.secrets``'s own established doctrine is
  "raw token bytes live ONLY as governed files under
  ``/opt/bagman/secrets/xero/``, never a Postgres column, since this
  codebase has no at-rest encryption primitive." Adding a second, new
  Postgres surface for the exact same class of secret — even
  "temporarily" — would violate that same doctrine, not merely
  duplicate it under a different name.
* Not a file: this bridge state is inherently short-lived and
  same-process — seconds to a few minutes, entirely AFTER the external,
  human-timescale OAuth round trip to Xero's own site has already
  completed. That is the key difference from ``OAuthState``, which
  MUST survive that external round trip (a real human may take minutes
  on Xero's own consent screen) and is therefore Postgres-backed in
  production. If the server process restarts mid-selection here, the
  pending selection is simply lost — never a correctness problem,
  since nothing has been written as ANY entity's usable
  connection/tokens yet — and the operator restarts the OAuth flow from
  the GUI's own already-supported "PENDING → Restart Xero connection"
  action.

Consume-and-remove, not mark-and-retain (architect correction, PID
§102.4)
------------------------------------------------------------------------
A first version of this module kept holding raw tokens on the record
even after resolution — it replaced the entry with an otherwise
identical ``resolved_at``-stamped copy, still sitting in the store, and
never purged unresolved-but-expired entries at all. That is a REAL
defect: it silently turned a "documented short-lived secret bridge"
into process-lifetime raw-token retention, which directly contradicts
this module's own reason for existing (see "Where the freshly-
exchanged tokens live" above — the whole premise is that this state is
short-lived).

:meth:`PendingTenantSelectionStore.consume` fixes this: it is an
ATOMIC consume-and-remove — under ONE lock it finds the record, rejects
it if unknown/expired, validates the operator's chosen tenant_id
against the exact frozen candidate set, and — ONLY on success — pops
the record out of the store entirely before returning it to the
caller for that one request's own local use. After `consume()`
returns, the store holds NO raw access/refresh token for that
selection, full stop — there is no separate "mark resolved" step that
would leave a token-bearing tombstone behind. A replay of the same
`selection_id` therefore fails not because some `resolved_at` flag was
checked, but because the record genuinely no longer exists — the
SIMPLEST possible one-time-use guarantee, and the audit event
(`XERO_CONNECTION_CONNECTED`/`XERO_CONNECTION_CONNECT_FAILED`/
`XERO_CONNECTION_CALLBACK_SUPERSEDED`, all already recorded by
`app/api/routers/xero.py::_complete_with_tenant`) is the durable
evidence of what happened — this module itself keeps none.

If completing the connection AFTER a successful `consume()` fails
unexpectedly (a tenant conflict, a superseded connection — see
`_complete_with_tenant`'s own docstring), the tokens simply fall out of
scope with the request and are never retained for a retry: the
operator restarts the OAuth flow from the GUI's own already-supported
"PENDING → Restart Xero connection" action. Correctness/security is
more important than retry convenience here — an explicit, deliberate
trade-off, not an oversight.

Every store method also opportunistically purges any entry whose
`expires_at` has passed (architect requirement: "purge expired records
opportunistically on create/get/consume, or otherwise ensure expired
selections cannot accumulate indefinitely") — so an abandoned,
never-resolved selection is never retained past its own TTL either,
closing the second half of the same "process-lifetime retention"
concern.
"""
from __future__ import annotations

import abc
import secrets as _stdlib_secrets
import threading as _threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from core.errors import TenantSelectionError
from core.timestamps import utc_now

#: Mirrors `services.xero.oauth_state.STATE_TTL_SECONDS`'s own "short
#: but human-scale" rationale — enough time for an operator to read a
#: short list of organisation names and click one.
PENDING_SELECTION_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class TenantCandidate:
    tenant_id: str
    tenant_name: Optional[str]


@dataclass(frozen=True)
class PendingTenantSelection:
    """No `resolved_at`/`is_resolved` field, deliberately: "resolved"
    now means "no longer present in the store at all" (see module
    docstring's "Consume-and-remove, not mark-and-retain" section) —
    there is no in-between state where a resolved-but-still-token-
    bearing record exists for anything to check."""

    selection_id: str
    entity_id: str
    xero_connection_id: str
    candidates: tuple[TenantCandidate, ...]
    access_token: str
    refresh_token: str
    token_expires_at: datetime
    created_at: datetime
    expires_at: datetime

    def candidate_tenant_ids(self) -> frozenset[str]:
        return frozenset(c.tenant_id for c in self.candidates)


def generate_selection_id() -> str:
    """A fresh, cryptographically random, URL-safe selection token —
    functionally the same bearer-correlation shape as
    `services.xero.oauth_state.generate_state_value`, for the same
    reason: this codebase has no user-authentication layer at all
    (every actor is self-asserted, repo-wide), so possession of this
    unguessable value is already the only thing standing between an
    arbitrary caller and driving this resolution — exactly the same
    property `state` already has, not a new or weaker one."""
    return _stdlib_secrets.token_urlsafe(32)


class PendingTenantSelectionStore(abc.ABC):
    """Repository-shaped abstraction (this codebase's established
    convention) even though, unlike every other repository in this
    delivery, there is only ever ONE implementation — see the module
    docstring for why this is deliberately never Postgres-backed."""

    @abc.abstractmethod
    def create(
        self,
        *,
        entity_id: str,
        xero_connection_id: str,
        candidates: tuple[TenantCandidate, ...],
        access_token: str,
        refresh_token: str,
        token_expires_at: datetime,
    ) -> PendingTenantSelection:
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, selection_id: str) -> Optional[PendingTenantSelection]:
        """Read-only lookup for the picker's own candidate-listing
        call — never mutates, never removes anything itself (beyond
        the same opportunistic expired-entry purge every method
        performs). Returns `None` for BOTH an unknown selection_id and
        an expired one — indistinguishable to a read-only caller, and
        deliberately so (see :meth:`consume` for why "unknown" and
        "expired" are not distinguished anywhere in this module)."""
        raise NotImplementedError

    @abc.abstractmethod
    def consume(
        self, selection_id: str, selected_tenant_id: str, *, now: Optional[datetime] = None
    ) -> PendingTenantSelection:
        """The ONE authoritative operation across this selection's
        entire lifecycle — atomic consume-and-remove, under a single
        lock:

        1. find the record;
        2. reject unknown (never existed, already consumed by an
           earlier call, or purged for having expired — all
           indistinguishable, deliberately: see module docstring);
        3. reject expired (purged before the lookup, so an expired
           record is simply absent by this point — folded into the
           same "unknown" rejection above, not a separate check);
        4. validate `selected_tenant_id` against the record's own
           frozen (immutable since :meth:`create`) candidate set;
        5. remove the record from the store;
        6. return the removed record to the caller for THIS request's
           own local, one-time use.

        After this returns successfully, the store contains NO raw
        access/refresh token for `selection_id` — there is no
        intermediate "resolved but still retained" state. A second
        call with the same `selection_id` (a genuine replay) fails at
        step 2, identically to a `selection_id` that never existed.

        Raises:
            core.errors.TenantSelectionError: unknown/expired/already-
                consumed `selection_id`, or `selected_tenant_id` not in
                the authorised candidate set.
        """
        raise NotImplementedError


class InMemoryPendingTenantSelectionStore(PendingTenantSelectionStore):
    """The ONLY production implementation (see module docstring) — real
    `threading.Lock`, since this store's real caller is an HTTP handler
    that may run across genuine OS threads (this delivery's own
    `run_in_threadpool` precedent, the CD-6 reliability delta)."""

    def __init__(self) -> None:
        self._by_id: dict[str, PendingTenantSelection] = {}
        self._lock = _threading.Lock()

    def _purge_expired_locked(self, now: datetime) -> None:
        """Must be called only while holding `self._lock`. Removes
        every entry whose TTL has passed — architect requirement:
        "ensure expired selections cannot accumulate indefinitely." An
        abandoned, never-resolved selection is therefore never retained
        past its own TTL, closing the same "process-lifetime
        retention" concern :meth:`consume`'s own removal closes for the
        resolved case."""
        expired_ids = [sid for sid, record in self._by_id.items() if now > record.expires_at]
        for sid in expired_ids:
            del self._by_id[sid]

    def create(
        self,
        *,
        entity_id: str,
        xero_connection_id: str,
        candidates: tuple[TenantCandidate, ...],
        access_token: str,
        refresh_token: str,
        token_expires_at: datetime,
    ) -> PendingTenantSelection:
        now = utc_now()
        candidate = PendingTenantSelection(
            selection_id=generate_selection_id(),
            entity_id=entity_id,
            xero_connection_id=xero_connection_id,
            candidates=candidates,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
            created_at=now,
            expires_at=now + timedelta(seconds=PENDING_SELECTION_TTL_SECONDS),
        )
        with self._lock:
            self._purge_expired_locked(now)
            self._by_id[candidate.selection_id] = candidate
        return candidate

    def get(self, selection_id: str) -> Optional[PendingTenantSelection]:
        now = utc_now()
        with self._lock:
            self._purge_expired_locked(now)
            return self._by_id.get(selection_id)

    def consume(
        self, selection_id: str, selected_tenant_id: str, *, now: Optional[datetime] = None
    ) -> PendingTenantSelection:
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            # Purging BEFORE the lookup, with the SAME `resolved_now`
            # this call uses throughout, means: if `current` is found
            # below at all, it is provably unexpired relative to
            # `resolved_now` — no separate expiry re-check is needed
            # (and none is performed) after this point.
            self._purge_expired_locked(resolved_now)
            current = self._by_id.get(selection_id)
            if current is None:
                raise TenantSelectionError(
                    f"no pending Xero tenant selection '{selection_id}' "
                    "(unknown, already consumed, or expired)"
                )
            if selected_tenant_id not in current.candidate_tenant_ids():
                raise TenantSelectionError("the selected Xero organisation was not part of this authorisation")
            # Atomic consume-and-remove — see this method's own
            # abstract docstring and the module docstring's
            # "Consume-and-remove, not mark-and-retain" section for why
            # this is a `del`, never a "mark resolved and retain".
            del self._by_id[selection_id]
            return current
