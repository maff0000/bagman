"""``GET /version`` (PID §7, §49).

Exposes exactly the four fields PID §49 asks for, and nothing secret:

* ``git_commit`` — baked into the image at build time (see
  ``deployment/docker/api/Dockerfile``'s ``GIT_COMMIT`` build arg,
  which becomes the ``BAGMAN_GIT_COMMIT`` environment variable this
  module reads). ``"unknown"`` outside a built image (e.g. running
  ``uvicorn`` directly from a checkout without that env var set) —
  never fabricated.
* ``build_version`` — the literal contents of the repo-root ``VERSION``
  file, copied into the image at build time.
* ``schema_migration_version`` — the current Alembic head revision.
  In production mode this is queried LIVE against the real database via
  ``alembic.runtime.migration.MigrationContext`` (proving the schema
  actually applied to *this* database, not merely what the repository's
  migration scripts claim); in development/test mode (no database to
  ask) it is read statically from the Alembic script directory instead
  — that is a deliberate, explicit fallback for the *version endpoint*
  only, unrelated to (and not in tension with) the composition root's
  "no fallback" invariant for repository/object-store selection
  (``app/api/composition.py``'s docstring) — reporting "what
  migration state the code on disk targets" is not "silently reading
  or writing canonical data".
* ``runtime_environment`` — the same value ``composition.py`` resolved,
  never read from the environment a second time here.
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from app.api.composition import REPO_ROOT, get_composition

router = APIRouter()

_VERSION_FILE = REPO_ROOT / "VERSION"
_ALEMBIC_INI = REPO_ROOT / "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = REPO_ROOT / "alembic"


def _read_build_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _static_migration_head() -> str | None:
    """The Alembic head revision as declared by the script directory on
    disk — no database connection involved (development/test mode)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))
    script = ScriptDirectory.from_config(cfg)
    return script.get_current_head()


def _live_migration_head(engine) -> str | None:
    """The Alembic revision actually recorded in the real database's
    ``alembic_version`` table — proves what schema state *this specific
    database* is at, not merely what the code on disk targets."""
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        return context.get_current_revision()


@router.get("/version")
async def version() -> dict:
    composition = get_composition()

    if composition.runtime_environment == "production":
        schema_migration_version = _live_migration_head(composition.engine)
    else:
        schema_migration_version = _static_migration_head()

    return {
        "git_commit": os.environ.get("BAGMAN_GIT_COMMIT", "unknown"),
        "build_version": _read_build_version(),
        "schema_migration_version": schema_migration_version,
        "runtime_environment": composition.runtime_environment,
    }
