"""``ClaudeClient`` — the Anthropic Messages API provider adapter (CD-5
PID §6/§8/§17-18/§33-35/§49, WI-3).

This is BAGMAN's ONE Claude-speaking adapter (PID §6): nothing outside
this module ever imports ``requests``/``httpx``/an Anthropic SDK to
call the Anthropic API directly. It is instantiated exactly once, in
production, by ``app/api/composition.py`` — no router, service, or
``agent/`` module ever constructs one itself.

Uses ``requests`` directly against the Anthropic Messages API
(``POST /v1/messages``) rather than the ``anthropic`` Python SDK — a
deliberate WI-3 choice: ``requests`` is already a BAGMAN dependency
(``tests/acceptance/_lib.py``), the Messages API's HTTP contract is
small and stable, and this avoids introducing a new third-party SDK
dependency for what is, structurally, one JSON POST with a few
well-documented headers. If a future WI needs SDK-only functionality
(e.g. streaming), revisit this call then.

Retry policy (PID §49) — the exact, documented contract
---------------------------------------------------------
* **401/403 (authentication failure)** -> :class:`ClaudeAuthenticationError`.
  NEVER retried — a bad/revoked credential does not become valid by
  trying again.
* **Any other 4xx** (malformed request, invalid tool schema, content
  policy rejection, ...) -> :class:`ClaudeRequestError`. NEVER
  retried — this is a caller-side/content problem, not a transient
  condition.
* **429 (rate limited)** -> retried up to ``max_retries`` additional
  times with a short bounded exponential backoff
  (0.25s, 0.5s, 1s, capped at 2s), then :class:`ClaudeRateLimitedError`.
* **5xx, or a connection error** (DNS/connection refused/reset) ->
  retried the same way, then :class:`ClaudeUnavailableError`.
* **A connect/read timeout** -> retried the same way, then
  :class:`ClaudeTimeoutError`.

``max_retries`` bounds every retryable class independently at the same
value; total attempts for a retryable failure = ``max_retries + 1``.
No call ever waits unboundedly (PID §75) — ``connect_timeout_s``/
``read_timeout_s`` are always explicit, and retries are always finite.

Logging (PID §49/§78)
-----------------------
Every log line carries invocation-level METADATA only — model,
stop_reason, latency, message/tool counts, HTTP status, attempt number.
The API key is never logged (it is read from disk fresh for every
request and passed only as an HTTP header this module builds
directly — never `repr()`-ed, never included in any log `extra`). Full
`system`/`messages`/tool-call prompt or response CONTENT is never
logged by this module, by default — only its shape (counts/lengths).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

import requests

logger = logging.getLogger("bagman.ai.providers.claude")

_DEFAULT_API_KEY_FILE = "/run/secrets/anthropic_api_key"
#: PID §18 — a real, current Claude model identifier, fully overridable
#: via BAGMAN_OPERATOR_MODEL (never scattered as a literal elsewhere in
#: BAGMAN source). Matches the model this WI's own delivery session was
#: run under (2026-09-13) — the current, non-deprecated default.
_DEFAULT_MODEL = "claude-sonnet-5"
_DEFAULT_API_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_CONNECT_TIMEOUT_S = 5.0
_DEFAULT_READ_TIMEOUT_S = 60.0
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_MAX_TOKENS = 1024


# ---------------------------------------------------------------------
# errors (PID §49 — never let a raw requests/HTTP exception escape)
# ---------------------------------------------------------------------


class ClaudeProviderError(Exception):
    """Base class for every Claude-adapter-specific failure. Mapped to
    ``AIInvocation.error_code``/``FAILED`` by
    ``agent.bagman.orchestrator`` — never allowed to leak a raw
    ``requests``/HTTP/JSON exception to a caller of this module."""


class ClaudeAuthenticationError(ClaudeProviderError):
    """HTTP 401/403 — never retried (see module docstring)."""


class ClaudeRequestError(ClaudeProviderError):
    """Any other 4xx (malformed request/content policy/invalid tool
    schema) — never retried (see module docstring)."""


class ClaudeRateLimitedError(ClaudeProviderError):
    """HTTP 429 — retried a small bounded number of times, then raised
    (see module docstring)."""


class ClaudeTimeoutError(ClaudeProviderError):
    """Connect/read timeout — retried a small bounded number of times,
    then raised (see module docstring)."""


class ClaudeUnavailableError(ClaudeProviderError):
    """5xx, a connection error, or an unparsable response body —
    retried a small bounded number of times, then raised (see module
    docstring)."""


# ---------------------------------------------------------------------
# provider-neutral request/response shapes
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDefinition:
    """One tool Claude may choose to call this turn — provider-neutral;
    :meth:`ClaudeClient.send_message` translates this into Anthropic's
    own ``tools`` wire shape internally. Built from
    ``agent.tools.registry.ToolSpec`` by the orchestration loop, never
    constructed by this module itself."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool call Claude requested this turn."""

    tool_call_id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True)
