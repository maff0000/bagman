"""Content-safety scanning abstraction (PID §19/§20, CD-4 WI-2).

``EvidenceSafetyScanner`` is the abstraction
``services.evidence.intake.validation_pipeline`` depends on — never a
hardcoded antivirus implementation detail (PID §19). This module
provides exactly one concrete production implementation,
:class:`ClamAVScanner`, talking to a ``clamd`` daemon (a
``bagman-scan``-style container in deployment, PID §20) over its
native TCP protocol.

Why a raw socket client instead of the ``clamd`` PyPI package
----------------------------------------------------------------
PID §20 explicitly allows either "the ``clamd`` Python package or a
raw socket protocol implementation". The ``clamd`` protocol
(``INSTREAM``/``PING``) is small, stable, and already well-documented;
implementing it directly here (see :class:`ClamAVScanner`) avoids
adding a new third-party dependency for what is, in practice, about 40
lines of socket code, and keeps the entire scanning transport
auditable in one file. Unlike the ``python-magic``/``libmagic``
question in ``content_sniffing.py``, this is not a "some doubt, so
avoid it" call — the ``clamd`` package itself is pure Python with no
system-library dependency either way, so either choice would have been
fine; the raw protocol was chosen simply because it was already fully
prototyped and verified against a real daemon during this delivery
(see the delivery report), and needs nothing beyond the standard
library's own ``socket`` module.

Fail-closed contract (PID §20)
--------------------------------
:meth:`EvidenceSafetyScanner.scan` NEVER lets a transport-level
failure (connection refused, timeout, protocol error) escape as a
raised exception to a normal caller — it is caught internally and
returned as ``ScanVerdict.SCAN_ERROR`` (with a human-readable
``detail``). This is deliberate: :data:`ScanVerdict` names the full
outcome space (PID §19) INCLUDING infrastructure failure, so a caller
(``validation_pipeline``) can treat "the scanner did not give me an
answer" as an ordinary value to switch on, rather than needing a
``try/except`` around every call. The pipeline is what actually
enforces "never silently mean CLEAN" by routing a non-``CLEAN``
verdict away from ``ACCEPTED`` — this module's own responsibility ends
at honestly reporting what happened.
"""
from __future__ import annotations

import abc
import enum
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union

#: Read/write chunk size when streaming a spooled file's bytes to
#: clamd via INSTREAM — bounded, so scanning never loads an entire
#: large file into memory at once either (PID §13's streaming doctrine
#: applies here just as much as to the initial spool).
_STREAM_CHUNK_SIZE = 64 * 1024


class ScanVerdict(str, enum.Enum):
    """The closed verdict vocabulary PID §19 names."""

    CLEAN = "CLEAN"
    SUSPICIOUS = "SUSPICIOUS"
    MALICIOUS = "MALICIOUS"
    UNSUPPORTED = "UNSUPPORTED"
    SCAN_ERROR = "SCAN_ERROR"


@dataclass(frozen=True)
class ScanResult:
    """One scan outcome. ``detail`` is a short, human-readable
    explanation (a matched signature name, an error message, ...) —
    never included in any downstream canonical field verbatim without
    thought, since a signature/error string is scanner-controlled
    advisory output (PID §62: "scanner output is advisory infrastructure
    output"), not canonical BAGMAN domain truth."""

    verdict: ScanVerdict
    detail: str = ""


