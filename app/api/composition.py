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

from agent.claude_code.fake import FakeClaudeCodeOperatorRunner
from agent.claude_code.runner import ClaudeCodeOperatorRunner, ClaudeCodeOperatorRunnerProtocol
from ai.invocation import AIInvocationRepository
from ai.providers.litellm.client import LiteLLMClientProtocol
from core import actor
from core.api import BagmanCanonicalAPI
from persistence.objects.store import EvidenceObjectStore
from services.evidence.intake.intake import IntakeRepository
from services.evidence.intake.scanner import EvidenceSafetyScanner, ScanResult, ScanVerdict
from services.needs_you.needs_you import NeedsYouRepository
from services.xero.account import XeroAccountRepository
from services.xero.client import XeroAccountingClientProtocol, XeroOAuthClientProtocol
from services.xero.connection import XeroConnectionRepository
from services.xero.oauth_state import OAuthStateRepository
from services.xero.secrets import TokenStoreProtocol
from services.xero.sync import XeroSyncRunRepository

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


def _dev_mode_claude_code_default_response(system_prompt: str, user_prompt: str):
    """CD-5 Gate-2 closure dev-mode usability fix, mirroring
    `_dev_mode_litellm_default_response` immediately above exactly: a
    real click on Ask BAGMAN in a live dev server, with nothing
    pre-scripted on `FakeClaudeCodeOperatorRunner`, should still return
    a genuine, clearly-labelled `SUCCEEDED` result the GUI can render —
    never a 500, never a fabricated real answer.
    """
    from agent.claude_code.runner import ClaudeCodeInvocationResult, ClaudeCodeOutcomeStatus

    return ClaudeCodeInvocationResult(
        status=ClaudeCodeOutcomeStatus.OK,
        text=(
            "(dev-mode fake response — no real headless Claude Code invocation was made; "
            "this is composition's own default response for local development)"
        ),
        session_id="dev-mode-fake-session",
        model_usage={"fake-claude-code-dev-default-v1": {}},
        total_cost_usd=0.0,
        duration_ms=1,
        num_turns=1,
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
    #: CD-5 WI-2 (PID §26-27/§6/§8) — a single `AIInvocationRepository`
    #: shared by BOTH the `OPERATOR` role (Ask BAGMAN) and the
    #: `BACKGROUND` role (WI-2's own gateway) — WI-1 already built both
    #: the in-memory and PostgreSQL implementations. `litellm_client`
    #: is `ai.providers.litellm.client.LiteLLMClientProtocol`-shaped
    #: (fake in development/test, real adapter in production).
    ai_invocation_repository: AIInvocationRepository
    litellm_client: LiteLLMClientProtocol
    #: CD-5 Gate-2 closure (2026-09-16, PID §97) — the ONE, sole
    #: authoritative operator-intelligence seam
    #: `agent.claude_code.orchestrator.handle_operator_message` uses.
    #: Fake in development/test (PID §61 — no real subprocess in
    #: ordinary tests/dev server), the real `ClaudeCodeOperatorRunner`
    #: in production. Supersedes the CD-5 WI-3 direct-Anthropic-API
    #: design (`ai/providers/claude/`, `agent/tools/`,
    #: `agent/bagman/orchestrator.py`) — that implementation was
    #: proven to have zero remaining live dependents (CD-5 evidence
    #: file's own classification finding) and was removed entirely as
    #: part of this same closure, to prevent dual-authority ambiguity
    #: (the architect's own explicit instruction); it is not merely
    #: unwired here. See `PID.md` §97 and the evidence file for the
    #: full, preserved history of what it was and why it was
    #: superseded.
    claude_code_operator_runner: ClaudeCodeOperatorRunnerProtocol
    #: CD-6 Slice 1 (PID §98.5) — the universal Needs You queue
    #: repository. In-memory in development/test, a real
    #: `PostgresNeedsYouRepository` (sharing the same `engine` as every
    #: other Postgres-backed repository above) in production — never
    #: mixed across modes, same discipline as `intake_repository`.
    needs_you_repository: NeedsYouRepository
    #: CD-6 Slice 2 (PID §98.4, architect spec §1-24) — the Xero
    #: reference-data domain. In-memory in development/test, real
    #: `Postgres*` implementations (sharing `engine`) in production —
    #: same never-mixed-across-modes discipline as every repository
    #: above. `xero_oauth_client`/`xero_accounting_client` are the real
    #: `services.xero.client` adapters in production and
    #: `services.xero.fake_client`'s deterministic substitutes in
    #: development/test (this codebase's established `Fake*` pattern —
    #: see `ai_invocation_repository`/`litellm_client` above for the
    #: identical split). `xero_token_store` is `InMemoryTokenStore` in
    #: development/test (a real dev machine has no
    #: `/opt/bagman/secrets/xero/` directory at all) and `FileTokenStore`
    #: in production.
    xero_connection_repository: XeroConnectionRepository
    xero_account_repository: XeroAccountRepository
    xero_sync_run_repository: XeroSyncRunRepository
    oauth_state_repository: OAuthStateRepository
    xero_oauth_client: XeroOAuthClientProtocol
    xero_accounting_client: XeroAccountingClientProtocol
    xero_token_store: TokenStoreProtocol


def _build_development_or_test(runtime_environment: str) -> RuntimeComposition:
    from ai.invocation import InMemoryAIInvocationRepository
    from ai.providers.litellm.fake import FakeLiteLLMClient
    from persistence.objects.memory_store import InMemoryObjectStore
    from services.evidence.intake.intake import InMemoryIntakeRepository
    from services.needs_you.needs_you import InMemoryNeedsYouRepository
    from services.xero.account import InMemoryXeroAccountRepository
    from services.xero.connection import InMemoryXeroConnectionRepository
    from services.xero.fake_client import FakeXeroAccountingClient, FakeXeroOAuthClient
    from services.xero.oauth_state import InMemoryOAuthStateRepository
    from services.xero.secrets import InMemoryTokenStore
    from services.xero.sync import InMemoryXeroSyncRunRepository

    api = BagmanCanonicalAPI()  # CD-2's own in-memory default construction
    object_store = InMemoryObjectStore()
    scanner = _AlwaysCleanDevelopmentScanner()
    intake_repository = InMemoryIntakeRepository()
    needs_you_repository = InMemoryNeedsYouRepository()
    xero_connection_repository = InMemoryXeroConnectionRepository()
    xero_account_repository = InMemoryXeroAccountRepository()
    xero_sync_run_repository = InMemoryXeroSyncRunRepository()
    oauth_state_repository = InMemoryOAuthStateRepository()
    # CD-6 Slice 2: no real Xero Developer App exists yet (PID §102.1's
    # own stated constraint) — development/test composition ALWAYS uses
    # the deterministic fakes, never the real network-speaking adapters,
    # exactly like `litellm_client`/`claude_code_operator_runner` above.
    xero_oauth_client = FakeXeroOAuthClient()
    xero_accounting_client = FakeXeroAccountingClient()
    xero_token_store = InMemoryTokenStore()

    # CD-6 reliability delta: shares `api.audit_repository` so the
    # bounded stale-RUNNING recovery backstop's own audit events land in
    # the SAME in-memory audit trail every test/dev-mode caller already
    # reads via `api.audit_repository` — see `ai.invocation`'s module
    # docstring ("Stale-RUNNING recovery") and
    # `AIInvocationRepository`'s own class docstring.
    ai_invocation_repository = InMemoryAIInvocationRepository(audit_repository=api.audit_repository)
    # `default_response` closes the WI-4 dev-mode gap documented on
    # `_dev_mode_litellm_default_response` above — without it, a real
    # click on the GUI's "Run analysis" button in a live dev server
    # 500s immediately (nothing pre-scripted, no default). Ordinary
    # tests are unaffected: any test that wants a SPECIFIC scripted
    # outcome still calls `queue_success()`/`queue_failure()`, which
    # always takes priority over this default (see
    # `FakeLiteLLMClient.complete()`).
    litellm_client = FakeLiteLLMClient(default_response=_dev_mode_litellm_default_response)
    claude_code_operator_runner = FakeClaudeCodeOperatorRunner(
        default_response=_dev_mode_claude_code_default_response
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
        claude_code_operator_runner=claude_code_operator_runner,
        needs_you_repository=needs_you_repository,
        xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository,
        xero_sync_run_repository=xero_sync_run_repository,
        oauth_state_repository=oauth_state_repository,
        xero_oauth_client=xero_oauth_client,
        xero_accounting_client=xero_accounting_client,
        xero_token_store=xero_token_store,
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
    from persistence.postgres.needs_you_repository import PostgresNeedsYouRepository
    from persistence.postgres.provenance_repository import PostgresProvenanceRepository
    from persistence.postgres.session import get_engine
    from persistence.postgres.source_repository import PostgresSourceRepository
    from persistence.postgres.xero_repository import (
        PostgresOAuthStateRepository,
        PostgresXeroAccountRepository,
        PostgresXeroConnectionRepository,
        PostgresXeroSyncRunRepository,
    )
    from services.evidence.intake.scanner import ClamAVScanner
    from services.xero.client import XeroAccountingClient, XeroOAuthClient
    from services.xero.secrets import FileTokenStore

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

    # CD-6 Slice 1 (PID §98.5): durable Needs You repository, sharing
    # the same engine as every other Postgres-backed repository above.
    needs_you_repository = PostgresNeedsYouRepository(engine)

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
    # CD-6 reliability delta: shares `api.audit_repository`
    # (`PostgresAuditRepository(engine)`, built above) so the bounded
    # stale-RUNNING recovery backstop's own audit events land in the
    # SAME `audit_events` table every other production audit event
    # already writes to — see `ai.invocation`'s module docstring
    # ("Stale-RUNNING recovery") and `AIInvocationRepository`'s own
    # class docstring.
    ai_invocation_repository = PostgresAIInvocationRepository(engine, audit_repository=api.audit_repository)
    litellm_client = LiteLLMClient(
        endpoint=os.environ.get("BAGMAN_LITELLM_ENDPOINT", DEFAULT_LITELLM_ENDPOINT),
        api_key_file=os.environ.get("BAGMAN_LITELLM_API_KEY_FILE", DEFAULT_LITELLM_API_KEY_FILE),
    )

    # CD-5 Gate-2 closure (2026-09-16, PID §97): the real bounded
    # headless Claude Code operator runner — the ONE, sole
    # authoritative operator-intelligence seam. Constructing this
    # performs NO I/O itself (same "no eager I/O at construction"
    # discipline as every other adapter in this function) —
    # reachability (`shutil.which("claude")`) is proven live by
    # `is_available()`, never here; a real invocation is only ever
    # attempted inside a real Ask BAGMAN request.
    claude_code_operator_runner = ClaudeCodeOperatorRunner()

    # CD-6 Slice 2 (PID §98.4, architect spec §1-24): durable Xero
    # repositories, sharing the same engine as every other Postgres-
    # backed repository above, plus the two real adapters
    # (`XeroOAuthClient`/`XeroAccountingClient`) and the real
    # file-backed per-connection token store. Constructing any of these
    # performs NO I/O itself (same "no eager I/O at construction"
    # discipline as every other adapter in this function) — a missing
    # Xero Developer App credential (no real one is provisioned yet,
    # PID §102.1's own stated constraint) surfaces as a live, per-call
    # `CONFIG_ERROR`/`XeroAppCredentials is None` outcome the first time
    # a real OAuth call is attempted, never at composition/startup time.
    xero_connection_repository = PostgresXeroConnectionRepository(engine)
    xero_account_repository = PostgresXeroAccountRepository(engine)
    xero_sync_run_repository = PostgresXeroSyncRunRepository(engine)
    oauth_state_repository = PostgresOAuthStateRepository(engine)
    xero_oauth_client = XeroOAuthClient()
    xero_accounting_client = XeroAccountingClient()
    xero_token_store = FileTokenStore()

    return RuntimeComposition(
        runtime_environment=_PRODUCTION,
        api=api,
        object_store=object_store,
        engine=engine,
        intake_repository=intake_repository,
        scanner=scanner,
        ai_invocation_repository=ai_invocation_repository,
        litellm_client=litellm_client,
        claude_code_operator_runner=claude_code_operator_runner,
        needs_you_repository=needs_you_repository,
        xero_connection_repository=xero_connection_repository,
        xero_account_repository=xero_account_repository,
        xero_sync_run_repository=xero_sync_run_repository,
        oauth_state_repository=oauth_state_repository,
        xero_oauth_client=xero_oauth_client,
        xero_accounting_client=xero_accounting_client,
        xero_token_store=xero_token_store,
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
    memoized ``MANUAL_UPLOAD`` source id (CD-4 WI-3) and the memoized
    canonical-entity seed ids (CD-6 Slice 1) — either, memoized against
    one composition (e.g. a prior test's disposable Postgres database),
    would otherwise be silently stale/invalid against the NEXT
    composition this process builds."""
    global _composition, _manual_upload_source_id, _seed_entity_ids
    with _lock:
        _composition = None
        _manual_upload_source_id = None
        _seed_entity_ids = None


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


# ---------------------------------------------------------------------
# Stable canonical entity seed lifecycle (CD-6 Slice 1, PID §98.3)
# ---------------------------------------------------------------------
#
# PID §98.3: "Select from canonical BAGMAN entities. Initial expected
# entities: Infosecurs Limited, NoustAI Limited, Matthew Scott Personal
# ... These labels must not be hardcoded as business truth in the UI.
# They map to canonical entity IDs." CD-6's own dispatch is explicit
# that the seed mechanism should "follow the exact pattern already used
# for get_manual_upload_source_id/_MANUAL_UPLOAD_SOURCE_TYPE" above —
# this section is that same lifecycle, applied to three well-known
# GovernedEntity rows instead of one well-known Source row:
#
#   1. an in-process memoized {canonical_name: entity_id} dict (fast
#      path, no query at all once warm) — mirrors
#      _manual_upload_source_id's own cache;
#   2. for each of the three canonical names, a read-only
#      find_by_canonical_name() lookup (the entity may already exist —
#      created by an earlier process, or an earlier request in this
#      same process before the memoized value was set);
#   3. only if not found, register_entity() creates it.
#
# `entity_type` values: "COMPANY" for the two Limiteds, "PERSON" for
# the personal entity — both are the contract's own DOCUMENTED (open,
# non-enum-enforced) "known initial values" for entity_type
# (contracts/entity/bagman.entity.v1.schema.json's own description:
# "Known initial values (documentation only, not an enforced closed
# set): COMPANY, PERSON"), not new ad hoc types invented for this
# delivery.
#
# Trigger point: resolved lazily the first time GET /internal/entities
# is called (app/api/routers/internal.py) — never inside
# get_composition() itself, consistent with this module's own
# documented "no eager PostgreSQL I/O at composition-build time"
# invariant (see this module's top-of-file docstring: constructing the
# PostgreSQL-backed repositories never itself contacts the database).
# A GUI that has not yet loaded the entity dropdown simply has not
# triggered the seed yet — exactly the same lazy-resolution shape
# get_manual_upload_source_id has always had for its first caller
# (POST /internal/intake/evidence).
#
# Known limitation, stated plainly rather than silently accepted — same
# class of gap _MANUAL_UPLOAD_SOURCE_TYPE's own docstring already
# accepts for CD-4, for the identical reason: no database-level
# uniqueness constraint on governed_entities.canonical_name backs this
# (unlike needs_you_items' or intake_records' own real partial unique
# indexes), so a genuine multi-PROCESS cold-start race could each
# independently create a duplicate canonical-name row. Acceptable for
# CD-6 Slice 1 for the same reason it was acceptable for CD-4's
# MANUAL_UPLOAD source: BAGMAN runs as a single bagman-api process/
# container (no multi-replica deployment exists yet), so the only race
# that matters in practice is intra-process, fully closed by the
# module-level lock below. Flagged here for whoever introduces
# multi-replica bagman-api, not solved speculatively now.
_SEED_ENTITY_TYPE_COMPANY = "COMPANY"
_SEED_ENTITY_TYPE_PERSON = "PERSON"

#: (canonical_name, display_name, entity_type) — PID §98.3's own three
#: "initial expected entities", in the order the GUI should offer them.
SEED_ENTITIES: tuple[tuple[str, str, str], ...] = (
    ("INFOSECURS_LIMITED", "Infosecurs Limited", _SEED_ENTITY_TYPE_COMPANY),
    ("NOUSTAI_LIMITED", "NoustAI Limited", _SEED_ENTITY_TYPE_COMPANY),
    ("MATTHEW_SCOTT_PERSONAL", "Matthew Scott Personal", _SEED_ENTITY_TYPE_PERSON),
)

_seed_entity_ids: Optional[dict[str, str]] = None


def ensure_seed_entities(composition: "RuntimeComposition") -> dict[str, str]:
    """Resolve (or, on first use, create) the three canonical
    :data:`SEED_ENTITIES` rows (see module section docstring above).
    Returns ``{canonical_name: entity_id}``, memoized for the life of
    the process once every entity has been resolved."""
    global _seed_entity_ids
    if _seed_entity_ids is not None:
        return _seed_entity_ids

    with _lock:
        if _seed_entity_ids is not None:
            return _seed_entity_ids

        resolved: dict[str, str] = {}
        for canonical_name, display_name, entity_type in SEED_ENTITIES:
            existing = composition.api.entity_repository.find_by_canonical_name(canonical_name)
            if existing is not None:
                resolved[canonical_name] = existing.entity_id
                continue

            entity = composition.api.register_entity(
                entity_type=entity_type,
                canonical_name=canonical_name,
                display_name=display_name,
                status="ACTIVE",
                actor_type=actor.SYSTEM,
                actor_id="bagman-entity-seed-bootstrap",
                metadata={
                    "note": "canonical entity seed, resolved-or-created once per process "
                    "(CD-6 Slice 1, PID §98.3) — never re-created on a later call"
                },
            )
            resolved[canonical_name] = entity.entity_id

        _seed_entity_ids = resolved
        return _seed_entity_ids
