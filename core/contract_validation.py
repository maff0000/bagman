"""Validate BAGMAN domain dicts against their canonical JSON Schema
contracts under ``contracts/`` — the single source of truth (PID §18).

Every ``register_*``/``record_*`` construction path in ``core/`` and
``services/evidence/`` calls :func:`validate_against_contract` on the
dict a domain object is about to become, *before* accepting that
object, and never lets a raw ``jsonschema`` (or other third-party)
exception escape — failures are re-raised as the canonical
:class:`core.errors.ValidationError`.

This module is the one place that knows how to locate, parse, cache,
and cross-reference (via ``$ref``) the schema files in ``contracts/``,
and how to wire up format validation (in particular ``format:
date-time``, which plain ``jsonschema`` does not enforce without the
``rfc3339-validator`` package — see ``requirements.txt``). It is a
single, well-defined cross-domain responsibility (schema validation),
not a generic dumping ground (PID §37).
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from core.errors import ValidationError

_CONTRACTS_ROOT = Path(__file__).resolve().parent.parent / "contracts"


@functools.lru_cache(maxsize=None)
def _registry() -> Registry:
    """Build a ``referencing.Registry`` covering every schema file
    under ``contracts/``, keyed by each schema's own ``$id`` — this is
    what lets a domain schema's ``$ref`` to
    ``contracts/common/bagman.identifier.v1.schema.json`` (etc.)
    resolve correctly regardless of which schema is being validated
    against.
    """
    resources = []
    for path in sorted(_CONTRACTS_ROOT.rglob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(contents, default_specification=DRAFT202012)
        resources.append((contents["$id"], resource))
    return Registry().with_resources(resources)


@functools.lru_cache(maxsize=None)
def _validator_for(schema_relative_path: str) -> Draft202012Validator:
    """Return a cached validator for a contract file, addressed by its
    path relative to ``contracts/`` (e.g.
    ``"entity/bagman.entity.v1.schema.json"``).
    """
    path = _CONTRACTS_ROOT / schema_relative_path
    schema = json.loads(path.read_text(encoding="utf-8"))
    # A plain FormatChecker() picks up every format checker jsonschema
    # has registered at import time — including "date-time", which
    # jsonschema only registers if the `rfc3339-validator` package is
    # importable (see requirements.txt).
    return Draft202012Validator(
        schema, registry=_registry(), format_checker=FormatChecker()
    )


def validate_against_contract(instance: dict, schema_relative_path: str) -> None:
    """Validate ``instance`` against the contract at
    ``schema_relative_path`` (relative to ``contracts/``).

    Raises:
        ValidationError: if ``instance`` does not conform, or if the
            underlying schema/validator machinery itself fails for any
            reason (never lets a raw ``jsonschema`` exception escape).
    """
    try:
        validator = _validator_for(schema_relative_path)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(map(str, e.path)))
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive: never leak third-party errors
        raise ValidationError(
            f"contract validation machinery failed for '{schema_relative_path}': {exc}"
        ) from exc

    if errors:
        detail = "; ".join(
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise ValidationError(
            f"instance failed validation against contract '{schema_relative_path}': {detail}"
        )


def describe_schema_errors(instance: Any, schema: dict) -> list[str]:
    """Validate ``instance`` against an in-memory JSON Schema ``schema``
    dict — as opposed to :func:`validate_against_contract`, which loads
    its schema from a file under ``contracts/`` and raises on failure.

    Added for CD-5 WI-1 (PID §24/§76): a task's structured-output
    validation must produce a non-raising, storable
    ``{"valid": ..., "errors": [...]}``-shaped result (see
    ``ai.tasks.validate_task_output``) rather than crash the caller —
    "do not repair malformed output silently; record validation
    failure" (PID §76) requires the failure to be data, not an
    exception. This function is the shared, non-raising primitive that
    makes that possible without ``ai.tasks`` duplicating any
    ``jsonschema`` wiring of its own.

    Returns a list of human-readable error strings (``path: message``),
    empty if ``instance`` conforms. Reuses the same cross-referencing
    :func:`_registry` (so a task schema could ``$ref`` a
    ``contracts/common/`` primitive if it ever needed to) and the same
    ``FormatChecker`` :func:`validate_against_contract` uses.

    Raises:
        ValidationError: only if the schema/validator machinery itself
            is broken (e.g. a malformed ``schema`` dict) — never for an
            ordinary validation failure, which is returned as data
            instead (mirrors :func:`validate_against_contract`'s own
            "never leak a raw jsonschema exception" discipline for
            this specific failure mode only).
    """
    try:
        validator = Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(map(str, e.path)))
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive: never leak third-party errors
        raise ValidationError(f"schema validation machinery failed: {exc}") from exc

    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
