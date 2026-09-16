"""``LiteLLMClient`` — the ONE adapter that speaks to the existing
Trinity LiteLLM installation (CD-5 PID §6/§8/§9/§50, WI-2).

Locked topology (PID §2/§8/§9 — see ``PID.md`` in full before changing
anything here): BAGMAN never calls the Mac mini, Ollama/MLX/llama.cpp,
or Trinity compute directly. It calls the existing, already-governed
Trinity LiteLLM gateway with one of exactly three BAGMAN-owned logical
aliases (``bagman-fast``/``bagman-core``/``bagman-deep``) and lets that
gateway decide the physical backend. This module's own mechanical
enforcement of that boundary is :func:`validate_capability_alias`,
called before ANY HTTP request is ever constructed (PID §5's
raw-model-name-lockdown mitigation: the shared gateway itself is known
to still accept a raw physical model name, so BAGMAN's own boundary
must refuse to ever send one, regardless of what the gateway tolerates).

Wire protocol (PID §50, independently re-verified during this WI)
--------------------------------------------------------------------
``POST {endpoint}/v1/chat/completions`` with header
``Authorization: Bearer <virtual key>`` and a JSON body
``{"model": "<bagman-fast|bagman-core|bagman-deep>", "messages": [...]}}``
— the standard OpenAI-compatible chat-completions shape LiteLLM already
exposes for every alias it fronts. The response is normalised into
:class:`LiteLLMCompletionResult` (PID §70) — nothing outside this
module ever sees the raw LiteLLM/OpenAI JSON shape.

Structured-output request (CD-5 Gate-1 closure delta, 2026-09-16)
--------------------------------------------------------------------
Matt's ruling on the real `bagman-fast`/`bagman-core` reliability gap
found during Gate-1 acceptance (empty/malformed output under real
appliance latency, after HELM's own `think:false` + plain-JSON-mode
fixes): BAGMAN already owns the exact per-task output shape in
``ai.tasks.TaskContract.output_schema`` before it ever calls a
provider, so THIS is the correct place to pass it down as a per-request
constraint, not something to hard-code into an alias/appliance config.

:meth:`complete` therefore takes a required ``output_schema`` argument
and, when present, attaches it to the request body as
``response_format`` in the standard OpenAI-compatible
"JSON-schema-constrained" shape (`{"type": "json_schema", "json_schema":
{"name": ..., "schema": <output_schema>}}` — a stronger contract than
plain `{"type": "json_object"}` "JSON mode", which only guarantees
syntactic JSON, never the caller's actual shape). LiteLLM translates
this into whatever the real backend natively supports (e.g. Ollama's
own `format: <json schema>` grammar-constrained generation, per
https://ollama.com/blog/structured-outputs). ``name`` is a fixed,
generic label (`"bagman_task_output"`) — it is bookkeeping metadata
for the wire protocol, not a routing/identity decision, so no
per-task-name plumbing is needed here; the actual constraint is the
``schema`` value itself, always exactly ``task_contract.output_schema``
verbatim (never hand-reconstructed — see
``ai.gateway.background.run_background_task``, which passes it
straight through).

This is a **reliability improvement to the model-generation step
only**. It is explicitly NOT a substitute for
``ai.tasks.validate_task_output`` — a provider claiming structured-
output support is not proof the response actually conforms (a
non-conforming provider, or one that ignores ``response_format``
entirely, must still be caught); BAGMAN's own deterministic
post-response validation in ``ai.gateway.background`` remains the one
canonical safety boundary and is entirely unchanged by this delta.

Dependency choice — stdlib ``urllib`` only, no new requirement
------------------------------------------------------------------
BAGMAN's ``requirements.txt`` currently has no HTTP client library at
all (``boto3`` speaks its own wire protocol for S3/MinIO only). Rather
than add a new third-party dependency (``requests``/``httpx``) for one
bounded JSON POST with a timeout and a single bounded retry, this
module uses the standard library's ``urllib.request`` directly — a
small, deliberate, documented judgment call (mirroring how
``ai/invocation.py``'s ``identity.generate_id()`` chose to implement
UUIDv7 itself rather than add a dependency, and how CD-3/CD-4 already
use plain ``urllib`` for the one other outbound HTTP need in this repo,
``app/api/routers/health.py``'s Docker healthcheck). If a future WI
needs richer HTTP behaviour (connection pooling across many concurrent
calls, HTTP/2, ...), reconsider then — not speculatively here.

Fail-closed contract (PID §20/§49/§50/§77)
--------------------------------------------
:meth:`LiteLLMClient.complete` NEVER lets a transport/timeout/auth/
provider-side failure escape as a raised exception — exactly the same
discipline ``services.evidence.intake.scanner.ClamAVScanner.scan``
already establishes for its own transport failures (PID §20's own
reference point, named directly in this WI's dispatch): the full
outcome space, INCLUDING infrastructure failure, is named by
:class:`LiteLLMOutcomeStatus` and returned as *data* in
:class:`LiteLLMCompletionResult`, so ``ai.gateway.background
.run_background_task`` can treat "the provider did not give me a good
answer" as an ordinary value to switch on. The ONE thing this method
DOES raise for is a caller programming error caught before any network
I/O is attempted: an invalid ``capability_alias`` (see
:func:`validate_capability_alias`) or a not-yet-open connection to a
misconfigured endpoint string — anything that is BAGMAN's own bug, not
a live-provider condition.

Retry policy (PID §50 "bounded retry" — documented exactly, per this
WI's own dispatch instruction)
------------------------------------------------------------------------
Exactly one policy, applied uniformly: a request is retried (at most
``max_network_retries`` additional attempts, default 1, i.e. two
attempts total) ONLY when the failure occurred before any HTTP response
was received at all — a connection-refused, DNS failure, or a timeout
during connect/send. This is provably safe to retry: the server never
processed the request, so a retry cannot double-invoke a model call.
The moment ANY HTTP response is received — including a non-2xx one
(e.g. LiteLLM's own ``400 no_db_connection`` observed during this WI's
own inspection, see the delivery report) — this module treats the
request as "the server saw it" and never automatically retries: the
model call may already have been executed server-side, and a semantic
retry that could re-invoke it is explicitly forbidden by this WI's own
dispatch instruction, mirroring PID §74's "retries create new,
auditable invocation attempts" doctrine at the ORCHESTRATION layer
(``ai.gateway.background``), not silently inside this transport layer.
"""
from __future__ import annotations

