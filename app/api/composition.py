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

import functools
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from agent.tools.background import BackgroundTaskRunner, DeterministicFakeBackgroundTaskRunner
from agent.tools.handlers import ToolDependencies, build_default_tool_registry
from agent.tools.registry import ToolRegistry
from ai.invocation import AIInvocationRepository
from ai.providers.claude.client import ClaudeClientProtocol
from ai.providers.claude.fake import FakeClaudeClient
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


def _dev_mode_litellm_default_response(system_instructions: str) -> "LiteLLMCompletionResult":
    """`FakeLiteLLMClient`'s `default_response` for development/test
    composition (CD-5 WI-4 gap closure).

    Without this, `BAGMAN_RUNTIME_ENV=development uvicorn app.api.main:app`
    plus a real click on the GUI's "Run analysis" button would 500
    immediately: `FakeLiteLLMClient.complete()` raises `AssertionError`
    when nothing was pre-scripted and no `default_response` exists (see
    that module's own docstring) — there was previously no interactive/
    manual way to see a background-task result without pre-scripting one
    via a test harness, which does not exist in a live dev server.

    Matches `agent/tools/background.py`'s own
    `DeterministicFakeBackgroundTaskRunner._canned_output_for` in spirit
    exactly: a small, fixed, structurally-valid-per-task canned output,
    never phrased to look like a plausible real model answer (PID §61).
    Since `FakeLiteLLMClient.complete()` hands `default_response` the
    exact `system_instructions` string it was called with (rather than
    a bare zero-arg factory), this can determine which of the three
    CD-5 background tasks is actually running from that text — each
    task's prompt asset names its own `task_id` verbatim (e.g. "task
    DOCUMENT_SUMMARY" — see `ai/prompts/*/v1.md`) — and return an
    output shaped to match THAT task's own `output_schema`, so a
    genuine `SUCCEEDED` result renders in the GUI rather than an
    `OUTPUT_SCHEMA_INVALID` failure caused merely by guessing wrong.
    """
    import json as _json

    from ai.providers.litellm.client import LiteLLMCompletionResult, LiteLLMOutcomeStatus

    _dev_warning = (
        "(dev-mode fake response — not a real model result; no live LiteLLM/Mac-mini/"
        "Trinity call was made)"
    )
    if "task DOCUMENT_TYPE_PROPOSAL" in system_instructions:
        content: dict = {
            "proposed_type": "UNKNOWN",
            "confidence": 0.0,
            "signals": [],
            "warnings": [_dev_warning],
        }
    elif "task DOCUMENT_SUMMARY" in system_instructions:
        content = {
            "summary": f"{_dev_warning} — no real document summary was generated.",
            "confidence": 0.0,
            "signals": [],
            "warnings": [_dev_warning],
        }
    elif "task ENTITY_PROPOSAL" in system_instructions:
        content = {
            "proposed_entity_hint": None,
            "confidence": 0.0,
            "signals": [],
            "warnings": [_dev_warning],
        }
    else:
        # No CD-5 BACKGROUND task registered today falls outside the
        # three branches above (see ai/tasks.py::TASK_REGISTRY) — this
        # is a defensive fallback only, for a future task this dev-mode
        # default has not been taught about yet. It will legitimately
        # fail that task's own output_schema validation (an honest,
        # visible FAILED/OUTPUT_SCHEMA_INVALID state, PID §76), not a
        # crash — never silently fabricated as a false SUCCEEDED.
        content = {"warnings": [_dev_warning, "unrecognised task — dev-mode default has no shape for it"]}

    return LiteLLMCompletionResult(
        status=LiteLLMOutcomeStatus.OK,
        content=_json.dumps(content),
        provider_model="fake-litellm-dev-default-v1 (composition dev-mode default — not a real model)",
        usage_metadata={},
        latency_ms=1,
    )


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
    #: CD-5 WI-2/WI-3 (PID §26-27/§6/§8) — a single
    #: `AIInvocationRepository` shared by BOTH the `OPERATOR` role (Ask
    #: BAGMAN, WI-3) and the `BACKGROUND` role (WI-2's own gateway) —
    #: WI-1 already built both the in-memory and PostgreSQL
    #: implementations; WI-2/WI-3 each wire it into `RuntimeComposition`.
    #: `litellm_client` (WI-2) is `ai.providers.litellm.client
    #: .LiteLLMClientProtocol`-shaped. `claude_client` (WI-3) is
    #: `ai.providers.claude.client.ClaudeClientProtocol`-shaped. Both
    #: follow the same dev/test-vs-production pairing as every field
    #: above (fake in development/test, real adapter in production).
    #: `tool_registry` (WI-3) is the fixed, closed
    #: `agent.tools.registry.ToolRegistry` Claude may invoke (PID §34) —
    #: identical construction in every `runtime_environment` (the tools
    #: themselves are never environment-conditional; only the
    #: `background_task_runner` dependency they close over differs — see
    #: `_ProductionBackgroundTaskRunner` below, the PL reconciliation
    #: that wires it to WI-2's real gateway in production).
    ai_invocation_repository: AIInvocationRepository
    litellm_client: LiteLLMClientProtocol
    claude_client: ClaudeClientProtocol
    tool_registry: ToolRegistry