class ClaudeTurnResult:
    """Provider-neutral normalisation of one Claude API response (PID
    §70) — the orchestration loop never inspects Anthropic's raw
    ``content`` block shape directly for anything except re-emitting it
    verbatim via :meth:`to_assistant_content_blocks` (needed so
    Anthropic's own ``tool_use`` block ids are preserved exactly when
    continuing a multi-turn tool-call loop)."""

    stop_reason: str
    text: Optional[str]
    tool_calls: tuple[ToolCallRequest, ...]
    provider_model: str
    usage: Mapping[str, Any]
    latency_ms: int
    raw_content_blocks: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def to_assistant_content_blocks(self) -> list[dict]:
        """The exact Anthropic-shaped content blocks to echo back as
        this turn's ``assistant`` message when continuing a tool-use
        loop — preserved verbatim from the provider response (never
        reconstructed/paraphrased), so Claude's own ``tool_use`` block
        ids/inputs round-trip exactly."""
        return [dict(block) for block in self.raw_content_blocks]


class ClaudeClientProtocol(Protocol):
    """Structural shape both :class:`ClaudeClient` (real) and
    :class:`ai.providers.claude.fake.FakeClaudeClient` (deterministic
    test/dev double, PID §61) satisfy — this is the type
    ``app/api/composition.py``/``agent/bagman/orchestrator.py`` depend
    on, never a concrete class, so either implementation plugs in
    without any caller-side branching."""

    model: str

    def send_message(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition] = (),
        max_tokens: Optional[int] = None,
    ) -> ClaudeTurnResult: ...

    def is_available(self) -> bool: ...


