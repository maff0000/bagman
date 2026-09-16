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

_TRINITY_STAR_ALIASES = ["trinity-fast", "trinity-core", "trinity-deep", "trinity-embed"]
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


@pytest.mark.parametrize("bad_alias", _TRINITY_STAR_ALIASES + _RAW_MODEL_NAMES)
def test_validate_capability_alias_rejects_trinity_star_and_raw_model_names(bad_alias):
    with pytest.raises(ValidationError):
        validate_capability_alias(bad_alias)


@pytest.mark.parametrize("bad_alias", _TRINITY_STAR_ALIASES + _RAW_MODEL_NAMES)
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
    assert BACKGROUND_CAPABILITY_ALIASES == frozenset({"bagman-fast", "bagman-core", "bagman-deep"})


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


def test_run_background_task_capability_alias_comes_only_from_task_contract():
    """Confirms, by source inspection, that `create_invocation`'s
    `capability_alias=` argument is `task_contract.preferred_capability`
    — never any caller-supplied value."""
    path = REPO_ROOT / "ai" / "gateway" / "background.py"
    source = path.read_text(encoding="utf-8")
    assert "capability_alias=task_contract.preferred_capability" in source, (
        "expected run_background_task's create_invocation(...) call to pass "
        "capability_alias=task_contract.preferred_capability literally — routing must be "
        "decided entirely by the registered TaskContract, never a caller-supplied value"
    )


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
            yield from sorted(p for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            yield path


def test_no_trinity_star_alias_literal_in_this_wis_new_files():
    """PID §9's own instruction: 'BAGMAN's own architecture-boundary
    tests must assert that no BAGMAN source file references a
    `trinity-*` alias at all, only `bagman-*`.' This is the narrow,
    WI-2-scoped check (this WI's own new files only) — WI-5 owns the
    full repo-wide sweep per WI-1's own report."""
    violations = []
    for path in _iter_new_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for bad_alias in _TRINITY_STAR_ALIASES:
            if bad_alias in lowered:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {bad_alias!r}")

    assert violations == [], "\n".join(violations)