def _build_development_or_test(runtime_environment: str) -> RuntimeComposition:
    from ai.invocation import InMemoryAIInvocationRepository
    from ai.providers.litellm.fake import FakeLiteLLMClient
    from persistence.objects.memory_store import InMemoryObjectStore
    from services.evidence.intake.intake import InMemoryIntakeRepository

    api = BagmanCanonicalAPI()  # CD-2's own in-memory default construction
    object_store = InMemoryObjectStore()
    scanner = _AlwaysCleanDevelopmentScanner()
    intake_repository = InMemoryIntakeRepository()

    ai_invocation_repository = InMemoryAIInvocationRepository()
    # `default_response` closes the WI-4 dev-mode gap documented on
    # `_dev_mode_litellm_default_response` above — without it, a real
    # click on the GUI's "Run analysis" button in a live dev server
    # 500s immediately (nothing pre-scripted, no default). Ordinary
    # tests are unaffected: any test that wants a SPECIFIC scripted
    # outcome still calls `queue_success()`/`queue_failure()`, which
    # always takes priority over this default (see
    # `FakeLiteLLMClient.complete()`).
    litellm_client = FakeLiteLLMClient(default_response=_dev_mode_litellm_default_response)
    claude_client = FakeClaudeClient()
    tool_registry = _build_tool_registry(
        api=api,
        intake_repository=intake_repository,
        ai_invocation_repository=ai_invocation_repository,
        runtime_environment=runtime_environment,
        engine=None,
        object_store=object_store,
        scanner=scanner,
        claude_client=claude_client,
        litellm_client=litellm_client,
    )

    return RuntimeComposition(
        runtime_environment=runtime_environment,
        api=api,
        object_store=object_store,
        engine=None,
        intake_repository=intake_repository,
        scanner=scanner,
        ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client,
        claude_client=claude_client,
        tool_registry=tool_registry,
    )


def _build_production() -> RuntimeComposition:
    # Imported lazily so `import app.api.composition` alone never
    # requires boto3/psycopg to be installed for a pure development/test
    # run — mirrors how `persistence/objects/minio_store.py` is only
    # ever imported by something that actually needs MinIO.
    from persistence.objects.minio_store import MinIOConfig, MinIOObjectStore
    from persistence.postgres.ai_invocation_repository import PostgresAIInvocationRepository
    from persistence.postgres.audit_repository import PostgresAuditRepository
    from persistence.postgres.entity_repository import PostgresEntityRepository
    from persistence.postgres.evidence_repository import PostgresEvidenceRepository
    from persistence.postgres.external_reference_repository import (
        PostgresExternalReferenceRepository,
    )
    from persistence.postgres.intake_repository import PostgresIntakeRepository
    from persistence.postgres.provenance_repository import PostgresProvenanceRepository
    from persistence.postgres.session import get_engine
    from persistence.postgres.source_repository import PostgresSourceRepository
    from services.evidence.intake.scanner import ClamAVScanner

    from ai.providers.litellm.client import DEFAULT_LITELLM_API_KEY_FILE, DEFAULT_LITELLM_ENDPOINT, LiteLLMClient
    # CD-5 WI-3: imported lazily here too — `requests` (ClaudeClient's
    # only real dependency) is already a base BAGMAN dependency, but
    # keeping this import inside the production-only builder matches
    # this function's existing "nothing production-only loads at plain
    # `import app.api.composition` time" discipline.
    from ai.providers.claude.client import ClaudeClient
    from ai.gateway.background import run_background_task as _real_run_background_task

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

    # CD-5 WI-3: the real Anthropic adapter. Constructing this performs
    # NO I/O itself (mirrors ClamAVScanner above) — it only stores
    # config; reachability is proven live by `is_available()`
    # (`get_runtime_status_summary` below / `GET /internal/ai/health`),
    # never here.
    claude_client = ClaudeClient()

    # PL RECONCILIATION (WI-2 and WI-3 were dispatched in parallel;
    # neither could see the other's worktree): `run_background_analysis`
    # now dispatches through WI-2's REAL gateway function in production
    # composition via `_ProductionBackgroundTaskRunner` below, replacing
    # WI-3's own `DeterministicFakeBackgroundTaskRunner` placeholder for
    # this one `runtime_environment` only — development/test composition
    # keeps using the deterministic fake unconditionally (PID §61; see
    # `_build_development_or_test` above), exactly as WI-3's own
    # `agent/tools/background.py` docstring anticipated this seam.
    background_task_runner = _ProductionBackgroundTaskRunner(
        api=api,
        object_store=object_store,
        ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client,
        run_background_task=_real_run_background_task,
    )
    tool_registry = _build_tool_registry(
        api=api,
        intake_repository=intake_repository,
        ai_invocation_repository=ai_invocation_repository,
        runtime_environment=_PRODUCTION,
        engine=engine,
        object_store=object_store,
        scanner=scanner,
        claude_client=claude_client,
        litellm_client=litellm_client,
        background_task_runner=background_task_runner,
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
        claude_client=claude_client,
        tool_registry=tool_registry,
    )


