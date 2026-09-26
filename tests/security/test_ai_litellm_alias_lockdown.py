"""Architecture/security proofs for CD-5 WI-2's alias-only routing
boundary (PID §5/§9/§10/§77, Matt's explicit requirement per this WI's
own dispatch).

Static/import-based where possible (mirrors
`tests/integration/test_architecture_boundaries.py`'s own discipline —
`ast`, not a regex, for source inspection), plus direct behavioural
proofs against the real closed-set constants/validators themselves.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ai.invocation import BACKGROUND_CAPABILITY_ALIASES
from ai.providers.litellm.client import validate_capability_alias
from ai.providers.litellm.fake import FakeLiteLLMClient
from app.api.routers.ai import RunBackgroundTaskRequest
from core.errors import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Every `trinity-*` alias — used only by the narrow, WI-2-scoped
#: "no literal in THESE files" check below
#: (`test_no_trinity_star_alias_literal_in_this_wis_new_files`), which
#: still forbids ALL of them (including `trinity-core`) since none of
#: `ai/providers`/`ai/gateway`/`ai/prompts`/`app/api/routers/ai.py`
#: ever hardcodes a Trinity alias literal — they operate generically
#: via `ai.invocation.BACKGROUND_CAPABILITY_ALIASES`/a caller-supplied
#: variable, never a literal string.
_TRINITY_STAR_ALIASES = ["trinity-fast", "trinity-core", "trinity-deep", "trinity-embed"]

#: CD-6 §103 Inference Architecture Ruling: `trinity-core` is now the
#: SOLE authorised Trinity alias — no longer forbidden. Every OTHER
#: `trinity-*` alias remains rejected by `validate_capability_alias`
#: (used by the parametrized rejection tests below, which must NOT
#: include `trinity-core` any more — see
#: `test_trinity_core_is_now_accepted_not_rejected` for the positive
#: proof of the opposite).
_STILL_FORBIDDEN_TRINITY_ALIASES = ["trinity-fast", "trinity-deep", "trinity-embed"]
_RAW_MODEL_NAMES = ["gemma-3-12b", "llama3.1:8b", "gpt-4o", "claude-3-5-sonnet-20241022"]


# ---------------------------------------------------------------------
# 1. the HTTP request shape carries no model/alias/provider override
# ---------------------------------------------------------------------


def test_run_background_task_request_has_no_field_naming_a_model_provider_or_alias():
    """PID §77: 'no prompt-controlled provider/model/alias name'.
    `task_id` must be the ONLY routing input on this surface."""
    field_names = set(RunBackgroundTaskRequest.model_fields.keys())
    forbidden_substrings = ("model", "alias", "provider", "backend", "capability")
    suspicious = [
        name for name in field_names if any(token in name.lower() for token in forbidden_substrings)
    ]
    assert suspicious == [], (
        f"POST /internal/ai/tasks request model has a field that could let a caller select a "
        f"model/alias/provider directly: {suspicious} — routing must be decided ENTIRELY by "
        f"task_id via ai.tasks.TaskContract.preferred_capability (PID §77)"
    )
    assert field_names == {
        "task_id",
        "task_version",
        "input_references",
        "actor_type",
        "actor_id",
        "correlation_id",
    }


def test_run_background_task_request_has_no_field_naming_a_schema_or_response_format():
    """CD-5 Gate-1 closure delta (2026-09-16): `output_schema` is now
    threaded through to the provider, exactly like `preferred_capability`
    already was — this must be sourced ENTIRELY from
    `ai.tasks.TaskContract.output_schema` (resolved server-side from
    `task_id`), never from anything a caller supplies on this HTTP
    surface. Same closed-field-set discipline as the alias/model/
    provider check above, extended to cover this new provider-call
    parameter."""
    field_names = set(RunBackgroundTaskRequest.model_fields.keys())
    forbidden_substrings = ("schema", "format", "output_schema", "response_format")
    suspicious = [
        name for name in field_names if any(token in name.lower() for token in forbidden_substrings)
    ]
    assert suspicious == [], (
        f"POST /internal/ai/tasks request model has a field that could let a caller inject/"
        f"override the structured-output schema directly: {suspicious} — the schema must come "
        "ENTIRELY from the resolved TaskContract.output_schema (PID §21-22)"
    )


# ---------------------------------------------------------------------
# 2. the client's own alias validation rejects every forbidden value
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_alias", _STILL_FORBIDDEN_TRINITY_ALIASES + _RAW_MODEL_NAMES)
def test_validate_capability_alias_rejects_trinity_star_and_raw_model_names(bad_alias):
    with pytest.raises(ValidationError):
        validate_capability_alias(bad_alias)


def test_trinity_core_is_now_accepted_not_rejected():
    """CD-6 §103: `trinity-core` is the sole authorised Trinity alias —
    the positive counterpart to the parametrized rejection test above,
    which must no longer include it."""
    validate_capability_alias("trinity-core")  # must not raise


@pytest.mark.parametrize("bad_alias", _STILL_FORBIDDEN_TRINITY_ALIASES + _RAW_MODEL_NAMES)
def test_fake_client_also_rejects_the_same_forbidden_values(bad_alias):
    """The fake client used by every ordinary test/dev composition must
    never be more permissive than the real one — both call the same
    shared `validate_capability_alias`."""
    fake = FakeLiteLLMClient()
    with pytest.raises(ValidationError):
        fake.complete(
            capability_alias=bad_alias,
            system_instructions="irrelevant",
            evidence_content="irrelevant",
            output_schema={"type": "object"},
            timeout_seconds=1.0,
        )


def test_validate_capability_alias_uses_the_single_source_of_truth_constant():
    """The validator must accept EXACTLY `BACKGROUND_CAPABILITY_ALIASES`
    — no hand-maintained parallel list that could silently drift."""
    for alias in BACKGROUND_CAPABILITY_ALIASES:
        validate_capability_alias(alias)  # must not raise
    assert BACKGROUND_CAPABILITY_ALIASES == frozenset({"bagman-fast", "bagman-core", "trinity-core"})


# ---------------------------------------------------------------------
# 3. provider_model is never read for a routing decision (structural)
# ---------------------------------------------------------------------


def test_provider_model_is_never_read_in_the_background_gateway_routing_path():
    """PID §10/§24: `provider_model` is AUDIT-ONLY provenance. This
    inspects `ai/gateway/background.py`'s actual source via `ast` and
    asserts `provider_model` is only ever WRITTEN to (as a keyword
    argument value coming FROM the provider result), never READ as an
    attribute access that could feed a branch/decision."""
    path = REPO_ROOT / "ai" / "gateway" / "background.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    read_accesses = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "provider_model":
            # A keyword like `provider_model=result.provider_model` is
            # an Attribute access on `result` (the provider's own
            # response) being used to POPULATE a field — legitimate
            # (audit-only storage). What must never appear is
            # `invocation.provider_model` (reading it back OFF the
            # invocation to make a decision) or any conditional
            # (`ast.If`/`ast.IfExp`) whose test contains this attribute.
            parent_value = getattr(node.value, "id", None)
            if parent_value == "invocation":
                read_accesses.append(node.lineno)

    assert read_accesses == [], (
        f"ai/gateway/background.py reads invocation.provider_model at line(s) "
        f"{read_accesses} — provider_model must never be read to make a routing decision "
        "(PID §10/§24), only ever written as audit-only provenance"
    )

    # Also confirm no conditional anywhere in the file branches on
    # `provider_model` at all (belt-and-braces beyond the attribute-owner
    # check above).
    conditional_nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.If, ast.IfExp))]
    for node in conditional_nodes:
        test_source = ast.dump(node.test)
        assert "provider_model" not in test_source, (
            f"a conditional at line {node.lineno} branches on provider_model — forbidden (PID §10/§24)"
        )


def test_run_background_task_capability_alias_comes_only_from_task_contract_or_explicit_override():
    """Behavioural proof (CD-6 §103 replaced the old pure source-text
    match here: `capability_alias_override`/`inference_backend` are new
    optional keyword-only parameters on `run_background_task` — see
    `ai/gateway/background.py`'s own module docstring's "Backend-
    override parameters" section — so a literal
    `capability_alias=task_contract.preferred_capability` substring no
    longer appears verbatim in the source).

    Proves BOTH halves of the invariant this test's own name (and PID
    §77's "no prompt-controlled provider/model/alias name") actually
    cares about: (1) the two EXISTING callers, which never pass
    `capability_alias_override`, still get EXACTLY
    `task_contract.preferred_capability` — never any other value; (2)
    the override, when explicitly supplied (only
    `scripts.process_background_job_overflow` does this today), is used
    verbatim — never silently ignored or re-derived a second way."""
    from ai.invocation import InMemoryAIInvocationRepository
    from ai.gateway.background import run_background_task
    from ai.providers.litellm.fake import FakeLiteLLMClient
    from ai.tasks import get_task_contract
    from core.audit import InMemoryAuditRepository

    task_id, task_version = "DOCUMENT_TYPE_PROPOSAL", 1
    task_contract = get_task_contract(task_id, task_version)
    audit = InMemoryAuditRepository()

    def _run(*, capability_alias_override=None, inference_backend="MAC_LOCAL", evidence_id):
        repo = InMemoryAIInvocationRepository(audit_repository=audit)
        fake = FakeLiteLLMClient()
        expected_alias = capability_alias_override or task_contract.preferred_capability
        fake.queue_success(
            capability_alias=expected_alias,
            content='{"proposed_type": "INVOICE", "confidence": 0.5, "signals": [], "warnings": []}',
        )
        return run_background_task(
            task_id=task_id,
            task_version=task_version,
            input_references={"evidence_id": evidence_id},
            evidence_content="irrelevant",
            actor_type="SYSTEM",
            actor_id="test-harness",
            correlation_id=None,
            repository=repo,
            litellm_client=fake,
            record_audit_event=audit.record_audit_event,
            capability_alias_override=capability_alias_override,
            inference_backend=inference_backend,
        )

    # (1) no override supplied — existing-caller behaviour, unchanged.
    default_invocation = _run(evidence_id="ev-default")
    assert default_invocation.capability_alias == task_contract.preferred_capability
    assert default_invocation.inference_backend == "MAC_LOCAL"

    # (2) override supplied — used verbatim, never silently ignored.
    overridden_invocation = _run(
        capability_alias_override="trinity-core", inference_backend="TRINITY_CORE_OVERFLOW", evidence_id="ev-override"
    )
    assert overridden_invocation.capability_alias == "trinity-core"
    assert overridden_invocation.inference_backend == "TRINITY_CORE_OVERFLOW"


def test_run_background_task_output_schema_comes_only_from_task_contract():
    """CD-5 Gate-1 closure delta (2026-09-16): confirms, by source
    inspection, that `litellm_client.complete(...)`'s `output_schema=`
    argument is `task_contract.output_schema` literally — never
    reconstructed, never caller-supplied — same discipline as the
    capability_alias proof above."""
    path = REPO_ROOT / "ai" / "gateway" / "background.py"
    source = path.read_text(encoding="utf-8")
    assert "output_schema=task_contract.output_schema" in source, (
        "expected run_background_task's litellm_client.complete(...) call to pass "
        "output_schema=task_contract.output_schema literally — the schema must be decided "
        "entirely by the registered TaskContract, never hand-reconstructed or caller-supplied"
    )


# ---------------------------------------------------------------------
# 4. no `trinity-*` alias literal anywhere in this WI's own new files
# ---------------------------------------------------------------------

_NEW_AI_FILE_ROOTS = ["ai/providers", "ai/gateway", "ai/prompts", "app/api/routers/ai.py"]


def _iter_new_files():
    for rel in _NEW_AI_FILE_ROOTS:
        path = REPO_ROOT / rel
        if path.is_dir():
            yield from sorted(
                p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            )
        elif path.is_file():
            yield path


def test_no_trinity_star_alias_literal_in_this_wis_new_files():
    """PID §9's own instruction: 'BAGMAN's own architecture-boundary
    tests must assert that no BAGMAN source file references a
    `trinity-*` alias at all, only `bagman-*`.' This is the narrow,
    WI-2-scoped check (this WI's own new files only) — WI-5 owns the
    full repo-wide sweep per WI-1's own report.

    CD-6 §103 Inference Architecture Ruling update: `trinity-core` is
    now the sole authorised Trinity alias
    (`_STILL_FORBIDDEN_TRINITY_ALIASES` excludes it, unlike
    `_TRINITY_STAR_ALIASES` above, which is still used for the
    REJECTION-behaviour parametrisation only) — none of THESE
    particular files (`ai/providers`, `ai/gateway`, `ai/prompts`,
    `app/api/routers/ai.py`) ever hardcode it as a literal anyway (they
    operate generically via `ai.invocation.BACKGROUND_CAPABILITY_ALIASES`/
    a caller-supplied variable, or mention it only in prose describing
    the override mechanism), but this check still forbids every OTHER
    `trinity-*` literal here, exactly as before."""
    violations = []
    for path in _iter_new_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for bad_alias in _STILL_FORBIDDEN_TRINITY_ALIASES:
            if bad_alias in lowered:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {bad_alias!r}")

    assert violations == [], "\n".join(violations)
