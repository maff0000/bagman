"""Structured logging setup for ``bagman-api`` (PID §27).

A single, small, well-scoped responsibility: configure Python's
standard ``logging`` module so every log line the application container
emits is one JSON object on stdout (Docker/Compose captures container
stdout as its log stream; nothing here writes to a file).

Emitted fields, where applicable/known for a given record:

* ``timestamp_utc``     — always present, always UTC (PID §28).
* ``level``              — always present.
* ``component``          — the logger name unless a call site passes an
  explicit ``component=`` via ``extra=``.
* ``message``            — always present.
* ``correlation_id``     — present when a call site passes one via
  ``extra={"correlation_id": ...}`` (the request middleware in
  ``main.py`` does this for every request-scoped log line).
* ``canonical_object_id`` — present when a call site names the
  canonical object a log line concerns (e.g. an ``evidence_id``).
* ``event_type``         — present when a call site names a discrete
  event (e.g. an ``AuditEvent.event_type`` value, or an internal event
  such as ``"READINESS_CHECK_FAILED"``).

What this module never logs, by construction (PID §27): nobody in
``app/api/`` passes a secret, credential, full document/evidence
body, or full email body as a log message or `extra` field — this
formatter has no special redaction logic of its own because the
discipline is "never construct the log call with that content in the
first place", not "scrub it afterwards". A stack trace (``exc_info``)
is rendered only into this structured *server-side* log record, never
into an HTTP response body (see ``main.py``'s exception handlers).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

#: Extra fields a call site may attach via `extra={...}`; included in
#: the rendered JSON only when actually present on the LogRecord (never
#: rendered as a literal `null`/empty placeholder).
_OPTIONAL_EXTRA_FIELDS = ("correlation_id", "canonical_object_id", "event_type", "component")

#: A stack trace is genuinely useful server-side but can be long;
#: capped defensively so one runaway traceback can never dominate a log
#: stream (mirrors the same defensive-capping judgment
#: `persistence/objects/minio_store.py` already applies to provider
#: error strings).
_MAX_EXC_INFO_CHARS = 4000


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp_utc": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "message": record.getMessage(),
        }
        for field in _OPTIONAL_EXTRA_FIELDS:
            if field == "component":
                continue  # already resolved into `component` above
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)[:_MAX_EXC_INFO_CHARS]

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    """Configure the ROOT logger with the JSON formatter above.

    Idempotent: safe to call more than once (e.g. once from
    ``main.py`` at import time and once again from a test fixture) —
    existing handlers on the root logger are replaced, not
    accumulated, so log lines are never duplicated.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)

    # uvicorn's own loggers otherwise emit their own (non-JSON) access
    # line format; route them through the same JSON formatter/handler
    # rather than suppressing them, so container logs stay one
    # consistent shape end to end.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
