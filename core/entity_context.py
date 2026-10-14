"""``EntityContext`` / ``resolve_entity_context`` — the shared, reusable
entity-scoping contract WO-BAGMAN-117 §6.A.2 (PID §117.H) requires:
"the shared mechanism Banking, Invoicing, Tax, and every existing
entity-touching service must call — not a bespoke per-service
re-implementation."

Relationship to ``app/api/routers/xero.py``'s existing
``_require_entity``/line-1007 patterns (documented per this WO's own
explicit instruction to record which approach was taken and why)
------------------------------------------------------------------------
This module GENERALISES the already-proven shape of
``xero.py::_require_entity`` (validate ``entity_id`` is a real
``GovernedEntity`` before doing anything else) and the redundant
cross-entity ownership re-check at ``xero.py`` (the `get_account_suggestion`
handler's own "a suggestion/assignment row that belongs to a DIFFERENT
entity than the URL's own entity_id is treated identically to 'does
not exist'" doctrine) into one new, independent, reusable pair of
functions — :func:`resolve_entity_context` and
:meth:`EntityContext.filter_owned`.

It deliberately does **NOT** modify ``app/api/routers/xero.py`` itself.
WO-BAGMAN-117 §6.A's own enumerated file list does not include
``app/api/routers/xero.py`` (Xero write/read behaviour is explicitly
"must not be altered" territory for this delivery — PID §117.I's
exclusions, and WO-BAGMAN-117 §7's explicit exclusion of "any change to
Xero/Microsoft/Gmail OAuth provider-connection flows beyond preserving
their existing callback contracts"), so "leave the existing call sites
exactly as they are, and independently prove the new function is
behaviourally equivalent" is not merely the smaller/safer of two
judgment-call options here — it is the only option actually authorised
by this WO's own file scope. ``tests/`` for this module (see
``tests/persistence/test_entity_context.py``) prove exact behavioural
equivalence against both existing patterns: same ``NotFoundError`` for
an unknown ``entity_id``, same "treated identically to does not exist"
outcome for a cross-entity-owned object.

``Banking``/``Invoicing``/``Tax`` and any OTHER future entity-scoped
service (mailboxes, Needs You — both currently entirely unscoped per
PID §117.B's own ground-truth finding) can adopt this function going
forward without this WO itself rewiring any of them — that rewiring is
explicitly out of THIS WO's scope (§117.H: this WO delivers the
contract; services besides Xero adopting it is each service's own,
separately governed future work).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from core.entity import GovernedEntity


class _HasEntityRepository(Protocol):
    """Structural shape this module needs from a composition-like
    object — duck-typed (mirrors this codebase's existing
    duck-typed-dependency convention, e.g.
    ``services.evidence.classification.EvidenceClassificationRepository``'s
    own constructor dependencies) so this module never has to import
    ``app.api.composition.RuntimeComposition`` directly (which would be
    a backwards, app-depends-on-core-depends-on-app layering violation
    — PID §53)."""

    @property
    def api(self) -> Any: ...  # `composition.api.entity_repository.get_entity(...)`


@dataclass(frozen=True)
class EntityContext:
    """The resolved, validated scoping context for one request touching
    exactly one ``GovernedEntity`` (PID §117.H: "which governed entity's
    data is currently in view" — orthogonal to *who* is acting, which
    is the separate operator-session concept ``core.auth`` establishes).
    """

    entity_id: str
    entity: GovernedEntity

    def filter_owned(self, obj: Optional[Any]) -> Optional[Any]:
        """Generalises ``xero.py``'s own redundant cross-entity
        ownership re-check: given ``obj`` (or ``None``) that carries its
        own ``entity_id`` attribute, return it unchanged if
        ``obj.entity_id == self.entity_id`` — otherwise return ``None``,
        treating a different entity's record identically to "does not
        exist" (PID §117.H: "Explicit server-side entity-ownership
        validation is retained regardless of the single-operator
        simplification ... these checks protect accounting correctness
        ... must not be weakened").

        ``obj`` is duck-typed (any object with an ``entity_id``
        attribute — a Xero suggestion/assignment today, any future
        entity-scoped record tomorrow), exactly like
        ``xero.py::get_account_suggestion``'s own inline
        ``suggestion.entity_id != entity_id`` check this generalises,
        just lifted out of that one handler into a reusable method any
        caller can use for any object shape.
        """
        if obj is None:
            return None
        if getattr(obj, "entity_id", None) != self.entity_id:
            return None
        return obj


def resolve_entity_context(composition: _HasEntityRepository, entity_id: str) -> EntityContext:
    """Confirm ``entity_id`` is a real, currently-registered
    ``GovernedEntity`` and return the resulting :class:`EntityContext`.

    Generalises ``xero.py::_require_entity``'s own "an honest 404 for a
    typo'd/unknown entity_id, never a connection created against
    nothing" doctrine — behaviourally identical (same
    ``core.errors.NotFoundError`` raised by
    ``EntityRepository.get_entity`` on an unknown id, via the exact same
    call), just returning the resolved entity as a reusable context
    object instead of discarding it.

    Raises:
        core.errors.NotFoundError: no such ``entity_id`` (propagated
            directly from ``EntityRepository.get_entity`` — never
            re-wrapped, so an existing caller's exception-handling
            expectations are unaffected).
    """
    entity = composition.api.entity_repository.get_entity(entity_id)
    return EntityContext(entity_id=entity_id, entity=entity)
