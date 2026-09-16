"""``ClaudeCodeOperatorRunner`` — the ONE place BAGMAN ever constructs a
``claude`` subprocess invocation (CD-5 Gate-2 closure, 2026-09-16).

Security boundary (the whole point of this module)
------------------------------------------------------
The browser/HTTP caller NEVER controls the executable name, CLI flags,
working directory, environment, or shell syntax. Every one of those is
a FIXED, hard-coded value chosen by this module's own constructor —
the only caller-influenced values are ``system_prompt``/``user_prompt``
text content, passed as two single argv elements via the list form of
:func:`subprocess.Popen` (never ``shell=True``, never string
interpolation into a shell command line — the list form hands each
element straight to ``execve()``; shell metacharacters inside the
prompt text are inert, just literal characters in one argument).

Tool/authority containment: every invocation passes ``--tools ""``
(disable the ENTIRE built-in tool set — Read/Write/Edit/Bash/WebFetch/
everything) plus ``--restricted`` (belt-and-braces: additionally
refuses command-running tools even if something tried to re-enable
them, ignores user/project/local settings files — so no ambient
``CLAUDE.md``/hooks/MCP config from this host's own Trinity persona
system is ever loaded — and confines any file tool to the working
directory) plus ``--strict-mcp-config`` with no ``--mcp-config``
supplied (no MCP server of any kind is ever loaded). The invoked
process can read the prompt text it was given and produce a text
response — nothing else. This is verified live, adversarially, in
``tests/security/test_claude_code_operator_containment.py`` and
``tests/acceptance/claude_code_operator_live_proof.py``.

``--system-prompt`` REPLACES Claude Code's own default system prompt
entirely (never ``--append-system-prompt``) — the invoked process
never inherits this host's own normal "you are Claude Code, a
development agent" framing; it is told, exactly once per call, that it
is BAGMAN's bounded operator assistant (see
``agent.claude_code.orchestrator``).

Never uses ``--dangerously-skip-permissions``/
``--allow-dangerously-skip-permissions`` — irrelevant with ``--tools
""`` (nothing can prompt for permission when no tool exists to
invoke), and deliberately never enabled regardless, per Matt's own
explicit instruction.

Process control
-----------------
Every invocation runs in its own process group
(``start_new_session=True``) so a timeout kills the ENTIRE group
(``os.killpg``), not just the immediate child — defends against
`claude` itself spawning any descendant process. stdout is bounded
(``max_output_bytes``) before ever being handed to ``json.loads``. A
failed/timed-out/malformed invocation NEVER raises out of :meth:`run`
— every outcome (see :class:`ClaudeCodeOutcomeStatus`) is returned as
data, mirroring ``ai.providers.litellm.client.LiteLLMClient.complete``'s
own "never raise for a provider/transport-level failure" contract
exactly, so ``agent.claude_code.orchestrator`` can treat every case
uniformly.
"""
from __future__ import annotations

import enum
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

#: Fixed executable name only. Resolved via PATH at exec time exactly
#: like any other subprocess call — never a caller-supplied path,
#: never shell-resolved (no ``shell=True`` anywhere in this module).
_CLAUDE_EXECUTABLE = "claude"

#: BAGMAN's own operator-model configuration env var — same name CD-5
#: WI-3's direct-Anthropic adapter used (``ai/providers/claude/client.py``),
#: kept identical for deployment-config continuity across the Gate-2
#: architecture correction.
MODEL_ENV_VAR = "BAGMAN_OPERATOR_MODEL"
_DEFAULT_MODEL = "claude-sonnet-5"

#: Where Claude Code looks for its own auth/session state (PID §97 —
#: "Claude Code owns its own authentication/session mechanism").
#: BAGMAN's own code never reads, writes, or interprets anything under
#: this directory — it only tells the child process where to find it,
#: via the HOME environment variable of the CONTROLLED env this module
#: builds (never the caller's/parent's own HOME).
HOME_ENV_VAR = "BAGMAN_CLAUDE_CODE_HOME"
_DEFAULT_HOME = "/srv/bagman-secrets/claude_code_home"

#: A dedicated, minimal working directory for every operator
#: invocation — deliberately NOT `/srv/bagman` (BAGMAN's own source
#: tree) or any directory containing secrets/unrelated host files.
#: `--restricted` already confines file-tool access to this directory
#: (plus `--add-dir` additions, of which there are none here) — kept
#: empty on purpose, defense in depth alongside `--tools ""`.
CWD_ENV_VAR = "BAGMAN_CLAUDE_CODE_CWD"
_DEFAULT_CWD = "/srv/bagman-secrets/claude_code_operator_cwd"

#: PID's own "explicit invocation timeout" / "output-size bound"
#: requirements — sensible defaults, always overridable per call.
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_BYTES = 300_000


class ClaudeCodeOutcomeStatus(str, enum.Enum):
    """The full outcome space for one :meth:`ClaudeCodeOperatorRunner.run`
    attempt — never raised as an exception; always returned as data on
    :class:`ClaudeCodeInvocationResult.status` (mirrors
    ``ai.providers.litellm.client.LiteLLMOutcomeStatus`` exactly)."""

    #: A genuine response was received and parsed.
    OK = "OK"
    #: The invocation exceeded its timeout; the whole process group was
    #: killed.
    TIMEOUT = "TIMEOUT"
    #: The `claude` executable could not be started at all (not on
    #: PATH, permission denied, ...) or exited non-zero.
    PROCESS_ERROR = "PROCESS_ERROR"
    #: The process exited zero but stdout was not parseable as the
    #: expected `--output-format json` shape.
    OUTPUT_PARSE_ERROR = "OUTPUT_PARSE_ERROR"
    #: The process ran and returned parseable JSON, but Claude Code's
    #: own `is_error` field was true (e.g. an API-level failure inside
    #: the invocation).
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass(frozen=True)
class ClaudeCodeInvocationResult:
    """Provider-neutral, normalised outcome of one real (or faked)
    Claude Code invocation — nothing outside ``agent/claude_code/``
    ever sees the raw `claude --output-format json` response shape."""

    status: ClaudeCodeOutcomeStatus
    text: Optional[str] = None
    session_id: Optional[str] = None
    model_usage: Mapping[str, Any] = field(default_factory=dict)
    total_cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    #: Whatever Claude Code's own `permission_denials` array reported
    #: — normally empty, since `--tools ""` means nothing exists for
    #: the model to even attempt to invoke; a non-empty value here
    #: would itself be a notable containment signal worth surfacing.
    permission_denials: tuple = ()
    error_detail: Optional[str] = None


