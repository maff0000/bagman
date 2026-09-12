"""Canonical BAGMAN actor-type vocabulary (PID §14).

Matches the *closed* ``actor_type`` enum in
``contracts/audit/bagman.audit_event.v1.schema.json`` exactly:
``SYSTEM``, ``USER``, ``AGENT``, ``SERVICE``, ``EXTERNAL_SYSTEM``.

Represented as plain string constants plus a frozen set (not a
``enum.Enum``) so callers can pass and compare plain strings that
serialise directly into contract-conformant dicts with no translation
step — the contract's ``actor_type`` field is itself a plain string
enum, not a typed wrapper.

PID §14 also requires ``actor_id`` to identify the *specific* actor
(e.g. ``"matt"``, ``"bagman-agent"``, ``"mail-ingest-worker"``) and
forbids representing all activity as a generic ``"system"`` literal —
that is enforced by contract (``actor_id`` has ``minLength: 1`` and is
a separate required field) and by convention here, not by this module.
"""
from __future__ import annotations

SYSTEM = "SYSTEM"
USER = "USER"
AGENT = "AGENT"
SERVICE = "SERVICE"
EXTERNAL_SYSTEM = "EXTERNAL_SYSTEM"

ALL: frozenset[str] = frozenset({SYSTEM, USER, AGENT, SERVICE, EXTERNAL_SYSTEM})


def is_valid(actor_type: str) -> bool:
    """Return ``True`` if ``actor_type`` is one of the closed set of
    canonical BAGMAN actor types."""
    return actor_type in ALL
