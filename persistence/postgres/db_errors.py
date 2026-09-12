"""Shared helpers for translating raw SQLAlchemy/psycopg exceptions
into BAGMAN canonical errors (PID §57), reused by every
``persistence/postgres/*_repository.py`` module.

No repository in this package lets a raw ``sqlalchemy``/``psycopg``
exception escape as its public contract. Each repository catches
``sqlalchemy.exc.IntegrityError`` and uses the helpers below to decide
whether it is a *specific* constraint violation that repository
already expects (translated to the matching existing canonical error —
``ImmutabilityViolationError``, ``DuplicateExternalReferenceError``, or
``InvalidProvenanceError``), or anything else (translated to the
generic ``core.errors.PersistenceError``). Any other SQLAlchemy
failure (``sqlalchemy.exc.SQLAlchemyError`` — connectivity/operational
errors in particular) is likewise translated to ``PersistenceError``.
"""
from __future__ import annotations

from typing import Optional

from psycopg import errors as psycopg_errors
from sqlalchemy.exc import IntegrityError


def unique_violation_constraint(exc: IntegrityError) -> Optional[str]:
    """If ``exc`` wraps a psycopg ``UniqueViolation``, return the name
    of the violated constraint (e.g. ``"uq_external_references_tuple"``,
    ``"governed_entities_pkey"``) so the caller can decide which
    canonical error corresponds to *this specific* constraint.

    Returns ``None`` if ``exc`` is not a unique-violation at all (e.g.
    a foreign-key or not-null violation instead), or if the constraint
    name could not be determined from the driver's diagnostics.
    """
    orig = exc.orig
    if not isinstance(orig, psycopg_errors.UniqueViolation):
        return None
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


def is_foreign_key_violation(exc: IntegrityError) -> bool:
    """``True`` if ``exc`` wraps a psycopg ``ForeignKeyViolation`` —
    e.g. an orphan ``evidence_id`` on a ``Provenance`` insert, or an
    ``entity_id`` on an ``EvidenceItem``/``assign_entity`` call that
    does not reference an existing ``GovernedEntity``.
    """
    return isinstance(exc.orig, psycopg_errors.ForeignKeyViolation)