class EvidenceSafetyScanner(abc.ABC):
    """Abstraction ``services.evidence.intake.validation_pipeline``
    depends on (PID §19). Canonical intake/evidence logic never imports
    a concrete scanner implementation directly."""

    @abc.abstractmethod
    def scan(self, content: Union[Path, bytes]) -> ScanResult:
        """Scan ``content`` (either a path to a file already spooled on
        disk, or a small in-memory byte string — a real upload always
        goes through the ``Path`` form via
        ``services.evidence.intake.streaming.spool_stream``; the
        ``bytes`` form exists for small synthetic test fixtures and
        any future caller that already holds bytes in memory).

        Never raises for an ordinary scan-transport failure — returns
        :attr:`ScanVerdict.SCAN_ERROR` instead (see module docstring).
        May raise for a genuine programming error (e.g. a path that
        does not exist), which is not the same thing as "the scanner
        could not reach a verdict".
        """
        raise NotImplementedError

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Cheap reachability/health check — PID §60's own suggestion
        that a mandatory scanner "should be part of `/ready`
        readiness"; wiring an actual ``/ready`` check is WI-3's job
        (``app/api/routers/health.py`` lives outside this work item's
        scope), but this method is deliberately cheap (one round trip,
        no full scan) so WI-3 can call it directly from that endpoint.
        """
        raise NotImplementedError


class ClamAVScanner(EvidenceSafetyScanner):
    """``EvidenceSafetyScanner`` backed by a real ``clamd`` daemon,
    speaking its native ``INSTREAM``/``PING`` protocol directly over
    TCP (PID §20).

    ``host``/``port`` point at the daemon — in deployment, a
    ``bagman-scan``-style container's ``clamd`` (default port 3310);
    for local development/testing, any reachable clamd instance (see
    the delivery report for the disposable Docker container used to
    build and test this class against a REAL daemon).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3310,
        *,
        connect_timeout: float = 5.0,
        scan_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._scan_timeout = scan_timeout

    def _connect(self, timeout: float) -> socket.socket:
        return socket.create_connection((self._host, self._port), timeout=timeout)

    def is_available(self) -> bool:
        try:
            with self._connect(self._connect_timeout) as sock:
                sock.sendall(b"zPING\0")
                response = sock.recv(64)
        except OSError:
            return False
        return response.strip(b"\0") == b"PONG"

    def _iter_content_chunks(self, content: Union[Path, bytes]) -> Iterator[bytes]:
        if isinstance(content, (bytes, bytearray)):
            if content:
                yield bytes(content)
            return
        with open(content, "rb") as handle:
            while True:
                chunk = handle.read(_STREAM_CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk

    def scan(self, content: Union[Path, bytes]) -> ScanResult:
        try:
            with self._connect(self._connect_timeout) as sock:
                sock.settimeout(self._scan_timeout)
                sock.sendall(b"zINSTREAM\0")
                for chunk in self._iter_content_chunks(content):
                    sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
                sock.sendall((0).to_bytes(4, "big"))  # zero-length chunk terminates INSTREAM

                response = b""
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    response += data
        except (OSError, socket.timeout) as exc:
            return ScanResult(ScanVerdict.SCAN_ERROR, detail=f"clamd transport failure: {exc}")

        return self._parse_instream_response(response)

    @staticmethod
    def _parse_instream_response(response: bytes) -> ScanResult:
        text = response.strip(b"\0").decode("utf-8", errors="replace").strip()
        # clamd INSTREAM replies look like:
        #   "stream: OK"
        #   "stream: Eicar-Test-Signature FOUND"
        #   "stream: <message> ERROR"
        #   "INSTREAM size limit exceeded. ERROR" (no "stream:" prefix)
        if text.endswith("OK"):
            return ScanResult(ScanVerdict.CLEAN, detail=text)
        if text.endswith("ERROR"):
            return ScanResult(ScanVerdict.SCAN_ERROR, detail=text)
        if "FOUND" in text:
            signature = text
            if ":" in text:
                signature = text.split(":", 1)[1].strip()
            signature = signature[: -len(" FOUND")] if signature.endswith(" FOUND") else signature
            verdict = ScanVerdict.SUSPICIOUS if "heuristics" in signature.lower() else ScanVerdict.MALICIOUS
            return ScanResult(verdict, detail=signature)
        if not text:
            return ScanResult(ScanVerdict.SCAN_ERROR, detail="empty response from clamd")
        return ScanResult(ScanVerdict.UNSUPPORTED, detail=text)
