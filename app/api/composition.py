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

from ai.invocation import AIInvocationRepository
from ai.providers.litellm.client import LiteLLMClientProtocol
from core import actor
from core.api import BagmanCanonicalAPI
from persistence.objects.store import EvidenceObjectStore
from services.evidence.intake.intake import IntakeRepository
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict

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


class _AlwaysCleanDevelopmentScanner(EvidenceSafetyScanner):
    """A DEVELOPMENT-ONLY ``EvidenceSafetyScanner`` stub (CD-4 WI-3,
    PID §19/§20) — always reports ``CLEAN``/available, never actually
    inspecting any content. Mirrors exactly how development/test
    composition already substitutes
    :class:`persistence.objects.memory_store.InMemoryObjectStore` for a
    real MinIO endpoint (see this module's own docstring): something
    cheap and dependency-free so the app/test suite can run with zero
    external services, never mistaken for a real security control.

    Deliberately defined here, in ``composition.py``, and NEVER
    imported/exported anywhere else in the codebase — there is exactly
    one place this stub is legitimately constructed
    (:func:`_build_development_or_test`), and :func:`_build_production`
    never substitutes it under any condition (the same hard "no
    fallback" invariant this module's docstring already states for the
    object store/repositories).

    A stub scanner in dev composition must never become "the real
    scanner is never exercised at all": this WI's own test suite
    (``tests/app_api/test_intake_endpoint.py``) includes tests that
    build a PRODUCTION-mode composition wired to a REAL, disposable
    ``clamd`` daemon and drive the actual HTTP intake endpoint through
    it end-to-end.
    """

    def scan(self, content) -> ScanResult:  # noqa: ANN001 - matches EvidenceSafetyScanner.scan's own signature
        return ScanResult(ScanVerdict.CLEAN, detail="development-only stub scanner: always CLEAN")

    def is_available(self) -> bool:
        return True


@dataclass(frozen=True)
class RuntimeComposition:
    """Everything ``app/api/`` needs, wired for the current
    ``runtime_environment``.

    ``engine`` is ``None`` in development/test mode (nothing to check
    liveness against) and the real, process-wide SQLAlchemy ``Engine``
    in production mode — ``routers/health.py``'s ``/ready`` handler
    uses it directly for a live ``SELECT 1`` check.

    ``intake_repository``/``scanner`` (CD-4 WI-3) are wired the same
    way: an in-memory/stub pair in development/test composition, a
    real ``PostgresIntakeRepository``/``ClamAVScanner`` pair in
    production — never mixed across modes.
    """

    runtime_environment: str
    api: BagmanCanonicalAPI
    object_store: EvidenceObjectStore
    engine: Optional[Engine]
    intake_repository: IntakeRepository
    scanner: EvidenceSafetyScanner
    #: CD-5 WI-2 — durable AIInvocation storage and the one
    #: LiteLLM-speaking adapter, wired the same dev/test-vs-production
    #: way as every field above (in-memory/fake pair in development or
    #: test composition, Postgres/real-adapter pair in production).
    ai_invocation_repository: AIInvocationRepository
    litellm_client: LiteLLMClientProtocol


