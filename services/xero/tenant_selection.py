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

One-time consumption, mirrored from ``services.xero.oauth_state``
------------------------------------------------------------------------
:meth:`PendingTenantSelectionStore.mark_resolved` is the SAME
"authoritative, lock-guarded gate — re-validate under the lock, never
trust a pre-lock read alone" discipline
``services.xero.oauth_state.OAuthStateRepository.mark_consumed`` and
``ai.invocation._recover_if_stale`` both already establish elsewhere in
this codebase, applied here to the identical concurrency hazard: two
near-simultaneous resolution attempts for the same ``selection_id``
must not both succeed.
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
    selection_id: str
    entity_id: str
    xero_connection_id: str
    candidates: tuple[TenantCandidate, ...]
    access_token: str
    refresh_token: str
    token_expires_at: datetime
    created_at: datetime
    expires_at: datetime
    resolved_at: Optional[datetime] = None

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None

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
        """Read-only lookup — `None` if unknown/never created. Does NOT
        itself check expiry/resolution (callers needing the
        authoritative check use :meth:`mark_resolved`; a GET-only
        candidate-listing caller checks `is_resolved`/`expires_at`
        itself for an honest, non-mutating display)."""
        raise NotImplementedError

    @abc.abstractmethod
    def mark_resolved(self, selection_id: str, *, now: Optional[datetime] = None) -> PendingTenantSelection:
        """Atomically stamp `resolved_at` — the AUTHORITATIVE, race-safe
        consumption gate (mirrors
        `services.xero.oauth_state.OAuthStateRepository.mark_consumed`
        exactly, including WHY: a caller's own prior unlocked read is
        never trusted as the sole guarantee against two near-
        simultaneous resolution attempts).

        Raises:
            core.errors.TenantSelectionError: unknown, already
                resolved, or expired, as re-checked under the lock.
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
            resolved_at=None,
        )
        with self._lock:
            self._by_id[candidate.selection_id] = candidate
        return candidate

    def get(self, selection_id: str) -> Optional[PendingTenantSelection]:
        with self._lock:
            return self._by_id.get(selection_id)

    def mark_resolved(self, selection_id: str, *, now: Optional[datetime] = None) -> PendingTenantSelection:
        resolved_now = now if now is not None else utc_now()
        with self._lock:
            current = self._by_id.get(selection_id)
            if current is None:
                raise TenantSelectionError(f"no pending Xero tenant selection '{selection_id}'")
            # Re-validated INSIDE the lock — see this method's own
            # abstract docstring.
            if current.is_resolved:
                raise TenantSelectionError(
                    f"this Xero tenant selection was already resolved at {current.resolved_at.isoformat()}"
                )
            if resolved_now > current.expires_at:
                raise TenantSelectionError(
                    f"this Xero tenant selection expired at {current.expires_at.isoformat()} "
                    f"(now {resolved_now.isoformat()})"
                )
            updated = PendingTenantSelection(
                selection_id=current.selection_id,
                entity_id=current.entity_id,
                xero_connection_id=current.xero_connection_id,
                candidates=current.candidates,
                access_token=current.access_token,
                refresh_token=current.refresh_token,
                token_expires_at=current.token_expires_at,
                created_at=current.created_at,
                expires_at=current.expires_at,
                resolved_at=resolved_now,
            )
            self._by_id[selection_id] = updated
            return updated
