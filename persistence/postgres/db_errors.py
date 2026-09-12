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

Malformed-identifier lookups (bug fix, CD-3 WI-3 PL review)
------------------------------------------------------------
A caller-supplied id string that is not even valid UUID *syntax* (e.g.
``"not-a-valid-uuid"``) makes ``session.get(SomeRow, some_id)`` (or any
other query comparing it against a native PostgreSQL ``UUID`` column)
raise a DBAPI error — surfaced by SQLAlchemy as ``sqlalchemy.exc.
DataError`` wrapping psycopg's ``InvalidTextRepresentation`` (SQLSTATE
``22P02``, ``invalid_text_representation`` — Postgres has no distinct
``invalid_uuid`` SQLSTATE; malformed UUID input surfaces under this
same general "bad literal for the target type" code) — *before* the
row-is-None check a repository method's own body ever gets to run.
Every repository in this package already caught only ``IntegrityError``
and the generic ``SQLAlchemyError``, so this ``DataError`` fell through
to the generic branch and was (wrongly) translated to
``PersistenceError`` (-> HTTP 503) instead of ``NotFoundError`` (-> HTTP
404) — a malformed id can never correspond to an existing row, so "not
found" is the honest, correct answer, not "the persistence layer is
broken". :func:`is_invalid_uuid_format` lets each repository add one
narrow ``except DataError`` branch (checked BEFORE the generic
``except SQLAlchemyError``, since ``DataError`` is itself a
``SQLAlchemyError`` subclass and would otherwise be caught there first)
without broadening what the generic branch still catches — a genuine
connectivity/operational ``DataError`` shape this helper does not
recognise still falls through to ``PersistenceError`` as before.
"""
from __future__ import annotations

from typing import Optional

from psycopg import errors as psycopg_errors
from sqlalchemy.exc import DataError, IntegrityError


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


def is_invalid_uuid_format(exc: DataError) -> bool:
    """``True`` if ``exc`` wraps a psycopg ``InvalidTextRepresentation``
    (SQLSTATE ``22P02``) — a caller-supplied id string that is not
    valid UUID syntax at all, raised by PostgreSQL itself when the
    value is compared against a native ``UUID`` column (e.g. inside
    ``session.get(SomeRow, some_id)``). A malformed id can never
    correspond to an existing row, so the caller should translate this
    to ``core.errors.NotFoundError`` — never ``PersistenceError``,
    which must stay reserved for genuine connectivity/operational
    failures.
    """
    return isinstance(exc.orig, psycopg_errors.InvalidTextRepresentation)
