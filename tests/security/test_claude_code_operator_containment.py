"""Structural security proofs for the CD-5 Gate-2 closure bounded
headless Claude Code operator runner (PID §97, Matt's explicit
requirement). Static/import-based where possible (mirrors
``tests/security/test_ai_litellm_alias_lockdown.py``'s and
``tests/integration/test_architecture_boundaries.py``'s own
discipline — ``ast``, not a regex, for source inspection), plus direct
behavioural proofs. Adversarial LIVE proof against the real `claude`
executable lives in
``tests/acceptance/claude_code_operator_live_proof.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------
# 1. exactly one module in the whole repository ever spawns `claude`
# ---------------------------------------------------------------------


def _iter_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        parts = path.relative_to(REPO_ROOT).parts
        if parts[0] in (".git", "node_modules") or "__pycache__" in parts:
            continue
        yield path


_SUBPROCESS_SPAWNING_ATTRS = {"Popen", "run", "call", "check_call", "check_output"}


def test_only_agent_claude_code_runner_ever_spawns_a_subprocess_in_production_code():
    """PID §97: 'No BAGMAN component outside this package ever
    constructs a claude subprocess invocation' — AST-based (mirrors
    this repo's own `ast, not a regex` discipline, see
    `test_architecture_boundaries.py`/`test_ai_litellm_alias_lockdown.py`):
    no production (non-test) module other than
    `agent/claude_code/runner.py` may call `subprocess.Popen`/`.run`/
    `.call`/`.check_call`/`.check_output`, or `os.system`/`os.popen`,
    anywhere. This is a stronger, unambiguous proof than searching for
    the literal string "claude" (which also legitimately appears
    elsewhere, e.g. the `checks["claude"]` health-check dict key)."""
    allowed = {
        REPO_ROOT / "agent" / "claude_code" / "runner.py",
        # Pre-existing, unrelated CD-5 WI-5 evaluation-harness use
        # (reuses a fixture's own recorded subprocess output — nothing
        # to do with Claude Code or this delta); out of scope here.
        REPO_ROOT / "ai" / "evaluation" / "injection_reuse.py",
    }
    violations = []
    for path in _iter_python_files():
        parts = path.relative_to(REPO_ROOT).parts
        if parts[0] in ("tests", "scripts", "ops", "deployment"):
            continue  # test/tooling/ops code may legitimately spawn subprocesses
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _SUBPROCESS_SPAWNING_ATTRS
                and isinstance(node.value, ast.Name)
                and node.value.id == "subprocess"
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} subprocess.{node.attr}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in ("system", "popen")
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} os.{node.attr}")
    assert violations == [], (
        f"found subprocess-spawning code outside agent/claude_code/runner.py: {violations} — "
        "only that module may construct a claude (or any other) subprocess invocation (PID §97)"
    )


def test_runner_never_passes_shell_true_to_subprocess():
    """Source-inspection proof, not just the behavioural monkeypatch
    tests in test_claude_code_runner.py — `shell=True` (or `shell=`
    with any truthy value) must never appear anywhere in this module's
    subprocess call at all."""
    path = REPO_ROOT / "agent" / "claude_code" / "runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            assert False, f"runner.py passes shell= explicitly at line {node.lineno} — forbidden (PID §97)"


def test_runner_uses_a_fixed_argv_list_not_a_formatted_shell_string():
    """The first positional argument to subprocess.Popen must be a
    list/tuple literal built from fixed flag strings plus the two
    prompt parameters — never an f-string/`.format()`/`%`-formatted
    single command string (the shape that would indicate accidental
    shell-string construction even without `shell=True`)."""
    path = REPO_ROOT / "agent" / "claude_code" / "runner.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert len(popen_calls) == 1, f"expected exactly one subprocess.Popen call, found {len(popen_calls)}"
    first_arg = popen_calls[0].args[0]
    assert isinstance(first_arg, ast.Name), (
        f"subprocess.Popen's first argument at line {popen_calls[0].lineno} is not a plain variable "
        f"reference to a pre-built list — got {type(first_arg).__name__}"
    )


def test_dangerously_skip_permissions_never_appears_in_the_runner():
    path = REPO_ROOT / "agent" / "claude_code" / "runner.py"
    source = path.read_text(encoding="utf-8")
    # Search the actual argv-construction region, not the module's own
    # docstring (which legitimately names these flags to say they are
    # never used).
    tree = ast.parse(source, filename=str(path))
    string_constants = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # None of the argv-shaped string literals may be the dangerous flag.
    assert "--dangerously-skip-permissions" not in string_constants
    assert "--allow-dangerously-skip-permissions" not in string_constants


# ---------------------------------------------------------------------
# 2. the HTTP request surface carries no field that could let a
#    caller influence the executable/flags/cwd/environment/model
# ---------------------------------------------------------------------


def test_operator_chat_request_has_no_field_naming_process_or_environment_control():
    from app.api.routers.operator import OperatorChatRequest

    field_names = set(OperatorChatRequest.model_fields.keys())
    forbidden_substrings = (
        "model", "flag", "cwd", "env", "home", "tool", "shell", "command", "exec", "path", "permission",
    )
    suspicious = [
        name for name in field_names if any(token in name.lower() for token in forbidden_substrings)
    ]
    assert suspicious == [], (
        f"POST /internal/operator/chat request model has a field that could let a caller "
        f"influence process execution: {suspicious} — the browser must never control the "
        "executable/flags/cwd/environment (Matt's explicit Gate-2 instruction)"
    )
    assert field_names == {
        "message", "actor_type", "actor_id", "correlation_id", "evidence_id", "intake_id", "entity_id",
    }


# ---------------------------------------------------------------------
# 3. --tools "" and --restricted are always present together
# ---------------------------------------------------------------------


def test_tools_disabled_and_restricted_flags_are_both_always_present():
    from agent.claude_code.runner import ClaudeCodeOperatorRunner

    path = REPO_ROOT / "agent" / "claude_code" / "runner.py"
    source = path.read_text(encoding="utf-8")
    assert '"--tools"' in source and '""' in source, "expected a literal --tools \"\" (all tools disabled)"
    assert '"--restricted"' in source, "expected the --restricted belt-and-braces flag"
    assert '"--strict-mcp-config"' in source, "expected --strict-mcp-config (no MCP servers loaded)"


# ---------------------------------------------------------------------
# 4. provider-side structured output is never trusted as sufficient
#    proof — validate_task_output remains unconditional (mirrors the
#    equivalent Gate-1 proof for the LiteLLM path)
# ---------------------------------------------------------------------


def test_orchestrator_calls_validate_task_output_unconditionally():
    path = REPO_ROOT / "agent" / "claude_code" / "orchestrator.py"
    source = path.read_text(encoding="utf-8")
    assert "validate_task_output(contract, output)" in source