def _build_development_or_test(runtime_environment: str) -> RuntimeComposition:
    from ai.invocation import InMemoryAIInvocationRepository
    from ai.providers.litellm.fake import FakeLiteLLMClient
    from persistence.objects.memory_store import InMemoryObjectStore
    from services.evidence.intake.intake import InMemoryIntakeRepository

    return RuntimeComposition(
        runtime_environment=runtime_environment,
        api=BagmanCanonicalAPI(),  # CD-2's own in-memory default construction
        object_store=InMemoryObjectStore(),
        engine=None,
        intake_repository=InMemoryIntakeRepository(),
        scanner=_AlwaysCleanDevelopmentScanner(),
        ai_invocation_repository=InMemoryAIInvocationRepository(),
        litellm_client=FakeLiteLLMClient(),
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
    from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
    from persistence.postgres.intake_repository import PostgresIntakeRepository
    from persistence.postgres.provenance_repository import PostgresProvenanceRepository
    from persistence.postgres.session import get_engine
    from persistence.postgres.source_repository import PostgresSourceRepository
    from services.evidence.intake.scanner import ClamAVScanner

    from ai.providers.litellm.client import DEFAULT_LITELLM_API_KEY_FILE, DEFAULT_LITELLM_ENDPOINT, LiteLLMClient

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

    # CD-4 WI-3: durable intake repository, sharing the same engine as
    # every other Postgres-backed repository above.
    intake_repository = PostgresIntakeRepository(engine)

    # CD-4 WI-3: a real ClamAV scanner — configured from
    # BAGMAN_SCANNER_HOST/BAGMAN_SCANNER_PORT (mirroring the existing
    # env-var-driven MinIO configuration pattern above), defaulting to
    # the `bagman-scan` service name/port this WI adds to
    # deployment/compose/docker-compose.yml. Constructing a
    # ClamAVScanner performs no I/O itself (unlike MinIOObjectStore
    # above) — it only stores host/port; reachability is proven live by
    # `/ready` (routers/health.py), never here, so a down bagman-scan
    # does not prevent composition from succeeding.
    scanner = ClamAVScanner(
        host=os.environ.get("BAGMAN_SCANNER_HOST", "bagman-scan"),
        port=int(os.environ.get("BAGMAN_SCANNER_PORT", "3310")),
    )

    # CD-5 WI-2: durable AIInvocation storage, sharing the same engine
    # as every other Postgres-backed repository above, plus the one
    # real LiteLLM-speaking adapter — see ai/providers/litellm/client.py
    # for its endpoint/secret-file/timeout/retry configuration and its
    # own documented "no eager I/O at construction" contract (mirrors
    # ClamAVScanner immediately above: reachability is proven live by
    # GET /internal/ai/health, never here, so a down/misconfigured
    # LiteLLM gateway does not prevent composition from succeeding —
    # PID §48's own "evidence/runtime services remain usable even when
    # AI is unavailable").
    ai_invocation_repository = PostgresAIInvocationRepository(engine)
    litellm_client = LiteLLMClient(
        endpoint=os.environ.get("BAGMAN_LITELLM_ENDPOINT", DEFAULT_LITELLM_ENDPOINT),
        api_key_file=os.environ.get("BAGMAN_LITELLM_API_KEY_FILE", DEFAULT_LITELLM_API_KEY_FILE),
    )

    return RuntimeComposition(
        runtime_environment=_PRODUCTION,
        api=api,
        object_store=object_store,
        engine=engine,
        intake_repository=intake_repository,
        scanner=scanner,
        ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client,
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
    no-fallback proof, within a single test process. Also clears the
    memoized ``MANUAL_UPLOAD`` source id (CD-4 WI-3) — a source id
    memoized against one composition (e.g. a prior test's disposable
    Postgres database) would otherwise be silently stale/invalid
    against the NEXT composition this process builds."""
    global _composition, _manual_upload_source_id
    with _lock:
        _composition = None
        _manual_upload_source_id = None


# ---------------------------------------------------------------------
# Stable MANUAL_UPLOAD Source lifecycle (CD-4 WI-3, PID §9)
# ---------------------------------------------------------------------
#
# PID §9: "The implementation should not create a new Source for every
# file... A stable manual-upload source for the BAGMAN operator/runtime
# is preferable... Exact source lifecycle may be determined during
# implementation but must remain explicit and idempotent."
#
# Chosen lifecycle (explicit, documented here since this IS the
# decision): exactly one Source row, identified by the well-known,
# closed pair (source_type="MANUAL_UPLOAD", provider=
# "bagman-manual-upload"), created lazily the first time it is needed
# and reused for every subsequent manual-upload intake for the rest of
# this process's life. Resolution is:
#
#   1. an in-process memoized id (fast path, no query at all once
#      warm) — mirrors get_composition()'s own _composition cache;
#   2. a read-only find_by_provider() lookup (the source may already
#      exist — created by an earlier process, or an earlier request in
#      this same process before the memoized value was set);
#   3. only if neither finds it, register_source() creates it.
#
# Known limitation, stated plainly rather than silently accepted: there
# is no database-level uniqueness constraint on (source_type, provider)
# backing this — unlike intake idempotency_key (a real partial unique
# index) or evidence external_reference tuples (a real unique
# constraint), a genuine multi-PROCESS race the very first time this
# runs (two bagman-api processes/workers, both cold, both losing the
# find_by_provider() race) could each independently create a distinct
# MANUAL_UPLOAD Source row. This is judged acceptable for CD-4: BAGMAN
# today runs as a single bagman-api process/container (see
# deployment/compose/docker-compose.yml — no multi-replica deployment
# exists yet), so the only race that matters in practice is
# intra-process, which the module-level lock below fully closes. A
# multi-replica deployment would need either a real unique constraint
# on sources(source_type, provider) or an explicit migration-time seed
# row — flagged here for whoever introduces multi-replica bagman-api,
# not solved speculatively now.
_MANUAL_UPLOAD_SOURCE_TYPE = "MANUAL_UPLOAD"
_MANUAL_UPLOAD_SOURCE_PROVIDER = "bagman-manual-upload"

_manual_upload_source_id: Optional[str] = None


def get_manual_upload_source_id(composition: "RuntimeComposition") -> str:
    """Resolve (or, on first use, create) the single stable
    ``MANUAL_UPLOAD`` ``Source`` id (see module section docstring
    above). Memoized for the life of the process once resolved."""
    global _manual_upload_source_id
    if _manual_upload_source_id is not None:
        return _manual_upload_source_id

    with _lock:
        if _manual_upload_source_id is not None:
            return _manual_upload_source_id

        existing = composition.api.source_repository.find_by_provider(
            source_type=_MANUAL_UPLOAD_SOURCE_TYPE,
            provider=_MANUAL_UPLOAD_SOURCE_PROVIDER,
        )
        if existing is not None:
            _manual_upload_source_id = existing.source_id
            return _manual_upload_source_id

        source = composition.api.register_source(
            source_type=_MANUAL_UPLOAD_SOURCE_TYPE,
            provider=_MANUAL_UPLOAD_SOURCE_PROVIDER,
            status="ACTIVE",
            actor_type=actor.SYSTEM,
            actor_id="bagman-manual-upload-bootstrap",
            metadata={
                "note": "stable MANUAL_UPLOAD source, resolved-or-created once per "
                "process (CD-4 WI-3, PID §9) — never one Source per uploaded file"
            },
        )
        _manual_upload_source_id = source.source_id
        return _manual_upload_source_id