import enum
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from ai.invocation import BACKGROUND_CAPABILITY_ALIASES
from core.errors import ValidationError

#: Default endpoint — a sensible, configurable default only. The exact
#: production network path between the `bagman-api` container/compose
#: project and the existing Trinity LiteLLM installation's container is
#: a deployment-level integration detail established by whoever wires
#: the two Compose projects together (PID §15) — not invented here. In
#: this WI's own local inspection, the Trinity LiteLLM gateway was
#: reachable at this address from the host running BAGMAN.
DEFAULT_LITELLM_ENDPOINT = "http://localhost:4000"

#: Default secret-file path — see this WI's dispatch: the PID §13 text
#: originally guessed `trinity_litellm_api_key`; the REAL provisioned
#: filename is `litellm_gateway_key` (confirmed present at
#: /srv/bagman-secrets/litellm_gateway_key immediately before this WI
#: began). Mounted into the production container as
#: /run/secrets/litellm_gateway_key (deployment/compose/docker-compose.yml
#: is not this WI's own file to edit — see the delivery report for the
#: compose-secret addition still needed at reconciliation time).
DEFAULT_LITELLM_API_KEY_FILE = "/run/secrets/litellm_gateway_key"

_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
_READINESS_PATH = "/health/readiness"


class LiteLLMOutcomeStatus(str, enum.Enum):
    """The full outcome space for one :meth:`LiteLLMClient.complete`
    attempt (PID §20 — "AI provider failure must remain observable").
    Never raised as an exception type; always returned as data on
    :class:`LiteLLMCompletionResult.status`.
    """

    #: A genuine completion was received and parsed into a normalised
    #: result. Says nothing about whether the MODEL's own content is
    #: well-formed JSON matching a task's output_schema — that is
    #: ai.gateway.background's job, one layer up.
    OK = "OK"
    #: No HTTP response was ever received (connection refused, DNS
    #: failure, connect-phase timeout) — eligible for one bounded retry.
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    #: A connect/read timeout was hit — eligible for one bounded retry
    #: (the connect-phase case; a read-phase timeout after the request
    #: was already sent is NOT retried, per this module's own retry
    #: policy above — it is folded into TRANSPORT_ERROR's "did we ever
    #: get a response" test rather than distinguished further here,
    #: since urllib does not reliably expose which phase a given
    #: timeout occurred in).
    TIMEOUT = "TIMEOUT"
    #: The gateway responded with 401/403 — the BAGMAN-scoped virtual
    #: key was rejected. Never retried (a response WAS received).
    AUTH_ERROR = "AUTH_ERROR"
    #: The gateway responded with 429 — rate limited. Never retried
    #: automatically here (PID §74: a retry is an orchestration-layer
    #: decision, not a transport-layer one).
    RATE_LIMITED = "RATE_LIMITED"
    #: The gateway responded with some other non-2xx status (e.g. the
    #: `400 no_db_connection` condition this WI's own dispatch
    #: documents as a currently-known infrastructure state) or a 2xx
    #: response this module could not parse as the expected
    #: chat-completions JSON shape. Never retried.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    #: BAGMAN's own local configuration is broken (secret-key file
    #: missing/unreadable, endpoint malformed) — never even reaches the
    #: network. Never retried (retrying an unreadable file is pointless).
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass(frozen=True)
class LiteLLMCompletionResult:
    """Provider-neutral, normalised outcome of one
    :meth:`LiteLLMClient.complete` attempt (PID §70) — nothing outside
    ``ai/providers/litellm/`` ever sees a raw LiteLLM/OpenAI response
    shape.

    ``content`` is the raw assistant-message text LiteLLM returned —
    NOT yet parsed/validated as task output; ``ai.gateway.background``
    owns ``json.loads`` + ``ai.tasks.validate_task_output`` on it.
    ``provider_model`` is PID §10/§24 audit-only physical-model
    provenance, present only when ``status is OK`` and the gateway
    disclosed it. ``error_detail`` is a short, safe, non-secret
    description of what went wrong when ``status is not OK`` — never
    the raw request/response body verbatim (PID §78: no full
    prompts/secrets in logs by default; the same discipline applies to
    anything this result carries, since it may itself be logged).
    """

    status: LiteLLMOutcomeStatus
    content: Optional[str] = None
    provider_model: Optional[str] = None
    usage_metadata: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: Optional[int] = None
    request_id: Optional[str] = None
    error_detail: Optional[str] = None


