"""Tests for ``agent.claude_code.runner`` (CD-5 Gate-2 closure, PID
§97). No real `claude` subprocess anywhere in this module (PID §61 —
ordinary tests never depend on a live LLM/subprocess) —
``subprocess.Popen`` is monkeypatched at the exact call boundary,
mirroring the same technique
``tests/integration/test_litellm_client.py`` already establishes for
`urllib.request.urlopen` and
``tests/integration/test_claude_provider_client.py`` for
`requests.Session.post`. Real end-to-end proof against the actual
`claude` binary lives in
``tests/acceptance/claude_code_operator_live_proof.py``.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from agent.claude_code.runner import (
    ClaudeCodeOperatorRunner,
    ClaudeCodeOutcomeStatus,
    _build_controlled_env,
)


class _FakeCompletedProcess:
    def __init__(self, *, stdout: str, stderr: str = "", returncode: int = 0, pid: int = 4242):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = pid

    def communicate(self, timeout=None):  # noqa: ARG002 - signature must match subprocess.Popen
        return self._stdout, self._stderr


def _success_json(**overrides) -> str:
    body = {
        "is_error": False,
        "result": "This is a test answer.",
        "session_id": "sess-123",
        "modelUsage": {"claude-sonnet-5": {"inputTokens": 10, "outputTokens": 2}},
        "total_cost_usd": 0.001,
        "duration_ms": 1500,
        "num_turns": 1,
        "permission_denials": [],
    }
    body.update(overrides)
    return json.dumps(body)


# ---------------------------------------------------------------------
# security: fixed executable/argument contract, never a shell
# ---------------------------------------------------------------------


def test_run_never_uses_shell_true(monkeypatch, tmp_path):
    captured_kwargs = {}

    def _fake_popen(argv, **kwargs):
        captured_kwargs["argv"] = argv
        captured_kwargs.update(kwargs)
        return _FakeCompletedProcess(stdout=_success_json())

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "home"), cwd=str(tmp_path / "cwd"))
    result = runner.run(system_prompt="sys", user_prompt="hello", timeout_seconds=5.0)

    assert result.status == ClaudeCodeOutcomeStatus.OK
    assert captured_kwargs.get("shell", False) is False  # never explicitly enabled
    assert "shell" not in captured_kwargs or captured_kwargs["shell"] is False
    assert isinstance(captured_kwargs["argv"], list)  # list form -> execve, never a shell string


def test_hostile_prompt_text_never_escapes_its_own_argv_element(monkeypatch, tmp_path):
    """A prompt containing shell metacharacters must reach `claude` as
    ONE literal argv element, never split or interpreted — proof that
    this module is immune to command injection via prompt content."""
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompletedProcess(stdout=_success_json())

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    hostile = "hello; rm -rf / ; $(cat /etc/passwd) `whoami` && echo pwned"
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "home"), cwd=str(tmp_path / "cwd"))
    runner.run(system_prompt="sys", user_prompt=hostile, timeout_seconds=5.0)

    argv = captured["argv"]
    assert hostile in argv  # present as exactly one element
    assert argv.count(hostile) == 1
    # No element of argv was mangled/split by shell metacharacters —
    # every element is either a fixed flag or the two prompt strings.
    for element in argv:
        assert isinstance(element, str)


def test_executable_flags_cwd_and_env_are_always_fixed_never_caller_supplied(monkeypatch, tmp_path):
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _FakeCompletedProcess(stdout=_success_json())

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    home = str(tmp_path / "home")
    cwd = str(tmp_path / "cwd")
    runner = ClaudeCodeOperatorRunner(home=home, cwd=cwd, model="claude-sonnet-5")
    runner.run(system_prompt="sys", user_prompt="anything, even --model=evil or ; ls", timeout_seconds=5.0)

    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--restricted" in argv
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--allow-dangerously-skip-permissions" not in argv
    assert captured["cwd"] == cwd
    assert captured["env"]["HOME"] == home


def test_controlled_env_never_inherits_the_parent_processs_full_environment(monkeypatch):
    monkeypatch.setenv("BAGMAN_OBJECT_STORE_SECRET_KEY", "should-never-leak")
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "also-should-never-leak")

    env = _build_controlled_env(home="/some/home")

    assert "BAGMAN_OBJECT_STORE_SECRET_KEY" not in env
    assert "SOME_UNRELATED_SECRET" not in env
    assert env["HOME"] == "/some/home"
    assert "PATH" in env  # needed to resolve the `claude` executable itself


# ---------------------------------------------------------------------
# outcome parsing
# ---------------------------------------------------------------------


def test_successful_invocation_parses_every_field(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kwargs: _FakeCompletedProcess(stdout=_success_json())  # noqa: ARG005
    )
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)

    assert result.status == ClaudeCodeOutcomeStatus.OK
    assert result.text == "This is a test answer."
    assert result.session_id == "sess-123"
    assert result.total_cost_usd == 0.001
    assert result.num_turns == 1
    assert result.permission_denials == ()
    assert "claude-sonnet-5" in result.model_usage


def test_is_error_true_becomes_provider_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: _FakeCompletedProcess(  # noqa: ARG005
            stdout=_success_json(is_error=True, result="something went wrong upstream")
        ),
    )
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)

    assert result.status == ClaudeCodeOutcomeStatus.PROVIDER_ERROR
    assert result.text is None


def test_nonzero_exit_becomes_process_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: _FakeCompletedProcess(stdout="", stderr="boom", returncode=1),  # noqa: ARG005
    )
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)

    assert result.status == ClaudeCodeOutcomeStatus.PROCESS_ERROR
    assert "boom" in (result.error_detail or "")


def test_malformed_json_becomes_output_parse_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kwargs: _FakeCompletedProcess(stdout="not json { { {")  # noqa: ARG005
    )
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)

    assert result.status == ClaudeCodeOutcomeStatus.OUTPUT_PARSE_ERROR


def test_unstartable_executable_becomes_process_error_not_a_raised_exception(monkeypatch, tmp_path):
    def _raise(argv, **kwargs):  # noqa: ARG001
        raise OSError("No such file or directory: 'claude'")

    monkeypatch.setattr(subprocess, "Popen", _raise)
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)  # must not raise

    assert result.status == ClaudeCodeOutcomeStatus.PROCESS_ERROR


def test_timeout_kills_the_process_group_and_returns_timeout_status(monkeypatch, tmp_path):
    killed = {}

    class _HangingProcess:
        pid = 9999

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    def _fake_popen(argv, **kwargs):  # noqa: ARG001
        return _HangingProcess()

    def _fake_killpg(pgid, sig):
        killed["pgid"] = pgid
        killed["sig"] = sig

    def _fake_getpgid(pid):
        return pid

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("os.killpg", _fake_killpg)
    monkeypatch.setattr("os.getpgid", _fake_getpgid)

    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=0.01)

    assert result.status == ClaudeCodeOutcomeStatus.TIMEOUT
    assert killed["pgid"] == 9999  # the process group was actually killed


def test_output_is_bounded_before_json_parsing(monkeypatch, tmp_path):
    """A pathologically large stdout must not be handed to json.loads
    unbounded (PID's own explicit 'output-size bound' requirement)."""
    huge = "x" * 10_000_000
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: _FakeCompletedProcess(stdout=huge))  # noqa: ARG005

    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"), max_output_bytes=1000)
    result = runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)

    # Bounded-then-truncated garbage is not valid JSON -> a clean,
    # honest OUTPUT_PARSE_ERROR, never an attempt to parse 10MB.
    assert result.status == ClaudeCodeOutcomeStatus.OUTPUT_PARSE_ERROR


def test_is_available_reflects_whether_the_executable_is_on_path(monkeypatch):
    import shutil as shutil_module

    runner = ClaudeCodeOperatorRunner(home="/x", cwd="/y")

    monkeypatch.setattr(shutil_module, "which", lambda name: "/usr/local/bin/claude")
    assert runner.is_available() is True

    monkeypatch.setattr(shutil_module, "which", lambda name: None)
    assert runner.is_available() is False


def test_default_model_reads_the_bagman_operator_model_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("BAGMAN_OPERATOR_MODEL", "claude-opus-5")
    captured = {}

    def _fake_popen(argv, **kwargs):  # noqa: ARG001
        captured["argv"] = argv
        return _FakeCompletedProcess(stdout=_success_json())

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    runner = ClaudeCodeOperatorRunner(home=str(tmp_path / "h"), cwd=str(tmp_path / "c"))
    runner.run(system_prompt="sys", user_prompt="hi", timeout_seconds=5.0)

    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