# ---------------------------------------------------------------------
# CD-5 WI-3 — agent/tools wiring + runtime status summary
# ---------------------------------------------------------------------
#
# A fixed, well-formed, never-actually-registered object-storage key
# used purely to prove the object store is reachable — the exact same
# probe key `app/api/routers/health.py`'s `/ready` already uses (kept
# as a second, deliberately duplicated constant rather than imported
# from that module, so this file never depends on `app/api/routers/`;
# see `get_runtime_status_summary`'s own docstring for why its checks
# are duplicated rather than shared with `/ready` outright).
_READINESS_PROBE_EVIDENCE_ID = "00000000-0000-0000-0000-000000000000"
_READINESS_PROBE_HASH = "0" * 64


def get_runtime_status_summary(
    *,
    runtime_environment: str,
    engine: Optional[Engine],
    object_store: EvidenceObjectStore,
    scanner: EvidenceSafetyScanner,
    claude_client: ClaudeClientProtocol,
    litellm_client: LiteLLMClientProtocol,
) -> Mapping[str, Any]:
    """Read-only runtime status snapshot for `agent.tools`'
    `get_runtime_status` tool (PID §34/§46-48). `GET /internal/ai/health`
    (`app/api/routers/ai.py`, WI-2) is the separate, per-alias-granular
    HTTP surface for the background tier specifically — this summary is
    a coarser, tool-facing view spanning ALL of core runtime + every AI
    tier (PID §47's "Matt must be able to distinguish these different
    failure classes" applies to Claude's own `get_runtime_status` answer
    just as much as to the GUI).

    Deliberately never raises — every check is independently wrapped so
    a caller always gets a full, structured snapshot back even if every
    dependency is completely unreachable (a status TOOL must never
    itself become the thing that crashes the Ask BAGMAN conversation
    asking about status).

    Mirrors, rather than calls directly, the same three checks
    `app/api/routers/health.py`'s `/ready` performs in production mode
    (PostgreSQL `SELECT 1`, object-store `exists()`, scanner
    `is_available()`) — a deliberate, documented WI-3 judgment call: a
    genuine shared-helper refactor of `/ready` itself was judged
    higher-risk than its value for this WI (touching a small, already
    covered-by-tests, production-critical readiness route to shave one
    duplicated 3-check block), so the two call sites independently
    perform conceptually the same checks rather than sharing one
    function. Flagged for the PL/a future WI to consolidate if desired.
    """
    checks: dict[str, str] = {}

    if runtime_environment != _PRODUCTION:
        checks["postgres"] = "not_applicable (in-memory composition)"
        checks["object_store"] = "not_applicable (in-memory composition)"
        checks["scanner"] = "not_applicable (development stub scanner)"
    else:
        try:
            assert engine is not None
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception:  # noqa: BLE001 - a status probe must never raise
            checks["postgres"] = "unreachable"

        try:
            object_store.exists(f"evidence/{_READINESS_PROBE_EVIDENCE_ID}/{_READINESS_PROBE_HASH}")
            checks["object_store"] = "ok"
        except Exception:  # noqa: BLE001
            checks["object_store"] = "unreachable"

        try:
            checks["scanner"] = "ok" if scanner.is_available() else "unreachable"
        except Exception:  # noqa: BLE001
            checks["scanner"] = "unreachable"

    try:
        checks["claude_operator"] = "ok" if claude_client.is_available() else "unreachable"
    except Exception:  # noqa: BLE001
        checks["claude_operator"] = "unreachable"

    try:
        # Gateway-wide only (PID §9's three background aliases all
        # share one LiteLLM installation) — see
        # `ai.providers.litellm.client.LiteLLMClient.is_available`'s own
        # docstring for why a genuinely per-alias check is not possible
        # today.
        checks["litellm_background_gateway"] = "ok" if litellm_client.is_available() else "unreachable"
    except Exception:  # noqa: BLE001
        checks["litellm_background_gateway"] = "unreachable"

    return {"runtime_environment": runtime_environment, "checks": checks}