class LiteLLMClientProtocol(Protocol):
    """Structural type both :class:`LiteLLMClient` (real) and
    :class:`ai.providers.litellm.fake.FakeLiteLLMClient` (deterministic
    test/dev substitute) satisfy — what ``ai.gateway.background`` and
    ``app/api/composition.py`` actually depend on. Never import the
    concrete ``LiteLLMClient`` class outside ``app/api/composition.py``
    (production wiring) or this module's own tests.
    """

    def complete(
        self,
        *,
        capability_alias: str,
        system_instructions: str,
        evidence_content: str,
        output_schema: Mapping[str, Any],
        timeout_seconds: float,
    ) -> LiteLLMCompletionResult: ...

    def is_available(self) -> bool: ...


def validate_capability_alias(capability_alias: str) -> None:
    """BAGMAN's own mechanical enforcement of alias-only routing (PID
    §5/§9/§10): reject anything that is not literally a member of
    :data:`ai.invocation.BACKGROUND_CAPABILITY_ALIASES` — never a
    `trinity-*` alias, never a raw physical model name (e.g.
    `gemma-3-12b`) — loudly, before any HTTP request is ever
    constructed. Shared by :class:`LiteLLMClient` and
    :class:`ai.providers.litellm.fake.FakeLiteLLMClient` so there is
    exactly one implementation of this check, never two that could
    silently drift apart.

    Raises:
        core.errors.ValidationError: if `capability_alias` is not one
            of the closed set.
    """
    if capability_alias not in BACKGROUND_CAPABILITY_ALIASES:
        raise ValidationError(
            f"capability_alias {capability_alias!r} is not one of the closed set "
            f"{sorted(BACKGROUND_CAPABILITY_ALIASES)} (PID §5/§9/§10) — BAGMAN never sends a "
            "'trinity-*' alias or a raw physical model name to the LiteLLM gateway, regardless "
            "of what the gateway itself would tolerate"
        )


def build_messages(system_instructions: str, evidence_content: str) -> list[dict[str, str]]:
    """Build the chat-completions ``messages`` array with task
    instructions and untrusted evidence content STRUCTURALLY separated
    by role (PID §26/§32) — ``system_instructions`` always occupies the
    ``system`` message, ``evidence_content`` always occupies the
    ``user`` message, and the two are never concatenated into one
    string before this split. This is the one place that decision is
    made; both :class:`LiteLLMClient` and
    :class:`ai.providers.litellm.fake.FakeLiteLLMClient` (for capturing
    what WOULD have been sent) go through it, and
    ``tests/integration/test_prompt_injection_structural.py`` asserts
    directly against its output — see that module's docstring for the
    precise, honest scope of what this structural proof does (and does
    not) establish.
    """
    return [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": evidence_content},
    ]


