"""The PID §14 composition root (CD-3 WI-3).

This is the ONE place in the entire BAGMAN codebase that reads
``BAGMAN_RUNTIME_ENV`` to decide which repository/object-store
implementations back a running ``core.api.BagmanCanonicalAPI``. No
other file under ``core/``, ``services/``, or ``persistence/`` contains
an ``if ENV == ...`` check of any kind — and none should ever be added
there; if a future change seems to need one, it belongs here instead.

``BAGMAN_RUNTIME_ENV`` values
------------------------------
* ``"development"`` (the default when unset) or ``"test"`` — an
  in-memory ``BagmanCanonicalAPI`` (CD-2's own default construction)
  plus an :class:`persistence.objects.memory_store.InMemoryObjectStore`.
  No external dependency of any kind.
* ``"production"`` — a ``BagmanCanonicalAPI`` built entirely from
  ``persistence.postgres.*`` repositories sharing one ``Engine``, plus
  a :class:`persistence.objects.minio_store.MinIOObjectStore` built
  from ``BAGMAN_OBJECT_STORE_*`` environment variables and Docker/
  Compose secret files (never a literal credential — PID §29-31).

Hard "no fallback" invariant (PID §45-46)
-------------------------------------------
There is no code path anywhere in this module that falls back from
``production`` composition to an in-memory one, under any condition —
not a missing secret file, not an unreachable database, not an
unreachable object store. If ``BAGMAN_RUNTIME_ENV=production`` is
selected, :func:`get_composition` either returns a composition wired
entirely to PostgreSQL/MinIO, or it raises (so the caller — ordinarily
``app/api/routers/health.py``'s ``/ready`` handler — can report
"not ready", never silently substitute a different backend.

This is deliberately observable: constructing the PostgreSQL-backed
repositories never itself contacts the database (`persistence.postgres
.session.get_engine()`'s ``Engine`` is lazy — no network I/O happens
until a query actually runs), so a down ``bagman-db`` does not prevent
composition from succeeding; ``/ready`` instead proves database
reachability itself, live, on every call (see ``routers/health.py``).
Object storage is the one part of composition that *can* fail eagerly
here: ``MinIOObjectStore.__init__`` performs a real ``head_bucket``/
``create_bucket`` call against the configured endpoint. When that
raises (object storage unreachable at the moment composition is first
attempted), :func:`get_composition` propagates the failure and — this
is the important part — does **not** cache a broken/partial
composition: the next call tries again from scratch. This is what lets
``/ready`` correctly recover once ``bagman-objects`` comes back after
being stopped (CD-3 WI-3 verification step 7), rather than staying
wedged "not ready" forever after one transient failure.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy.engine import Engine

from core.api import BagmanCanonicalAPI
from persistence.objects.store import EvidenceObjectStore

#: Repo root, resolved once from this file's own location
#: (``app/api/composition.py`` -> ``app/api`` -> ``app`` ->
#: repo root), used by this module and re-exported for
#: ``routers/version.py`` (VERSION file, alembic.ini/script directory)
#: so that path-resolution logic exists in exactly one place.
REPO_ROOT = Path(__file__).resolve().parents[2]

_DEVELOPMENT = "development"
_TEST = "test"
_PRODUCTION = "production"
_VALID_ENVIRONMENTS = frozenset({_DEVELOPMENT, _TEST, _PRODUCTION})
_DEFAULT_ENVIRONMENT = _DEVELOPMENT


def get_runtime_environment() -> str:
    """Read ``BAGMAN_RUNTIME_ENV`` (default ``"development"``).

    This is the ONLY function in this codebase that reads this
    environment variable — every other place that needs to know the
    current mode (``routers/version.py``'s ``runtime_environment``
    field, ``routers/health.py``'s ``/ready`` behaviour) gets it from
    the :class:`RuntimeComposition` this module builds, not by reading
    the environment variable a second time.
    """
    raw = os.environ.get("BAGMAN_RUNTIME_ENV", _DEFAULT_ENVIRONMENT).strip().lower()
    if raw not in _VALID_ENVIRONMENTS:
        raise RuntimeError(
            f"BAGMAN_RUNTIME_ENV={raw!r} is not one of {sorted(_VALID_ENVIRONMENTS)}"
        )
    return raw


def _read_secret_file(env_var: str, default_path: str) -> str:
    """Read a secret value from the file named by ``env_var`` (default
    ``default_path``) — the same secret-file discipline
    ``persistence/postgres/session.py`` already uses for the database
    password (PID §29-31). Never returns a default *value* — only ever
    a default *path*; the actual secret always comes from disk.
    """
    path = Path(os.environ.get(env_var, default_path))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"could not read required secret from {env_var}={str(path)!r}: {exc}. "
            "BAGMAN never hardcodes this credential; point the environment "
            "variable at a readable file (e.g. a mounted Docker/Compose secret)."
        ) from exc


@dataclass(frozen=True)
class RuntimeComposition:
    """Everything ``app/api/`` needs, wired for the current
    ``runtime_environment``.

    ``engine`` is ``None`` in development/test mode (nothing to check
    liveness against) and the real, process-wide SQLAlchemy ``Engine``
    in production mode — ``routers/health.py``'s ``/ready`` handler
    uses it directly for a live ``SELECT 1`` check.
    """

    runtime_environment: str
    api: BagmanCanonicalAPI
    object_store: EvidenceObjectStore
    engine: Optional[Engine]


def _build_development_or_test(runtime_environment: str) -> RuntimeComposition:
    from persistence.objects.memory_store import InMemoryObjectStore

    return RuntimeComposition(
        runtime_environment=runtime_environment,
        api=BagmanCanonicalAPI(),  # CD-2's own in-memory default construction
        object_store=InMemoryObjectStore(),
        engine=None,
    )


def _build_production() -> RuntimeComposition:
    # Imported lazily so `import app.api.composition` alone never
    # requires boto3/psycopg to be installed for a pure development/test
    # run — mirrors how `persistence/objects/minio_store.py` is only
    # ever imported by something that actually needs MinIO.
    from persistence.objects.minio_store import MinIOConfig, MinIOObjectStore
    from persistence.postgres.audit_repository import PostgresAuditRepository
    from persistence.postgres.entity_repository import PostgresEntityRepository
    from persistence.postgres.evidence_repository import PostgresEvidenceRepository
    from persistence.postgres.external_reference_repository import (
        PostgresExternalReferenceRepository,
    )
    from persistence.postgres.provenance_repository import PostgresProvenanceRepository
    from persistence.postgres.session import get_engine
    from persistence.postgres.source_repository import PostgresSourceRepository

    # `get_engine()` builds/returns a pooled SQLAlchemy Engine but never
    # itself opens a connection (PID §46 note above) — safe to call even
    # if bagman-db is currently down.
    engine: Engine = get_engine()

    external_reference_repository = PostgresExternalReferenceRepository(engine)
    evidence_repository = PostgresEvidenceRepository(external_reference_repository, engine)
    api = BagmanCanonicalAPI(
        entity_repository=PostgresEntityRepository(engine),
        source_repository=PostgresSourceRepository(engine),
        external_reference_repository=external_reference_repository,
        evidence_repository=evidence_repository,
        provenance_repository=PostgresProvenanceRepository(evidence_repository, engine),
        audit_repository=PostgresAuditRepository(engine),
    )

    # Unlike the repositories above, constructing MinIOObjectStore DOES
    # perform real I/O immediately (`_ensure_bucket()`'s head_bucket/
    # create_bucket call) — see this module's docstring for why that is
    # fine (and deliberately not cached on failure).
    object_store = MinIOObjectStore(
        MinIOConfig(
            endpoint_url=os.environ["BAGMAN_OBJECT_STORE_ENDPOINT_URL"],
            access_key=_read_secret_file(
                "BAGMAN_OBJECT_STORE_ACCESS_KEY_FILE", "/run/secrets/minio_root_user"
            ),
            secret_key=_read_secret_file(
                "BAGMAN_OBJECT_STORE_SECRET_KEY_FILE", "/run/secrets/minio_root_user_password"
            ),
            bucket=os.environ.get("BAGMAN_OBJECT_STORE_BUCKET", "bagman-evidence"),
            region_name=os.environ.get("BAGMAN_OBJECT_STORE_REGION", "us-east-1"),
        )
    )

    return RuntimeComposition(
        runtime_environment=_PRODUCTION,
        api=api,
        object_store=object_store,
        engine=engine,
    )


_composition: Optional[RuntimeComposition] = None
_lock = threading.Lock()


def get_composition() -> RuntimeComposition:
    """Return the process-wide :class:`RuntimeComposition`, building it
    on first use.

    A *successful* composition is cached for the life of the process
    (repositories/the object-store client are cheap to reuse and hold
    no per-request state). A *failed* build (production mode, object
    storage unreachable) is never cached — see the module docstring;
    the next call tries again.
    """
    global _composition
    if _composition is not None:
        return _composition

    with _lock:
        if _composition is not None:
            return _composition

        runtime_environment = get_runtime_environment()
        if runtime_environment == _PRODUCTION:
            composition = _build_production()
        else:
            composition = _build_development_or_test(runtime_environment)

        _composition = composition
        return composition


def reset_composition_for_tests() -> None:
    """Test-only hook: forces the next :func:`get_composition` call to
    re-read ``BAGMAN_RUNTIME_ENV`` and rebuild from scratch — used by
    ``tests/app_api/`` to exercise both composition modes, and the
    no-fallback proof, within a single test process."""
    global _composition
    with _lock:
        _composition = None
