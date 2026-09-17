"""``XeroConnection`` — the canonical, durable mapping of one BAGMAN
``GovernedEntity`` to one Xero organisation/tenant (CD-6 Slice 2, PID
§98.4, architect spec §1-3/§17/§22).

Where the real OAuth tokens live (read before changing anything here)
------------------------------------------------------------------------
This domain object NEVER carries a raw access/refresh token value —
only ``token_expires_at`` (metadata: "when does the current token
expire", used to decide when to pre-emptively refresh). The actual
token bytes are stored via ``services.xero.secrets`` as per-connection
files under ``/opt/bagman/secrets/xero/tokens/<entity_id>/`` (0700/0600)
— see that module's own docstring for the full "why a file, not an
encrypted Postgres column" reasoning. This split (durable, queryable,
non-secret metadata in Postgres; the one genuinely secret value on a
governed filesystem path, exactly like every other BAGMAN secret) is
the architect spec §3's own "governed secret storage discipline" read
literally.

State machine (see :data:`ALLOWED_TRANSITIONS` — architect spec §2's
own "a real, closed, documented state machine... design and document
the transitions the same rigorous way ``ai/invocation.py``'s
``ALLOWED_TRANSITIONS`` is documented")
------------------------------------------------------------------------
::

    PENDING      -> {CONNECTED, ERROR, DISCONNECTED}
    CONNECTED    -> {ERROR, REVOKED, DISCONNECTED, PENDING}
    ERROR        -> {CONNECTED, DISCONNECTED, PENDING}
    DISCONNECTED -> {PENDING}
    REVOKED      -> {PENDING}

* ``PENDING`` — an OAuth flow was initiated for this entity (a real
  server-side ``state`` token exists, see ``services.xero.oauth_state``)
  but has not yet completed. No ``tenant_id`` is known yet.
* ``CONNECTED`` — the OAuth callback resolved a real tenant via
  ``GET /connections`` and this connection is usable.
* ``ERROR`` — a PERSISTENT fault, not a single transient sync failure:
  today, exactly one thing puts a connection here —
  :func:`fail_refresh` (a token-refresh attempt itself failed, meaning
  BAGMAN can no longer even attempt an authenticated call without a
  fresh manual OAuth flow). An ordinary transient sync failure (rate
  limiting, a one-off provider 5xx, a malformed response) does NOT
  change this field at all — it is recorded on the ``XeroSyncRun`` row
  alone (see ``services.xero.sync``); the whole point of separating
  "this one sync attempt failed" from "this connection itself is
  broken" is that a transient blip must never make a healthy,
  reconnectable-without-operator-action connection look like it needs
  re-authorisation.
* ``REVOKED`` — Xero itself rejected access (a live 401/403 on an
  authenticated call, via :func:`fail_auth_revoked`) — distinct from
  ``ERROR`` because the remedy is different: a refresh failure MIGHT
  self-heal on the next scheduled refresh attempt; a genuine 401/403
  never will without a brand-new operator-driven OAuth consent.
* ``DISCONNECTED`` — an operator explicitly disconnected this
  connection (architect spec §3's `POST /internal/xero/{entity_id}
  /disconnect`).
* Every non-``PENDING`` state can re-enter ``PENDING`` (an operator
  re-running the OAuth connect flow, whether to fix scopes, recover
  from ``ERROR``/``REVOKED``, or reconnect after an explicit
  disconnect) — this is the ONE deliberate departure from a strict
  linear/one-way state machine, and is safe precisely because
  :func:`begin_connect` always re-resolves a fresh ``tenant_id`` from a
  fresh, real OAuth exchange; it never simply flips the label back.

``tenant_id``/``tenant_name``/``token_expires_at`` are cleared to
``None`` on every transition INTO ``PENDING``, ``DISCONNECTED``, or
``REVOKED`` — a stale tenant identity must never survive into a state
that no longer has a live, authorised token behind it (this is also
what releases a disconnected connection's ``tenant_id`` back for reuse
under the database's own partial-unique-tenant-id constraint — see
``persistence/postgres/xero_models.py``).

Uniqueness (architect spec §2/§17 — "protect against duplicate/wrong-
company mapping... a company maps to at most one Xero organisation...
must not accidentally be mapped to multiple BAGMAN companies")
------------------------------------------------------------------------
Enforced at TWO independent points, deliberately:

1. **One connection row per ``entity_id``, always.** There is exactly
   one ``XeroConnection`` per entity for its whole lifetime — re-running
   OAuth reuses/transitions the SAME row (see :func:`begin_connect`)
   rather than ever creating a second row for an entity that already
   has one. The real backstop is a plain (non-partial) unique
   constraint on ``entity_id`` at the database layer.
2. **At most one entity per non-cleared ``tenant_id``.** Checked here,
   in :func:`complete_connect`, against every OTHER connection's
   current ``tenant_id`` before accepting a callback — raising
   :class:`core.errors.ConflictError` if the tenant Xero's own
   ``GET /connections`` resolved is already mapped to a DIFFERENT
   entity. The real backstop is a partial unique index on
   ``tenant_id`` (``WHERE tenant_id IS NOT NULL``) at the database
   layer — see ``persistence/postgres/xero_models.py``, mirroring
   exactly the same "application check first, database constraint is
   the real proof under a race" discipline
   ``persistence.postgres.needs_you_repository`` already documents.

For THIS slice, tenant-to-entity is treated as strictly 1:1 in both
directions (architect spec §17's own explicit instruction) — a future
delivery that wants to support one Xero organisation genuinely serving
multiple BAGMAN entities needs its own deliberate migration/ruling, not
a silent relaxation here.
"""
from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from core import identity
from core.contract_validation import validate_against_contract
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now

