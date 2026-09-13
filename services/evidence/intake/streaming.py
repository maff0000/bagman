"""Bounded streaming/spooling ingest (PID §13/§23, CD-4 WI-2).

CD-3's route read an entire uploaded file into memory before doing
anything with it — an unbounded upload could exhaust process memory.
:func:`spool_stream` fixes that: it consumes a caller-supplied byte
stream (anything with a ``.read(n)`` method, e.g. an open file or an
HTTP request body reader, OR a plain iterator/generator of ``bytes``
chunks — whichever shape a future WI-3 HTTP layer finds easiest to
hand it) and, chunk by chunk:

* accumulates a running total and aborts — raising
  :class:`core.errors.FileTooLargeError` — the moment that total
  exceeds ``max_size_bytes``, WITHOUT first buffering the rest of an
  oversized stream (PID §13's explicit requirement);
* updates a SHA-256 hash incrementally (PID §23) — the same hash
  computed here is what the rest of the intake pipeline uses
  everywhere downstream (content-hash on the ``IntakeRecord``, the
  quarantine/staging object-store key, and eventually the
  ``EvidenceItem`` this intake produces), never a re-derived one;
* writes each chunk to a bounded temporary file.

BAGMAN-owned temporary storage (PID §13)
------------------------------------------
The spool file is created under a BAGMAN-owned directory (never the
repository, never a shared/system-wide ``/tmp`` used by anything else),
resolved by :func:`spool_root`:

* ``BAGMAN_INTAKE_SPOOL_DIR`` environment variable if set (the
  production/deployment override — a composition root can point this
  at a dedicated volume); otherwise
* ``<tempfile.gettempdir()>/bagman-intake-spool`` — created with
  ``0700`` permissions on first use.

This directory holds only transient, non-canonical material (PID §13:
"bounded", "BAGMAN-owned", "non-canonical", "never committed", "never
silently treated as durable evidence"). :func:`spool_stream` is a
context manager: the spooled file is deleted in a ``finally`` block on
BOTH the success and the failure/exception path — including the
``FileTooLargeError`` abort path itself — so no partial or completed
spool file is ever left behind once the ``with`` block exits, and the
directory never accumulates content across calls. Nothing under this
directory is ever treated as durable/canonical: the pipeline
(``services.evidence.intake.validation_pipeline``) only ever persists
bytes by explicitly handing them to an object store BEFORE this context
manager exits.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Protocol, Union

from core.errors import FileTooLargeError

#: Chunk size used both for reading a ``.read(n)``-shaped stream and
#: for reading a spooled file back off disk later (e.g. to hand to a
#: scanner or object store). 64 KiB is a conventional, small, bounded
#: buffer size — large enough to avoid excessive syscall overhead,
#: small enough that memory use never scales with file size.
DEFAULT_CHUNK_SIZE = 64 * 1024

_SPOOL_DIR_ENV_VAR = "BAGMAN_INTAKE_SPOOL_DIR"
_SPOOL_DIR_NAME = "bagman-intake-spool"


class _ReadableStream(Protocol):
    def read(self, size: int = ...) -> bytes: ...


#: What :func:`spool_stream` accepts: either a file-like object
#: exposing ``.read(n)``, or a plain iterable of ``bytes`` chunks (e.g.
#: a generator yielding pieces of an HTTP request body) — the shape a
#: caller has is used as-is rather than forcing one convention.
StreamLike = Union[_ReadableStream, Iterable[bytes]]


def spool_root() -> Path:
    """The BAGMAN-owned directory spooled intake files are created
    under (see module docstring). Created with ``0700`` permissions if
    it does not already exist."""
    configured = os.environ.get(_SPOOL_DIR_ENV_VAR)
    root = Path(configured) if configured else Path(tempfile.gettempdir()) / _SPOOL_DIR_NAME
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@dataclass(frozen=True)
class SpooledContent:
    """The result of successfully spooling a stream (PID §13/§23).

    ``path`` is only valid for the lifetime of the ``with
    spool_stream(...) as spooled:`` block that produced it — it is
    deleted the moment that block exits (success or failure).
    """

    path: Path
    size_bytes: int
    sha256_hex: str


def _iter_chunks(stream: StreamLike, chunk_size: int) -> Iterator[bytes]:
    read = getattr(stream, "read", None)
    if callable(read):
        while True:
            chunk = read(chunk_size)
            if not chunk:
                return
            yield chunk
        return
    # Not a `.read()`-shaped object — treat as an iterable of chunks
    # already produced by the caller (e.g. an ASGI request body
    # iterator). Each chunk's own size is the caller's choice; this
    # function still enforces the running-total limit after every
    # chunk regardless of how large any individual one is.
    for chunk in stream:
        yield chunk


@contextlib.contextmanager
def spool_stream(
    stream: StreamLike,
    *,
    max_size_bytes: int,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[SpooledContent]:
    """Stream ``stream`` to a bounded, BAGMAN-owned temporary file,
    enforcing ``max_size_bytes`` incrementally and hashing incrementally
    (PID §13/§23).

    Raises :class:`core.errors.FileTooLargeError` the moment the
    running total exceeds ``max_size_bytes`` — the partial spool file
    is still cleaned up (see module docstring), and no more of the
    source stream is read once the limit is exceeded.

    An empty stream (zero bytes) is valid and yields a
    :class:`SpooledContent` with ``size_bytes=0`` and the well-known
    SHA-256 of the empty byte string — callers decide separately
    whether an empty upload is otherwise acceptable (PID §63's "empty
    file" fixture case).
    """
    if max_size_bytes <= 0:
        raise ValueError(f"max_size_bytes must be positive, got {max_size_bytes}")

    root = spool_root()
    spool_path = root / f"intake-{uuid.uuid4().hex}.spool"
    try:
        hasher = hashlib.sha256()
        total = 0
        with open(spool_path, "wb") as handle:
            for chunk in _iter_chunks(stream, chunk_size):
                total += len(chunk)
                if total > max_size_bytes:
                    raise FileTooLargeError(
                        f"intake stream exceeded the configured limit of "
                        f"{max_size_bytes} bytes; aborted after reading at least "
                        f"{total} bytes (PID §12/§13) — the stream was not "
                        "buffered in full before this check"
                    )
                hasher.update(chunk)
                handle.write(chunk)

        yield SpooledContent(path=spool_path, size_bytes=total, sha256_hex=hasher.hexdigest())
    finally:
        # Cleaned up on every path — success, FileTooLargeError, or any
        # other exception raised by the caller while the `with` block
        # was open (PID §13: "cleaned on success/failure").
        spool_path.unlink(missing_ok=True)