class ClaudeCodeOperatorRunnerProtocol(Protocol):
    """Structural type both :class:`ClaudeCodeOperatorRunner` (real)
    and :class:`agent.claude_code.fake.FakeClaudeCodeOperatorRunner`
    (deterministic test/dev substitute) satisfy."""

    def run(
        self, *, system_prompt: str, user_prompt: str, timeout_seconds: float
    ) -> ClaudeCodeInvocationResult: ...

    def is_available(self) -> bool: ...


def _build_controlled_env(*, home: str) -> dict[str, str]:
    """A minimal, fixed environment — NEVER the caller's/parent
    process's own ``os.environ`` (which, inside the real `bagman-api`
    container, holds database/object-store/LiteLLM secrets that must
    never be exposed to this subprocess). Only what `claude` itself
    needs to run and find its own auth state."""
    env = {
        "HOME": home,
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    return env


class ClaudeCodeOperatorRunner:
    """The real adapter — actually execs `claude -p ...` (CD-5 Gate-2
    closure). See module docstring for the full security/process-
    control contract.
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        home: Optional[str] = None,
        cwd: Optional[str] = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        #: No I/O performed at construction time (mirrors
        #: `LiteLLMClient.__init__`/`ClaudeClient.__init__` — only
        #: config is stored; reachability is proven live by
        #: `is_available()`/`run()`, never assumed at construction).
        self._model = model or os.environ.get(MODEL_ENV_VAR, _DEFAULT_MODEL)
        self._home = home or os.environ.get(HOME_ENV_VAR, _DEFAULT_HOME)
        self._cwd = cwd or os.environ.get(CWD_ENV_VAR, _DEFAULT_CWD)
        self._max_output_bytes = max_output_bytes

    def run(
        self, *, system_prompt: str, user_prompt: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> ClaudeCodeInvocationResult:
        # Fixed executable + fixed flag set. `user_prompt`/`system_prompt`
        # are the ONLY caller-influenced values, each one single argv
        # element — never concatenated into a shell string, never
        # passed through `shell=True`. See module docstring.
        argv = [
            _CLAUDE_EXECUTABLE,
            "-p",
            user_prompt,
            "--tools",
            "",
            "--restricted",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--system-prompt",
            system_prompt,
            "--model",
            self._model,
        ]

        cwd_path = Path(self._cwd)
        try:
            cwd_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.PROCESS_ERROR,
                error_detail=f"could not prepare operator working directory {self._cwd!r}: {exc}"[:500],
            )

        env = _build_controlled_env(home=self._home)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # own process group -> clean group kill on timeout
                text=True,
            )
        except OSError as exc:
            return ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.PROCESS_ERROR,
                error_detail=f"could not start {_CLAUDE_EXECUTABLE!r}: {exc}"[:500],
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc)
            try:
                proc.communicate(timeout=5.0)
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                pass
            return ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.TIMEOUT,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_detail=f"invocation exceeded {timeout_seconds}s; process group terminated",
            )

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            return ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.PROCESS_ERROR,
                duration_ms=duration_ms,
                error_detail=f"exit code {proc.returncode}: {(stderr or '')[:400]}",
            )

        bounded_stdout = (stdout or "")[: self._max_output_bytes]
        try:
            payload = json.loads(bounded_stdout)
        except json.JSONDecodeError as exc:
            return ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.OUTPUT_PARSE_ERROR,
                duration_ms=duration_ms,
                error_detail=f"could not parse --output-format json response: {exc}"[:500],
            )

        permission_denials = tuple(payload.get("permission_denials") or ())

        if payload.get("is_error"):
            return ClaudeCodeInvocationResult(
                status=ClaudeCodeOutcomeStatus.PROVIDER_ERROR,
                session_id=payload.get("session_id"),
                duration_ms=duration_ms,
                permission_denials=permission_denials,
                error_detail=str(payload.get("result"))[:500],
            )

        return ClaudeCodeInvocationResult(
            status=ClaudeCodeOutcomeStatus.OK,
            text=payload.get("result"),
            session_id=payload.get("session_id"),
            model_usage=payload.get("modelUsage") or {},
            total_cost_usd=payload.get("total_cost_usd"),
            duration_ms=payload.get("duration_ms", duration_ms),
            num_turns=payload.get("num_turns"),
            permission_denials=permission_denials,
        )

    @staticmethod
    def _kill_process_group(proc: "subprocess.Popen[str]") -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # already gone, or never really started its own group

    def is_available(self) -> bool:
        """Cheap reachability check (mirrors
        `LiteLLMClient.is_available`/`ClaudeClient.is_available`'s own
        pattern): proves the `claude` executable is installed and on
        PATH. Does NOT prove authentication is valid or that a real
        invocation would succeed — a full round trip is too expensive
        for a health-check poll; `run()` is the only place that proves
        that live."""
        return shutil.which(_CLAUDE_EXECUTABLE) is not None