_SCHEMA = "xero/bagman.xero_connection.v1.schema.json"

SCHEMA_VERSION = "bagman.xero_connection.v1"

#: CD-6 Slice 2's own closed, single-value provider set — see the
#: contract's own `provider` field description for why this is a
#: Python-level (not JSON-Schema-level) closed set: a literal external
#: provider brand name in a schema structural constraint is exactly
#: what `tests/integration/test_architecture_boundaries.py
#: ::test_no_schema_file_hardcodes_a_provider_name_inside_a_structural_constraint`
#: forbids repo-wide. Mirrors `ai.invocation.PROVIDERS`'s role, one
#: layer down (application code, not the wire contract).
PROVIDER_XERO = "XERO"
PROVIDERS = frozenset({PROVIDER_XERO})

STATUSES = frozenset({"PENDING", "CONNECTED", "DISCONNECTED", "REVOKED", "ERROR"})
TERMINAL_LIKE_STATUSES = frozenset({"DISCONNECTED", "REVOKED"})  # not truly terminal — see module docstring

#: The single source of truth for valid XeroConnection state
#: transitions — see module docstring's "State machine" section for
#: the full rationale.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"CONNECTED", "ERROR", "DISCONNECTED"}),
    "CONNECTED": frozenset({"ERROR", "REVOKED", "DISCONNECTED", "PENDING"}),
    "ERROR": frozenset({"CONNECTED", "DISCONNECTED", "PENDING"}),
    "DISCONNECTED": frozenset({"PENDING"}),
    "REVOKED": frozenset({"PENDING"}),
}

#: Statuses that clear tenant identity/token metadata on entry — see
#: module docstring.
_CLEARS_TENANT_ON_ENTRY = frozenset({"PENDING", "DISCONNECTED", "REVOKED"})