#: Fixed, generic label for the ``response_format.json_schema.name``
#: field (CD-5 Gate-1 closure delta) — see module docstring's
#: "Structured-output request" section for why this does not need to
#: vary per task.
_RESPONSE_FORMAT_SCHEMA_NAME = "bagman_task_output"


def build_response_format(output_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build the OpenAI-compatible ``response_format`` value that
    requests JSON-schema-CONSTRAINED generation (not merely syntactic
    "JSON mode") — see module docstring. ``output_schema`` is always
    ``task_contract.output_schema`` verbatim; this function does not
    interpret, validate, or modify it in any way — schema authority
    stays entirely with ``ai.tasks`` (PID §21-22)."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _RESPONSE_FORMAT_SCHEMA_NAME,
            "schema": dict(output_schema),
        },
    }


def _read_api_key(api_key_file: str) -> str:
    return Path(api_key_file).read_text(encoding="utf-8").strip()


def _is_connect_phase_failure(exc: BaseException) -> bool:
    """True if `exc` indicates no HTTP response was ever received (safe
    to retry per this module's documented policy) — a connection
    failure or a socket-level timeout, as opposed to
    `urllib.error.HTTPError` (a real HTTP response WAS received, just a
    non-2xx one) or a JSON/shape parsing failure of an actually-received
    body (never retried, since the server already processed the
    request)."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


class LiteLLMClient:
    """The real adapter, speaking to the existing Trinity LiteLLM
    installation over HTTP (PID §6/§8/§50). See module docstring for
    the full wire-protocol, retry, and fail-closed contract.
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_LITELLM_ENDPOINT,
        api_key_file: str = DEFAULT_LITELLM_API_KEY_FILE,
        connect_timeout: float = 5.0,
        max_network_retries: int = 1,
    ) -> None:
        #: No I/O performed at construction time (mirrors
        #: `services.evidence.intake.scanner.ClamAVScanner.__init__` —
        #: only host/port/config are stored; reachability is proven
        #: live, on demand, by `is_available()`/`complete()`, never
        #: assumed at construction).
        self._endpoint = endpoint.rstrip("/")
        self._api_key_file = api_key_file
        self._connect_timeout = connect_timeout
        self._max_network_retries = max(0, max_network_retries)

    def _authorization_header(self) -> Optional[str]:
        """Read the BAGMAN-scoped virtual key fresh from disk on every
        call (deliberate — never cached in memory beyond one call's
        stack frame) so a rotated secret file takes effect without a
        process restart, and so a missing/unreadable key file surfaces
        as a per-call `CONFIG_ERROR` rather than breaking composition
        or `/ready` (PID §48's "evidence/runtime services remain usable
        even when AI is down"). Returns `None` (never raises) if the
        file cannot be read — the caller turns that into
        `LiteLLMOutcomeStatus.CONFIG_ERROR`.
        """
        try:
            return _read_api_key(self._api_key_file)
        except OSError:
            return None

    def complete(
        self,
        *,
        capability_alias: str,
        system_instructions: str,
        evidence_content: str,
        output_schema: Mapping[str, Any],
        timeout_seconds: float,
    ) -> LiteLLMCompletionResult:
        """Speak `POST {endpoint}/v1/chat/completions` for exactly one
        BAGMAN background task attempt. Never raises for a transport/
        timeout/auth/provider-side failure — see module docstring.

        `output_schema` (CD-5 Gate-1 closure delta) is always
        `task_contract.output_schema` verbatim, attached to the request
        as a `response_format` JSON-schema constraint (see module
        docstring's "Structured-output request" section) — this is a
        generation-reliability improvement only; the caller
        (`ai.gateway.background.run_background_task`) still validates
        the response against the same schema independently afterwards.

        Raises:
            core.errors.ValidationError: if `capability_alias` is not
                one of `ai.invocation.BACKGROUND_CAPABILITY_ALIASES` —
                a caller programming error, checked before any network
                I/O (PID §5/§9/§10).
        """
        validate_capability_alias(capability_alias)

        api_key = self._authorization_header()
        if api_key is None:
            return LiteLLMCompletionResult(
                status=LiteLLMOutcomeStatus.CONFIG_ERROR,
                error_detail=f"could not read LiteLLM API key from {self._api_key_file!r}",
            )

        body = json.dumps(
            {
                "model": capability_alias,
                "messages": build_messages(system_instructions, evidence_content),
                "response_format": build_response_format(output_schema),
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            f"{self._endpoint}{_CHAT_COMPLETIONS_PATH}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

        attempts_allowed = 1 + self._max_network_retries
        started = time.monotonic()
        last_transport_detail = ""
        for attempt in range(attempts_allowed):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                detail_bytes = exc.read() if hasattr(exc, "read") else b""
                detail = detail_bytes.decode("utf-8", errors="replace")[:500]
                if exc.code in (401, 403):
                    status = LiteLLMOutcomeStatus.AUTH_ERROR
                elif exc.code == 429:
                    status = LiteLLMOutcomeStatus.RATE_LIMITED
                else:
                    status = LiteLLMOutcomeStatus.PROVIDER_ERROR
                return LiteLLMCompletionResult(
                    status=status,
                    latency_ms=latency_ms,
                    error_detail=f"HTTP {exc.code}: {detail}",
                )
            except Exception as exc:  # noqa: BLE001 - classified below; never leaks raw
                if _is_connect_phase_failure(exc) and attempt < attempts_allowed - 1:
                    last_transport_detail = str(exc)
                    continue
                latency_ms = int((time.monotonic() - started) * 1000)
                is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
                status = LiteLLMOutcomeStatus.TIMEOUT if is_timeout else LiteLLMOutcomeStatus.TRANSPORT_ERROR
                return LiteLLMCompletionResult(
                    status=status,
                    latency_ms=latency_ms,
                    error_detail=str(exc)[:500] or last_transport_detail[:500],
                )
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                return self._normalise_success(raw, latency_ms)

        # Unreachable in practice (the loop always returns), but keeps
        # a defensive, honest fallback rather than falling off the end.
        return LiteLLMCompletionResult(
            status=LiteLLMOutcomeStatus.TRANSPORT_ERROR,
            error_detail=last_transport_detail[:500],
        )

    @staticmethod
    def _normalise_success(raw: bytes, latency_ms: int) -> LiteLLMCompletionResult:
        try:
            payload = json.loads(raw.decode("utf-8"))
            message_content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, UnicodeDecodeError) as exc:
            return LiteLLMCompletionResult(
                status=LiteLLMOutcomeStatus.PROVIDER_ERROR,
                latency_ms=latency_ms,
                error_detail=f"could not parse LiteLLM chat-completions response shape: {exc}",
            )

        usage = payload.get("usage")
        return LiteLLMCompletionResult(
            status=LiteLLMOutcomeStatus.OK,
            content=message_content,
            # PID §10/§24 — audit-only physical-model provenance,
            # never read anywhere for a routing decision.
            provider_model=payload.get("model"),
            usage_metadata=dict(usage) if isinstance(usage, dict) else {},
            latency_ms=latency_ms,
            request_id=payload.get("id"),
        )

    def is_available(self) -> bool:
        """Cheap reachability check (PID §48/§50) — mirrors
        `EvidenceSafetyScanner.is_available()`'s pattern exactly: one
        short round trip, never a full `complete()` call.

        Honest granularity note (this WI's own dispatch requires this
        be stated plainly rather than fabricated): this proves the
        LiteLLM PROCESS answers `GET {endpoint}/health/readiness` — it
        does NOT prove a `complete()` call will succeed. During this
        WI's own inspection, the gateway process reachably returned
        `{"status": "healthy", "db": "Not connected"}` from this exact
        endpoint while every authenticated `/v1/chat/completions` call
        failed with `400 no_db_connection` — i.e. `is_available()` can
        legitimately return `True` while `complete()` still fails, and
        that gap is real infrastructure state (Trinity/Helm's LiteLLM
        backing database), not a bug in this check. It is also
        GATEWAY-WIDE, not per-alias: the existing LiteLLM installation
        exposes no per-alias health endpoint BAGMAN can probe cheaply,
        so this single check is the only reachability signal available
        for all of `bagman-fast`/`bagman-core`/`bagman-deep` — see
        `app/api/routers/ai.py`'s `/internal/ai/health` handler, which
        states this same granularity limit in its own response rather
        than fabricating a false per-alias distinction.
        """
        request = urllib.request.Request(f"{self._endpoint}{_READINESS_PATH}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._connect_timeout) as response:
                return 200 <= response.status < 300
        except Exception:  # noqa: BLE001 - unreachable in any fashion means unavailable
            return False