def _backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff: 0.25s, 0.5s, 1s, capped at 2s."""
    return min(0.25 * (2**attempt), 2.0)


def _read_api_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ClaudeAuthenticationError(
            f"could not read Anthropic API key from {str(path)!r}: {exc}. BAGMAN never "
            "hardcodes this credential — point BAGMAN_ANTHROPIC_API_KEY_FILE at a readable "
            "file (e.g. a mounted Docker/Compose secret, PID §13)."
        ) from exc


class ClaudeClient:
    """The real, network-speaking Anthropic Messages API adapter. See
    module docstring for the full retry/logging contract.

    Configuration (all overridable; every default is a real, working
    value — PID §18):

    * ``BAGMAN_ANTHROPIC_API_KEY_FILE`` (default ``/run/secrets/anthropic_api_key``)
    * ``BAGMAN_OPERATOR_MODEL`` (default ``"claude-sonnet-5"``)
    * ``BAGMAN_ANTHROPIC_API_VERSION`` (default ``"2023-06-01"``)
    * ``BAGMAN_ANTHROPIC_BASE_URL`` (default ``"https://api.anthropic.com"``)
    * ``BAGMAN_CLAUDE_CONNECT_TIMEOUT_S`` (default ``5.0``)
    * ``BAGMAN_CLAUDE_READ_TIMEOUT_S`` (default ``60.0``)
    * ``BAGMAN_CLAUDE_MAX_RETRIES`` (default ``2`` — 3 total attempts)
    * ``BAGMAN_CLAUDE_MAX_TOKENS`` (default ``1024``)
    """

    def __init__(
        self,
        *,
        api_key_file: Optional[str] = None,
        model: Optional[str] = None,
        api_version: Optional[str] = None,
        base_url: Optional[str] = None,
        connect_timeout_s: Optional[float] = None,
        read_timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
        max_tokens: Optional[int] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._api_key_file = Path(
            api_key_file or os.environ.get("BAGMAN_ANTHROPIC_API_KEY_FILE", _DEFAULT_API_KEY_FILE)
        )
        self.model = model or os.environ.get("BAGMAN_OPERATOR_MODEL", _DEFAULT_MODEL)
        self._api_version = api_version or os.environ.get(
            "BAGMAN_ANTHROPIC_API_VERSION", _DEFAULT_API_VERSION
        )
        self._base_url = (
            base_url or os.environ.get("BAGMAN_ANTHROPIC_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self._connect_timeout_s = connect_timeout_s or float(
            os.environ.get("BAGMAN_CLAUDE_CONNECT_TIMEOUT_S", _DEFAULT_CONNECT_TIMEOUT_S)
        )
        self._read_timeout_s = read_timeout_s or float(
            os.environ.get("BAGMAN_CLAUDE_READ_TIMEOUT_S", _DEFAULT_READ_TIMEOUT_S)
        )
        self._max_retries = (
            max_retries
            if max_retries is not None
            else int(os.environ.get("BAGMAN_CLAUDE_MAX_RETRIES", _DEFAULT_MAX_RETRIES))
        )
        self._max_tokens = max_tokens or int(
            os.environ.get("BAGMAN_CLAUDE_MAX_TOKENS", _DEFAULT_MAX_TOKENS)
        )
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        # Read fresh on every call rather than cached at __init__ time —
        # lets an operator rotate the secret file without restarting
        # bagman-api, and guarantees the key is never held as a
        # long-lived attribute this class could accidentally repr()/log.
        api_key = _read_api_key(self._api_key_file)
        return {
            "x-api-key": api_key,
            "anthropic-version": self._api_version,
            "content-type": "application/json",
        }

    def _post_messages(self, body: dict) -> tuple[dict, int]:
        attempts = self._max_retries + 1
        last_exc: Optional[Exception] = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = self._session.post(
                    f"{self._base_url}/v1/messages",
                    headers=self._headers(),
                    json=body,
                    timeout=(self._connect_timeout_s, self._read_timeout_s),
                )
            except requests.exceptions.Timeout as exc:
                last_exc = exc
                logger.warning(
                    "claude_request_timeout",
                    extra={
                        "component": "bagman.ai.providers.claude",
                        "event_type": "CLAUDE_TIMEOUT",
                        "attempt": attempt + 1,
                    },
                )
                if attempt < attempts - 1:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise ClaudeTimeoutError(
                    f"Claude request timed out after {attempts} attempt(s)"
                ) from exc
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "claude_request_connection_error",
                    extra={
                        "component": "bagman.ai.providers.claude",
                        "event_type": "CLAUDE_UNAVAILABLE",
                        "attempt": attempt + 1,
                    },
                )
                if attempt < attempts - 1:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise ClaudeUnavailableError(
                    f"could not reach Claude after {attempts} attempt(s): {type(exc).__name__}"
                ) from exc

            latency_ms = int((time.monotonic() - started) * 1000)

            if response.status_code in (401, 403):
                # Never retried — see module docstring.
                raise ClaudeAuthenticationError(
                    f"Claude authentication failed (HTTP {response.status_code})"
                )
            if response.status_code == 429:
                logger.warning(
                    "claude_rate_limited",
                    extra={
                        "component": "bagman.ai.providers.claude",
                        "event_type": "CLAUDE_RATE_LIMITED",
                        "attempt": attempt + 1,
                    },
                )
                if attempt < attempts - 1:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise ClaudeRateLimitedError(
                    f"Claude rate-limited (HTTP 429) after {attempts} attempt(s)"
                )
            if 500 <= response.status_code < 600:
                logger.warning(
                    "claude_server_error",
                    extra={
                        "component": "bagman.ai.providers.claude",
                        "event_type": "CLAUDE_UNAVAILABLE",
                        "attempt": attempt + 1,
                        "status_code": response.status_code,
                    },
                )
                if attempt < attempts - 1:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise ClaudeUnavailableError(
                    f"Claude returned HTTP {response.status_code} after {attempts} attempt(s)"
                )
            if response.status_code >= 400:
                # Any other 4xx — a content/validation error. Never
                # retried (see module docstring) — the message text is
                # safe to include (Anthropic's own error body), never a
                # raw driver exception.
                raise ClaudeRequestError(
                    f"Claude rejected the request (HTTP {response.status_code}): "
                    f"{response.text[:500]}"
                )

            try:
                return response.json(), latency_ms
            except ValueError as exc:
                raise ClaudeUnavailableError("Claude returned a non-JSON response body") from exc

        raise ClaudeUnavailableError(  # pragma: no cover - defensive, unreachable
            f"Claude request failed after {attempts} attempt(s): {last_exc}"
        )

    def send_message(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition] = (),
        max_tokens: Optional[int] = None,
    ) -> ClaudeTurnResult:
        """Send one turn to Claude. Never raises a raw ``requests``/JSON
        exception — only this module's :class:`ClaudeProviderError`
        subclasses (PID §49). Never logs ``system``/``messages``/tool
        content (PID §49/§78) — only shape metadata.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system,
            "messages": list(messages),
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.input_schema)}
                for t in tools
            ]

        data, latency_ms = self._post_messages(body)

        content_blocks = data.get("content", []) or []
        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text" and b.get("text")]
        tool_calls = tuple(
            ToolCallRequest(tool_call_id=b["id"], name=b["name"], input=b.get("input", {}) or {})
            for b in content_blocks
            if b.get("type") == "tool_use"
        )
        usage = data.get("usage") or {}

        logger.info(
            "claude_turn_completed",
            extra={
                "component": "bagman.ai.providers.claude",
                "event_type": "CLAUDE_TURN_COMPLETED",
                "model": data.get("model", self.model),
                "stop_reason": data.get("stop_reason"),
                "latency_ms": latency_ms,
                "message_count": len(messages),
                "tool_call_count": len(tool_calls),
            },
        )

        return ClaudeTurnResult(
            stop_reason=data.get("stop_reason", "end_turn"),
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            provider_model=data.get("model", self.model),
            usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
            latency_ms=latency_ms,
            raw_content_blocks=tuple(content_blocks),
        )

    def is_available(self) -> bool:
        """Cheap (minimal-token) reachability/auth probe — for the
        eventual ``/internal/ai/health``'s ``claude`` key (see this
        WI's delivery report for the exact wiring the PL should add).
        Performs ONE real, minimal-cost Claude call (``max_tokens=4``);
        never raises — any failure (auth, network, rate limit, timeout)
        is reported as ``False`` and logged at WARNING, never allowed
        to propagate as an exception.
        """
        try:
            self.send_message(
                system="You are a health check. Reply with exactly one word: OK.",
                messages=[{"role": "user", "content": "ping"}],
                tools=(),
                max_tokens=4,
            )
            return True
        except ClaudeProviderError:
            logger.warning(
                "claude_is_available_check_failed",
                extra={
                    "component": "bagman.ai.providers.claude",
                    "event_type": "CLAUDE_UNAVAILABLE",
                },
            )
            return False