@dataclass(frozen=True)
class XeroConnection:
    """One BAGMAN entity's mapping to (at most) one Xero organisation.
    Immutable once constructed — every transition below produces a NEW
    snapshot via :func:`transition`, never an in-place mutation."""

    xero_connection_id: str
    entity_id: str
    provider: str
    tenant_id: Optional[str]
    tenant_name: Optional[str]
    status: str
    connected_at: Optional[datetime]
    last_successful_sync_at: Optional[datetime]
    last_attempted_sync_at: Optional[datetime]
    token_expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    error_detail: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/xero/bagman.xero_connection.v1.schema.json``.
        Never includes a token value — see module docstring."""
        return {
            "xero_connection_id": self.xero_connection_id,
            "entity_id": self.entity_id,
            "provider": self.provider,
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "status": self.status,
            "connected_at": to_contract_string(self.connected_at) if self.connected_at is not None else None,
            "last_successful_sync_at": (
                to_contract_string(self.last_successful_sync_at)
                if self.last_successful_sync_at is not None
                else None
            ),
            "last_attempted_sync_at": (
                to_contract_string(self.last_attempted_sync_at)
                if self.last_attempted_sync_at is not None
                else None
            ),
            "token_expires_at": (
                to_contract_string(self.token_expires_at) if self.token_expires_at is not None else None
            ),
            "created_at": to_contract_string(self.created_at),
            "updated_at": to_contract_string(self.updated_at),
            "error_detail": self.error_detail,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }


def transition(connection: XeroConnection, new_status: str, **field_updates: Any) -> XeroConnection:
    """Move ``connection`` to ``new_status``, enforcing
    :data:`ALLOWED_TRANSITIONS`. Stamps ``updated_at`` automatically;
    stamps ``connected_at`` automatically the moment ``new_status`` is
    first ``CONNECTED`` (never overwritten by a later re-entry into
    ``CONNECTED`` via ``ERROR -> CONNECTED`` recovery — the ORIGINAL
    connection moment is preserved, matching `AIInvocation`'s own
    "identity fields established at creation are not ordinary
    field_updates targets" spirit, applied here to "first connected"
    rather than full immutability); clears ``tenant_id``/``tenant_name``/
    ``token_expires_at`` automatically on entry to any status in
    :data:`_CLEARS_TENANT_ON_ENTRY` (see module docstring) unless the
    caller explicitly re-supplies them in ``field_updates`` (used by
    :func:`begin_connect` re-entering ``PENDING`` — nothing needs to
    re-supply them there; this default-clear is exactly what it wants).

    Raises:
        core.errors.InvalidStateTransitionError: if ``new_status`` is
            not a valid transition from ``connection.status``.
        core.errors.ValidationError: if the resulting connection fails
            contract validation.
    """
    allowed = ALLOWED_TRANSITIONS.get(connection.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            f"XeroConnection '{connection.xero_connection_id}' cannot transition from "
            f"'{connection.status}' to '{new_status}'; allowed transitions from "
            f"'{connection.status}' are {sorted(allowed) or '(none)'}"
        )

    now = utc_now()
    clears = new_status in _CLEARS_TENANT_ON_ENTRY
    connected_at = connection.connected_at
    if new_status == "CONNECTED" and connected_at is None:
        connected_at = now

    defaults: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
        "connected_at": connected_at,
    }
    if clears:
        defaults["tenant_id"] = None
        defaults["tenant_name"] = None
        defaults["token_expires_at"] = None

    merged = {**defaults, **field_updates}

    try:
        updated = dataclasses.replace(connection, **merged)
        validate_against_contract(updated.to_dict(), _SCHEMA)
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw exception
        raise ValidationError(f"could not transition XeroConnection: {exc}") from exc

    return updated


class XeroConnectionRepository(abc.ABC):
    """Repository abstraction for XeroConnection."""

    @abc.abstractmethod
    def begin_connect(self, *, entity_id: str) -> XeroConnection:
        """Resolve-or-create the ONE ``XeroConnection`` row for
        ``entity_id`` and move it into ``PENDING`` (the OAuth-connect
        initiation, architect spec §3's `POST /internal/xero/connect`).

        If no row exists yet, creates a fresh one directly in
        ``PENDING``. If a row already exists (any status), transitions
        it INTO ``PENDING`` (see module docstring's state machine —
        every status can re-enter ``PENDING``) UNLESS it is already
        ``PENDING``, in which case the existing row is returned
        unchanged (re-clicking 'Connect' while a flow is already in
        flight is a no-op, not a fresh transition)."""
        raise NotImplementedError

    @abc.abstractmethod
    def complete_connect(
        self,
        xero_connection_id: str,
        *,
        tenant_id: str,
        tenant_name: Optional[str],
        token_expires_at: Optional[datetime],
    ) -> XeroConnection:
        """The OAuth callback succeeded: move ``xero_connection_id``
        into ``CONNECTED`` with the real, server-resolved ``tenant_id``.

        Raises:
            core.errors.ConflictError: if ``tenant_id`` is already the
                ``tenant_id`` of a DIFFERENT connection (see module
                docstring's "Uniqueness" section) — never silently
                remaps a tenant that is already claimed elsewhere.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def fail_connect(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        """The OAuth callback failed (state mismatch already rejected
        earlier at the HTTP layer before this is ever called — this is
        for a failure genuinely reaching token exchange, e.g. Xero
        itself rejected the authorization code): ``PENDING -> ERROR``."""
        raise NotImplementedError

    @abc.abstractmethod
    def disconnect(self, xero_connection_id: str) -> XeroConnection:
        """An operator explicitly disconnected this connection: any
        non-``PENDING``-only-reachable status -> ``DISCONNECTED``."""
        raise NotImplementedError

    @abc.abstractmethod
    def fail_refresh(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        """A token-REFRESH attempt itself failed (not merely a single
        sync's own transient failure) -> ``ERROR``. See module
        docstring for why only this, and :func:`fail_auth_revoked`,
        change connection status on a sync-time failure."""
        raise NotImplementedError

    @abc.abstractmethod
    def fail_auth_revoked(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        """A live call returned 401/403 -> ``REVOKED``."""
        raise NotImplementedError

    @abc.abstractmethod
    def record_sync_attempt(self, xero_connection_id: str) -> XeroConnection:
        """Stamp ``last_attempted_sync_at`` — called at the START of
        every sync attempt, regardless of outcome. Never itself changes
        ``status``."""
        raise NotImplementedError

    @abc.abstractmethod
    def record_sync_success(
        self,
        xero_connection_id: str,
        *,
        tenant_name: Optional[str],
        token_expires_at: Optional[datetime],
    ) -> XeroConnection:
        """A sync run reached ``SUCCEEDED``: stamp
        ``last_successful_sync_at``, refresh the ``tenant_name``
        snapshot, refresh ``token_expires_at``, and — if the connection
        was in ``ERROR`` — recover it back to ``CONNECTED`` (a
        successful call is definitive proof the earlier refresh problem
        is resolved)."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_connection(self, xero_connection_id: str) -> XeroConnection:
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_entity(self, entity_id: str) -> Optional[XeroConnection]:
        """Read-only lookup, never ``NotFoundError`` — an entity with
        no connection yet is an ordinary, expected state (architect
        spec §9's 'Xero not connected' honesty requirement)."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_tenant(self, tenant_id: str) -> Optional[XeroConnection]:
        raise NotImplementedError

    @abc.abstractmethod
    def list_connections(self) -> list[XeroConnection]:
        raise NotImplementedError


class InMemoryXeroConnectionRepository(XeroConnectionRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, XeroConnection] = {}
        self._id_by_entity: dict[str, str] = {}

    def _new_row(self, entity_id: str) -> XeroConnection:
        now = utc_now()
        try:
            candidate = XeroConnection(
                xero_connection_id=identity.generate_id(),
                entity_id=entity_id,
                provider=PROVIDER_XERO,
                tenant_id=None,
                tenant_name=None,
                status="PENDING",
                connected_at=None,
                last_successful_sync_at=None,
                last_attempted_sync_at=None,
                token_expires_at=None,
                created_at=now,
                updated_at=now,
                error_detail=None,
                metadata={},
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroConnection: {exc}") from exc
        return candidate

    def begin_connect(self, *, entity_id: str) -> XeroConnection:
        existing_id = self._id_by_entity.get(entity_id)
        if existing_id is None:
            candidate = self._new_row(entity_id)
            self._by_id[candidate.xero_connection_id] = candidate
            self._id_by_entity[entity_id] = candidate.xero_connection_id
            return candidate

        current = self._by_id[existing_id]
        if current.status == "PENDING":
            return current
        updated = transition(current, "PENDING")
        self._by_id[existing_id] = updated
        return updated

    def complete_connect(
        self,
        xero_connection_id: str,
        *,
        tenant_id: str,
        tenant_name: Optional[str],
        token_expires_at: Optional[datetime],
    ) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        existing_for_tenant = self.get_by_tenant(tenant_id)
        if existing_for_tenant is not None and existing_for_tenant.entity_id != current.entity_id:
            raise ConflictError(
                f"Xero tenant '{tenant_id}' is already connected to a different BAGMAN "
                f"entity ('{existing_for_tenant.entity_id}') via connection "
                f"'{existing_for_tenant.xero_connection_id}' — refusing to map the same Xero "
                "organisation to a second BAGMAN company (architect spec §17)"
            )
        updated = transition(
            current,
            "CONNECTED",
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            token_expires_at=token_expires_at,
            error_detail=None,
        )
        self._by_id[xero_connection_id] = updated
        return updated

    def fail_connect(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        updated = transition(current, "ERROR", error_detail=error_detail)
        self._by_id[xero_connection_id] = updated
        return updated

    def disconnect(self, xero_connection_id: str) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        updated = transition(current, "DISCONNECTED", error_detail=None)
        self._by_id[xero_connection_id] = updated
        return updated

    def fail_refresh(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        updated = transition(current, "ERROR", error_detail=error_detail)
        self._by_id[xero_connection_id] = updated
        return updated

    def fail_auth_revoked(self, xero_connection_id: str, *, error_detail: str) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        updated = transition(current, "REVOKED", error_detail=error_detail)
        self._by_id[xero_connection_id] = updated
        return updated

    def record_sync_attempt(self, xero_connection_id: str) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        updated = dataclasses.replace(current, last_attempted_sync_at=utc_now(), updated_at=utc_now())
        validate_against_contract(updated.to_dict(), _SCHEMA)
        self._by_id[xero_connection_id] = updated
        return updated

    def record_sync_success(
        self,
        xero_connection_id: str,
        *,
        tenant_name: Optional[str],
        token_expires_at: Optional[datetime],
    ) -> XeroConnection:
        current = self.get_connection(xero_connection_id)
        now = utc_now()
        if current.status == "ERROR":
            updated = transition(
                current,
                "CONNECTED",
                tenant_name=tenant_name if tenant_name is not None else current.tenant_name,
                token_expires_at=token_expires_at,
                last_successful_sync_at=now,
                last_attempted_sync_at=now,
                error_detail=None,
            )
        else:
            updated = dataclasses.replace(
                current,
                tenant_name=tenant_name if tenant_name is not None else current.tenant_name,
                token_expires_at=token_expires_at,
                last_successful_sync_at=now,
                last_attempted_sync_at=now,
                updated_at=now,
            )
            validate_against_contract(updated.to_dict(), _SCHEMA)
        self._by_id[xero_connection_id] = updated
        return updated

    def get_connection(self, xero_connection_id: str) -> XeroConnection:
        try:
            return self._by_id[xero_connection_id]
        except KeyError:
            raise NotFoundError(f"no XeroConnection with xero_connection_id '{xero_connection_id}'") from None

    def get_by_entity(self, entity_id: str) -> Optional[XeroConnection]:
        existing_id = self._id_by_entity.get(entity_id)
        return self._by_id[existing_id] if existing_id is not None else None

    def get_by_tenant(self, tenant_id: str) -> Optional[XeroConnection]:
        for connection in self._by_id.values():
            if connection.tenant_id == tenant_id:
                return connection
        return None

    def list_connections(self) -> list[XeroConnection]:
        return list(self._by_id.values())
