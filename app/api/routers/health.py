"""``GET /health`` (liveness) and ``GET /ready`` (readiness) — PID §25-26.

Liveness and readiness answer two deliberately different questions,
and must never be conflated:

* ``/health`` — "is the process alive?" Always ``200`` if this handler
  can run at all. It does not touch ``app.api.composition`` (does
  not even build the composition) — a process that is alive but whose
  production dependencies are down is still *alive*, and Compose's own
  ``bagman-api`` healthcheck (which gates ``depends_on:
  condition: service_healthy`` for anything that might one day depend
  on ``bagman-api``) must not flap on a downstream outage this
  container itself did not cause.
* ``/ready`` — "can BAGMAN currently perform its required work?" In
  ``development``/``test`` composition mode this is unconditionally
  ``200`` (an in-memory backend has no external dependency to fail).
  In ``production`` mode this performs REAL, live checks on every call
  — a ``SELECT 1`` against PostgreSQL and an ``exists()`` probe against
  the object store — and returns ``503`` naming whichever dependency
  failed the moment either one does. There is no cached "was ready
  five seconds ago" state: every ``/ready`` call re-proves both
  dependencies from scratch, which is exactly what makes the CD-3 WI-3
  verification's "stop bagman-db, confirm 503; restart it, confirm
  recovery" proof (PID §45-46) meaningful rather than accidental.

Hard invariant (PID §46): nothing in this module ever causes BAGMAN to
fall back to an in-memory/local-disk backend when a production
dependency is unreachable — a failed check here always means "return
503", never "quietly reconfigure and try something else". See
``app/api/composition.py``'s module docstring for where that
guarantee actually lives (there is no fallback code path to invoke).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.errors import StorageError
from app.api.composition import get_composition

logger = logging.getLogger("bagman.runtime.api.health")

router = APIRouter()

#: A fixed, well-formed, never-actually-registered object-storage key
#: used purely to prove the object store is reachable (PID §25's "can
#: BAGMAN currently perform its required work?"). Shaped exactly like a
#: real `persistence.objects.store.object_key()` value so it passes
#: that module's own key-safety validation, but deliberately reads as a
#: readiness probe, not real evidence, to anyone inspecting bucket
#: contents or logs. `EvidenceObjectStore.exists()` is used (rather
#: than `put()`/`get()`) because it is the one operation that proves
#: reachability without writing anything or requiring the key to
#: already exist — a `False` result is just as valid a "the object
#: store answered" proof as `True`.
_READINESS_PROBE_EVIDENCE_ID = "00000000-0000-0000-0000-000000000000"
_READINESS_PROBE_HASH = "0" * 64


@router.get("/health")
async def health() -> dict:
    return {"status": "alive"}


@router.get("/ready")
async def ready() -> JSONResponse:
    try:
        composition = get_composition()
    except StorageError as exc:
        logger.warning(
            "readiness check failed: object store unreachable while building composition",
            extra={"component": "bagman.runtime.api.health", "event_type": "READINESS_CHECK_FAILED"},
            exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "runtime_environment": "production",
                "failed_dependency": "object_store",
                "detail": f"object storage unreachable: {exc.message}",
            },
        )
    except Exception as exc:  # noqa: BLE001 - composition build itself failed
        logger.error(
            "readiness check failed: could not build runtime composition",
            extra={"component": "bagman.runtime.api.health", "event_type": "READINESS_CHECK_FAILED"},
            exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "failed_dependency": "composition",
                "detail": f"could not build runtime composition: {exc}",
            },
        )

    if composition.runtime_environment != "production":
        # Development/test: an in-memory backend has no external
        # dependency to check — explicit about which mode is active
        # rather than ambiguous (PID §25's "be explicit ... never
        # ambiguous" instruction).
        return JSONResponse(
            status_code=200,
            content={
                "ready": True,
                "runtime_environment": composition.runtime_environment,
                "checks": {},
                "detail": "in-memory composition active; no external dependency to check",
            },
        )

    # Production: prove PostgreSQL reachability, live, right now.
    try:
        with composition.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning(
            "readiness check failed: PostgreSQL unreachable",
            extra={"component": "bagman.runtime.api.health", "event_type": "READINESS_CHECK_FAILED"},
        )
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "runtime_environment": "production",
                "failed_dependency": "postgres",
                "detail": f"PostgreSQL SELECT 1 failed: {type(exc).__name__}",
            },
        )

    # Production: prove object-store reachability, live, right now.
    try:
        composition.object_store.exists(
            f"evidence/{_READINESS_PROBE_EVIDENCE_ID}/{_READINESS_PROBE_HASH}"
        )
    except StorageError as exc:
        logger.warning(
            "readiness check failed: object store unreachable",
            extra={"component": "bagman.runtime.api.health", "event_type": "READINESS_CHECK_FAILED"},
        )
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "runtime_environment": "production",
                "failed_dependency": "object_store",
                "detail": f"object storage exists() probe failed: {exc.message}",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "ready": True,
            "runtime_environment": "production",
            "checks": {"postgres": "ok", "object_store": "ok"},
        },
    )