class _ProductionBackgroundTaskRunner:
    """PL reconciliation (WI-2 and WI-3 were dispatched in parallel;
    neither worktree could see the other's code): the production
    `agent.tools.background.BackgroundTaskRunner` implementation,
    wiring `run_background_analysis` (Claude's tool, WI-3) to WI-2's
    REAL `ai.gateway.background.run_background_task` orchestration
    instead of WI-3's own `DeterministicFakeBackgroundTaskRunner`
    placeholder — used only by `_build_production` below;
    `_build_development_or_test` keeps the deterministic fake
    unconditionally (PID §61).

    Evidence-content resolution deliberately duplicates (a small
    amount of) the same logic `app/api/routers/ai.py`'s own
    `_resolve_evidence_content` helper already implements for the HTTP
    path — refactoring that already-tested function to be shared was
    judged out of scope for this reconciliation; both must be kept in
    sync if evidence-content resolution ever changes.
    """

    def __init__(
        self,
        *,
        api: BagmanCanonicalAPI,
        object_store: EvidenceObjectStore,
        ai_invocation_repository: AIInvocationRepository,
        litellm_client: LiteLLMClientProtocol,
        run_background_task: Any,
    ) -> None:
        self._api = api
        self._object_store = object_store
        self._repository = ai_invocation_repository
        self._litellm_client = litellm_client
        self._run_background_task = run_background_task

    def _resolve_evidence_content(self, input_references: Mapping[str, Any]) -> str:
        evidence_id = input_references.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            return ""
        evidence = self._api.get_evidence(evidence_id)
        if not evidence.storage_reference:
            return ""
        raw_bytes = self._object_store.get(evidence.storage_reference)
        return raw_bytes.decode("utf-8", errors="replace")

    def run_background_task(
        self,
        *,
        task_id: str,
        task_version: int,
        input_references: Mapping[str, Any],
        actor_type: str,
        actor_id: str,
        correlation_id: Optional[str],
    ):
        evidence_content = self._resolve_evidence_content(input_references)
        return self._run_background_task(
            task_id=task_id,
            task_version=task_version,
            input_references=input_references,
            evidence_content=evidence_content,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
            repository=self._repository,
            litellm_client=self._litellm_client,
            record_audit_event=self._api.record_audit_event,
        )


def _build_tool_registry(
    *,
    api: BagmanCanonicalAPI,
    intake_repository: IntakeRepository,
    ai_invocation_repository: AIInvocationRepository,
    runtime_environment: str,
    engine: Optional[Engine],
    object_store: EvidenceObjectStore,
    scanner: EvidenceSafetyScanner,
    claude_client: ClaudeClientProtocol,
    litellm_client: LiteLLMClientProtocol,
    background_task_runner: Optional[BackgroundTaskRunner] = None,
) -> ToolRegistry:
    """Build the fixed, closed `agent.tools.registry.ToolRegistry`
    (PID §34) — identical construction regardless of
    `runtime_environment` (see `RuntimeComposition.tool_registry`'s own
    docstring); only `get_runtime_status_summary`'s OWN behaviour
    branches on `runtime_environment` internally.

    `background_task_runner` defaults to
    `DeterministicFakeBackgroundTaskRunner` (development/test
    composition, PID §61 — ordinary tests never depend on a live
    LiteLLM/Mac-mini/Trinity call) when the caller does not supply one.
    Production composition (`_build_production` below) passes a real
    `_ProductionBackgroundTaskRunner`, wired to WI-2's actual
    `ai.gateway.background.run_background_task` — the PL reconciliation
    of the WI-2/WI-3 parallel-dispatch seam `agent/tools/background.py`
    itself documents.
    """
    if background_task_runner is None:
        background_task_runner = DeterministicFakeBackgroundTaskRunner(ai_invocation_repository)
    deps = ToolDependencies(
        api=api,
        intake_repository=intake_repository,
        ai_invocation_repository=ai_invocation_repository,
        background_task_runner=background_task_runner,
        runtime_health_check=functools.partial(
            get_runtime_status_summary,
            runtime_environment=runtime_environment,
            engine=engine,
            object_store=object_store,
            scanner=scanner,
            claude_client=claude_client,
            litellm_client=litellm_client,
        ),
    )
    return build_default_tool_registry(deps)


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
