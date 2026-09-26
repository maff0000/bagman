"""``ImapClient`` — the real adapter that speaks plain IMAP to
``mail.noust.ai`` over implicit TLS (CD-6 GUI-operations-foundation
follow-on WO — second mailbox provider).

Mirrors ``services.mailbox.microsoft.graph_client``'s own conventions:
stdlib only (``imaplib``/``ssl`` — no new third-party dependency; Python
does not ship a well-established third-party IMAP library that is
already a BAGMAN dependency, and stdlib ``imaplib`` is sufficient),
never raises for a transport/protocol-level failure (the full outcome
space is returned as data via :class:`ImapOutcomeStatus`), no eager I/O
at construction time (a fresh connection is opened per real operation —
IMAP connections are cheap and short-lived here, unlike an OAuth access
token; there is no "session pool" to manage).

Hard security requirements — non-negotiable, read before touching this
file
------------------------------------------------------------------------
* :meth:`ImapClient.connect_and_login` connects via ``imaplib.IMAP4_SSL``
  (implicit TLS on port 993, the standard secure IMAP port) using
  ``ssl.create_default_context()`` — full certificate chain validation
  AND hostname verification, both ON by default for a context built this
  way. This module NEVER sets ``context.check_hostname = False``, NEVER
  sets ``context.verify_mode = ssl.CERT_NONE``, and NEVER passes any
  other cert-bypass flag anywhere. A TLS/certificate/hostname validation
  failure surfaces as the DISTINCT, dedicated
  :data:`ImapOutcomeStatus.TLS_VALIDATION_FAILED` outcome — never
  silently retried with weaker settings, never falls back to plaintext.
* STARTTLS-on-port-143 is NOT implemented anywhere in this module —
  implicit TLS on 993 only. If port 993 is unreachable, that is a
  :data:`ImapOutcomeStatus.TRANSPORT_ERROR`/``TIMEOUT`` to report, never
  a reason to fall back to an insecure transport. There is no plaintext
  IMAP code path anywhere in this codebase.
* Every per-folder read uses ``EXAMINE`` (via ``imaplib``'s own
  ``select(mailbox, readonly=True)``, which sends the real IMAP
  ``EXAMINE`` command, never ``SELECT``) — a stronger, PROTOCOL-ENFORCED
  read-only guarantee than merely avoiding write commands: the server
  itself refuses to accept a state-mutating command against an EXAMINEd
  mailbox. This client re-``EXAMINE``s the target folder at the start of
  every per-folder method (search/fetch) rather than caching a "current
  folder" — one extra bounded round trip per call, in exchange for every
  method being independently correct/stateless (never dependent on a
  previous call having already selected the right folder).
* :meth:`uid_fetch_headers` uses ``UID FETCH ... (UID BODY.PEEK[HEADER])``
  and :meth:`uid_fetch_content` uses ``UID FETCH ... (UID BODY.PEEK[])``
  — ``BODY.PEEK`` ALWAYS, never plain ``BODY``, since ``BODY`` implicitly
  sets the ``\\Seen`` flag as a side effect of fetching — explicitly
  forbidden (architect: "must NOT mark messages read merely as a side
  effect of processing").
* :meth:`uid_search` always issues ``UID SEARCH`` (via ``imaplib``'s own
  ``uid("SEARCH", ...)``), NEVER plain ``SEARCH`` (which returns
  sequence numbers, not UIDs — the wrong identity space entirely for
  this delivery's own UID/UIDVALIDITY message-identity doctrine — see
  ``services/mailbox/imap/imap_adapter.py``'s own module docstring).
* This client NEVER issues any IMAP command that mutates state: no
  ``STORE`` (flag changes), no ``COPY``/``MOVE``, no ``EXPUNGE``, no
  ``APPEND``, no ``DELETE``, and no generic "send a raw IMAP command"
  escape hatch. These methods do not exist ANYWHERE on this class — their
  absence IS the read-only guarantee (nothing to accidentally call),
  not merely a convention this module happens to follow.
* The password is NEVER logged, NEVER placed in an ``error_detail``
  string, NEVER placed in any exception message this module raises or
  returns as data. :func:`_redact` scrubs the password out of any
  exception text that might otherwise echo it (a defensive measure —
  ``imaplib``'s own exceptions typically only echo the SERVER's
  response, never the client-sent password, but this module does not
  rely on that being true of every IMAP server/library version).
* Every connection is closed on every exit path (success or failure) —
  :meth:`logout` is always reachable from a `finally`/explicit call at
  every real call site in this module and in
  ``services/mailbox/imap/imap_adapter.py``; no leaked sockets.

Outcome-status pattern (mirrors ``graph_client.GraphOutcomeStatus``)
------------------------------------------------------------------------
:class:`ImapOutcomeStatus` deliberately reuses the SAME member names AND
string VALUES as
``services.mailbox.microsoft.graph_client.GraphOutcomeStatus`` for every
member the two providers share (``OK``/``TRANSPORT_ERROR``/``TIMEOUT``/
``AUTH_ERROR``/``PERMISSION_ERROR``/``NOT_FOUND``/``RATE_LIMITED``/
``RESYNC_REQUIRED``/``PROVIDER_ERROR``/``MALFORMED_RESPONSE``/
``CONFIG_ERROR``), plus one IMAP-specific addition
(``TLS_VALIDATION_FAILED``). This is a DELIBERATE, documented judgment
call, not an accident: both are ``(str, enum.Enum)`` mixins, so a
member of one compares EQUAL to the same-named/valued member of the
other via plain string equality (``ImapOutcomeStatus.AUTH_ERROR ==
GraphOutcomeStatus.AUTH_ERROR`` is `True`) — this is what lets
``services/mailbox/sweep.py``'s existing status comparisons
(``page.status == GraphOutcomeStatus.RATE_LIMITED`` etc.) keep working
UNCHANGED for an IMAP-sourced result, with ZERO changes to that file's
own status-comparison logic. IMAP never actually PRODUCES
``RATE_LIMITED``/``PERMISSION_ERROR``/``RESYNC_REQUIRED`` (the protocol
has no equivalents this client recognises) — those members exist purely
so the shared vocabulary lines up; the sweep engine's own handling for
those codes simply never triggers for an IMAP mailbox, which is
correct. A module-level `ImapOutcomeStatus` (rather than importing
`GraphOutcomeStatus` from the Microsoft package directly) keeps this
package free of any runtime dependency on `services.mailbox.microsoft`
— a deliberate sibling, never a generalisation (mirrors
`services.mailbox.microsoft.secrets`'s own "duplicated rather than risk
the already-deployed [other provider]" reasoning, applied here in the
opposite direction: IMAP must not destabilise or entangle with the
already-live Microsoft pipeline).
"""
from __future__ import annotations

import email
import enum
import imaplib
import re
import socket
import ssl
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence

DEFAULT_TIMEOUT_SECONDS = 15.0
#: The standard, secure implicit-TLS IMAP port — the ONLY port this
#: module ever connects to (see module docstring's "no STARTTLS on 143"
#: requirement).
DEFAULT_IMAP_PORT = 993


class ImapOutcomeStatus(str, enum.Enum):
    """The full outcome space for one IMAP operation — never raised as
    an exception; always returned as data. See module docstring's
    "Outcome-status pattern" section for why these member names/values
    deliberately mirror `graph_client.GraphOutcomeStatus`."""

    OK = "OK"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    #: Login rejected (bad username/password, or account/mailbox access
    #: denied at the IMAP layer).
    AUTH_ERROR = "AUTH_ERROR"
    #: IMAP has no real analogue of a Graph-style 403 scope error — kept
    #: only for vocabulary parity with `GraphOutcomeStatus` (see module
    #: docstring); this client never produces it.
    PERMISSION_ERROR = "PERMISSION_ERROR"
    #: The requested UID no longer exists in this folder (vanished
    #: between SEARCH and FETCH).
    NOT_FOUND = "NOT_FOUND"
    #: IMAP has no standard rate-limit signal — kept only for vocabulary
    #: parity; this client never produces it.
    RATE_LIMITED = "RATE_LIMITED"
    #: IMAP has no delta-token-expiry concept (UID/UIDVALIDITY is a
    #: durable identity scheme, not a rotating cursor token) — kept only
    #: for vocabulary parity; this client never produces it.
    RESYNC_REQUIRED = "RESYNC_REQUIRED"
    #: Any other non-OK, well-formed IMAP command failure.
    PROVIDER_ERROR = "PROVIDER_ERROR"
    #: A response this client could not parse into the expected shape.
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    #: BAGMAN's own local configuration is broken (no username/password
    #: on disk yet) — never even reaches the network.
    CONFIG_ERROR = "CONFIG_ERROR"
    #: IMAP-specific addition (no Graph analogue) — a genuine TLS
    #: handshake / certificate-chain / hostname-verification failure.
    #: See module docstring's "Hard security requirements" section —
    #: this is a DISTINCT outcome from `TRANSPORT_ERROR` precisely so a
    #: caller can treat it as a whole-sweep-stopping condition, never an
    #: ordinary retry-next-time transient failure.
    TLS_VALIDATION_FAILED = "TLS_VALIDATION_FAILED"


def _redact(text: str, *secrets: Optional[str]) -> str:
    """Scrub every non-empty value in `secrets` out of `text` — used on
    every code path that renders an exception into an `error_detail`
    string during/after :meth:`ImapClient.connect_and_login`, where a
    real password is in scope (see module docstring's "the password is
    NEVER..." requirement)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


@dataclass(frozen=True)
class ImapSession:
    """Opaque handle around one live `imaplib.IMAP4_SSL` connection.
    Never inspected/parsed by any caller outside this module — see
    `services/mailbox/imap/imap_adapter.py`, which only ever passes this
    straight back into another `ImapClient` method or `logout`."""

    connection: "imaplib.IMAP4_SSL"


@dataclass(frozen=True)
class ImapConnectResult:
    status: ImapOutcomeStatus
    session: Optional[ImapSession] = None
    error_detail: Optional[str] = None
    #: Diagnostic-only TLS facts a normal handshake exposes (no private
    #: key material) — populated on a successful connect, used by
    #: `scripts/imap_capability_probe.py`.
    tls_version: Optional[str] = None
    tls_cipher: Optional[str] = None
    peer_cert_subject: Optional[str] = None
    peer_cert_issuer: Optional[str] = None


@dataclass(frozen=True)
class ImapCapabilityResult:
    status: ImapOutcomeStatus
    capabilities: Sequence[str] = field(default_factory=tuple)
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapFolderInfo:
    """One `LIST` response entry. `flags` are the RAW IMAP flags the
    server returned (e.g. `\\Noselect`, `\\HasChildren`, and — only when
    the server advertises the `SPECIAL-USE` extension (RFC 6154) — a
    real `\\Junk`/`\\Trash`/`\\Sent`/`\\Drafts`/`\\Archive`/`\\All` flag).
    Never interpreted here — see `services/mailbox/imap/imap_adapter.py`
    for the folder-classification logic this feeds."""

    name: str
    flags: Sequence[str] = field(default_factory=tuple)
    delimiter: Optional[str] = None


@dataclass(frozen=True)
class ImapFolderListResult:
    status: ImapOutcomeStatus
    folders: Sequence[ImapFolderInfo] = field(default_factory=tuple)
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapExamineResult:
    """The result of `EXAMINE`ing one folder — critically carries
    `uidvalidity` (see module docstring/`imap_adapter.py`'s own
    "message identity" doctrine: this is the epoch marker every UID
    must be interpreted relative to)."""

    status: ImapOutcomeStatus
    uidvalidity: Optional[int] = None
    uidnext: Optional[int] = None
    exists: Optional[int] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapSearchResult:
    status: ImapOutcomeStatus
    #: Ascending, de-duplicated UIDs — NEVER sequence numbers (this
    #: always comes from `UID SEARCH`, never plain `SEARCH`).
    uids: Sequence[int] = field(default_factory=tuple)
    #: The folder's own UIDVALIDITY at the moment of this search — see
    #: `ImapExamineResult.uidvalidity`'s own docstring; carried here too
    #: so a caller never needs a second round trip just to learn it.
    uidvalidity: Optional[int] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapMessageHeaders:
    uid: int
    #: `[{"name": ..., "value": ...}, ...]` — the SAME shape
    #: `GraphMessageSummary.raw_headers`/`GraphMessageHeadersResult
    #: .raw_headers` carry, order preserved as the raw header block's
    #: own field order.
    raw_headers: Sequence[Mapping[str, Optional[str]]] = field(default_factory=tuple)


@dataclass(frozen=True)
class ImapFetchHeadersResult:
    status: ImapOutcomeStatus
    messages: Sequence[ImapMessageHeaders] = field(default_factory=tuple)
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapFetchContentResult:
    status: ImapOutcomeStatus
    #: The raw `message/rfc822` bytes, present only when `status == OK`.
    content: Optional[bytes] = None
    error_detail: Optional[str] = None


class ImapClientProtocol(Protocol):
    """The exact structural shape `services/mailbox/imap/imap_adapter.py`
    depends on — satisfied by both the real `ImapClient` below and
    `services.mailbox.imap.fake_imap_client.FakeImapClient`."""

    def connect_and_login(
        self, *, host: str, port: int, username: str, password: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> ImapConnectResult: ...

    def capability(self, session: ImapSession) -> ImapCapabilityResult: ...

    def list_folders(self, session: ImapSession) -> ImapFolderListResult: ...

    def uid_search(self, session: ImapSession, *, folder: str, criteria: str) -> ImapSearchResult: ...

    def uid_fetch_headers(
        self, session: ImapSession, *, folder: str, uids: Sequence[int]
    ) -> ImapFetchHeadersResult: ...

    def uid_fetch_content(self, session: ImapSession, *, folder: str, uid: int) -> ImapFetchContentResult: ...

    def logout(self, session: ImapSession) -> None: ...


_LIST_LINE_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')
_UID_FETCH_META_RE = re.compile(rb"UID (\d+)")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _parse_list_line(raw: bytes) -> Optional[ImapFolderInfo]:
    match = _LIST_LINE_RE.match(raw.strip())
    if not match:
        return None
    flags = tuple(f for f in _decode(match.group("flags")).split() if f)
    delim_raw = match.group("delim")
    delimiter = _decode(delim_raw) if delim_raw is not None else None
    name = _decode(match.group("name")).strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    return ImapFolderInfo(name=name, flags=flags, delimiter=delimiter)


def _parse_uid_fetch_response(data: Sequence[object]) -> list[tuple[int, bytes]]:
    """Bounded, non-exhaustive (but correct for the standard `imaplib`
    response shape) parse of a `UID FETCH` response into
    `[(uid, literal_bytes), ...]` — see
    https://docs.python.org/3/library/imaplib.html for the documented
    `(response_line, literal_bytes)` tuple shape `imaplib` returns per
    fetched item; a non-tuple entry (e.g. the trailing `b')'` imaplib
    also includes) is skipped."""
    results: list[tuple[int, bytes]] = []
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        meta, literal = item
        if not isinstance(meta, (bytes, bytearray)):
            continue
        match = _UID_FETCH_META_RE.search(bytes(meta))
        if not match:
            continue
        uid = int(match.group(1))
        results.append((uid, bytes(literal) if literal is not None else b""))
    return results


def _headers_from_bytes(raw_header_block: bytes) -> tuple[dict, ...]:
    parsed = email.message_from_bytes(raw_header_block)
    return tuple({"name": name, "value": value} for name, value in parsed.items())


def _classify_transport_exception(exc: Exception) -> ImapOutcomeStatus:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ImapOutcomeStatus.TIMEOUT
    if isinstance(exc, (ssl.SSLCertVerificationError, ssl.SSLError)):
        return ImapOutcomeStatus.TLS_VALIDATION_FAILED
    return ImapOutcomeStatus.TRANSPORT_ERROR


class ImapClient:
    """The real adapter for `mail.noust.ai`'s plain IMAP endpoint. Every
    method opens no persistent state beyond the `ImapSession` handed
    back by :meth:`connect_and_login` — callers are responsible for
    calling :meth:`logout` on every exit path (see
    `services/mailbox/imap/imap_adapter.py`)."""

    def connect_and_login(
        self, *, host: str, port: int = DEFAULT_IMAP_PORT, username: str, password: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> ImapConnectResult:
        # `ssl.create_default_context()` enables full certificate-chain
        # validation AND hostname verification by default — see module
        # docstring's "Hard security requirements" section. NEVER weaken
        # this context anywhere in this method.
        context = ssl.create_default_context()

        try:
            connection = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout_seconds)
        except (ssl.SSLCertVerificationError, ssl.SSLError) as exc:
            return ImapConnectResult(
                status=ImapOutcomeStatus.TLS_VALIDATION_FAILED,
                error_detail=_redact(f"TLS validation failed connecting to {host}:{port}: {exc}", password),
            )
        except (socket.timeout, TimeoutError) as exc:
            return ImapConnectResult(
                status=ImapOutcomeStatus.TIMEOUT,
                error_detail=_redact(f"connection to {host}:{port} timed out: {exc}", password),
            )
        except OSError as exc:
            return ImapConnectResult(
                status=ImapOutcomeStatus.TRANSPORT_ERROR,
                error_detail=_redact(f"could not connect to {host}:{port}: {exc}", password),
            )

        tls_version: Optional[str] = None
        tls_cipher: Optional[str] = None
        peer_cert_subject: Optional[str] = None
        peer_cert_issuer: Optional[str] = None
        try:
            sock = connection.sock  # the underlying ssl.SSLSocket
            tls_version = sock.version()
            cipher = sock.cipher()
            tls_cipher = cipher[0] if cipher else None
            cert = sock.getpeercert()
            if cert:
                peer_cert_subject = _format_cert_name(cert.get("subject"))
                peer_cert_issuer = _format_cert_name(cert.get("issuer"))
        except Exception:  # noqa: BLE001 - diagnostic-only, never fatal
            pass

        try:
            connection.login(username, password)
        except imaplib.IMAP4.error as exc:
            try:
                connection.logout()
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                pass
            # imaplib's own login-failure exception text is the SERVER's
            # response (never the client-sent password) — redacted
            # anyway as defense-in-depth (see module docstring).
            return ImapConnectResult(
                status=ImapOutcomeStatus.AUTH_ERROR,
                error_detail=_redact(f"IMAP login rejected for {username!r}: {exc}", password),
            )
        except (socket.timeout, TimeoutError) as exc:
            try:
                connection.logout()
            except Exception:  # noqa: BLE001
                pass
            return ImapConnectResult(
                status=ImapOutcomeStatus.TIMEOUT, error_detail=_redact(f"IMAP login timed out: {exc}", password)
            )
        except (imaplib.IMAP4.abort, OSError) as exc:
            return ImapConnectResult(
                status=_classify_transport_exception(exc),
                error_detail=_redact(f"IMAP login transport failure: {exc}", password),
            )

        return ImapConnectResult(
            status=ImapOutcomeStatus.OK,
            session=ImapSession(connection=connection),
            tls_version=tls_version,
            tls_cipher=tls_cipher,
            peer_cert_subject=peer_cert_subject,
            peer_cert_issuer=peer_cert_issuer,
        )

    def capability(self, session: ImapSession) -> ImapCapabilityResult:
        try:
            typ, data = session.connection.capability()
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
            return ImapCapabilityResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=str(exc))
        except (socket.timeout, TimeoutError) as exc:
            return ImapCapabilityResult(status=ImapOutcomeStatus.TIMEOUT, error_detail=str(exc))
        except OSError as exc:
            return ImapCapabilityResult(status=_classify_transport_exception(exc), error_detail=str(exc))

        if typ != "OK" or not data:
            return ImapCapabilityResult(status=ImapOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"CAPABILITY returned {typ!r}")

        raw = data[0]
        capabilities = tuple(_decode(raw).split()) if isinstance(raw, (bytes, bytearray)) else tuple(str(raw).split())
        return ImapCapabilityResult(status=ImapOutcomeStatus.OK, capabilities=capabilities)

    def list_folders(self, session: ImapSession) -> ImapFolderListResult:
        try:
            typ, data = session.connection.list()
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
            return ImapFolderListResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=str(exc))
        except (socket.timeout, TimeoutError) as exc:
            return ImapFolderListResult(status=ImapOutcomeStatus.TIMEOUT, error_detail=str(exc))
        except OSError as exc:
            return ImapFolderListResult(status=_classify_transport_exception(exc), error_detail=str(exc))

        if typ != "OK":
            return ImapFolderListResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=f"LIST returned {typ!r}")

        folders = []
        for raw in data or ():
            if not isinstance(raw, (bytes, bytearray)):
                continue
            parsed = _parse_list_line(raw)
            if parsed is not None:
                folders.append(parsed)
        return ImapFolderListResult(status=ImapOutcomeStatus.OK, folders=tuple(folders))

    def examine_folder(self, session: ImapSession, *, folder: str) -> ImapExamineResult:
        """A lower-level primitive, deliberately NOT part of
        `ImapClientProtocol` (`uid_search`/`uid_fetch_headers`/
        `uid_fetch_content` below all call this internally and surface
        the UIDVALIDITY they need via their own result types — see
        `ImapSearchResult.uidvalidity`) — kept as its own public method
        because `scripts/imap_capability_probe.py` also calls it
        directly, per-folder, purely for diagnostic EXISTS/UIDNEXT
        output."""
        try:
            typ, data = session.connection.select(folder, readonly=True)
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
            return ImapExamineResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=str(exc))
        except (socket.timeout, TimeoutError) as exc:
            return ImapExamineResult(status=ImapOutcomeStatus.TIMEOUT, error_detail=str(exc))
        except OSError as exc:
            return ImapExamineResult(status=_classify_transport_exception(exc), error_detail=str(exc))

        if typ != "OK":
            detail = _decode(data[0]) if data and isinstance(data[0], (bytes, bytearray)) else str(data)
            return ImapExamineResult(status=ImapOutcomeStatus.NOT_FOUND, error_detail=f"could not EXAMINE {folder!r}: {detail}")

        exists = None
        try:
            if data and isinstance(data[0], (bytes, bytearray)):
                exists = int(data[0])
        except (ValueError, TypeError):
            exists = None

        uidvalidity = _response_int(session.connection, "UIDVALIDITY")
        uidnext = _response_int(session.connection, "UIDNEXT")
        return ImapExamineResult(status=ImapOutcomeStatus.OK, uidvalidity=uidvalidity, uidnext=uidnext, exists=exists)

    def uid_search(self, session: ImapSession, *, folder: str, criteria: str) -> ImapSearchResult:
        examined = self.examine_folder(session, folder=folder)
        if examined.status != ImapOutcomeStatus.OK:
            return ImapSearchResult(status=examined.status, error_detail=examined.error_detail)

        try:
            # `UID SEARCH`, never plain `SEARCH` — see module docstring.
            typ, data = session.connection.uid("SEARCH", None, criteria)
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
            return ImapSearchResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=str(exc))
        except (socket.timeout, TimeoutError) as exc:
            return ImapSearchResult(status=ImapOutcomeStatus.TIMEOUT, error_detail=str(exc))
        except OSError as exc:
            return ImapSearchResult(status=_classify_transport_exception(exc), error_detail=str(exc))

        if typ != "OK" or not data or not isinstance(data[0], (bytes, bytearray)):
            return ImapSearchResult(status=ImapOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"UID SEARCH returned {typ!r}")

        raw = _decode(data[0]).strip()
        if not raw:
            return ImapSearchResult(status=ImapOutcomeStatus.OK, uids=(), uidvalidity=examined.uidvalidity)
        try:
            uids = tuple(sorted({int(token) for token in raw.split()}))
        except ValueError:
            return ImapSearchResult(status=ImapOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"UID SEARCH returned non-numeric UIDs: {raw!r}")
        return ImapSearchResult(status=ImapOutcomeStatus.OK, uids=uids, uidvalidity=examined.uidvalidity)

    def uid_fetch_headers(self, session: ImapSession, *, folder: str, uids: Sequence[int]) -> ImapFetchHeadersResult:
        if not uids:
            return ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=())

        examined = self.examine_folder(session, folder=folder)
        if examined.status != ImapOutcomeStatus.OK:
            return ImapFetchHeadersResult(status=examined.status, error_detail=examined.error_detail)

        uid_set = ",".join(str(u) for u in uids)
        try:
            # `BODY.PEEK[HEADER]` — NEVER plain `BODY[HEADER]` (see
            # module docstring's `\Seen`-side-effect requirement).
            typ, data = session.connection.uid("FETCH", uid_set, "(UID BODY.PEEK[HEADER])")
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
            return ImapFetchHeadersResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=str(exc))
        except (socket.timeout, TimeoutError) as exc:
            return ImapFetchHeadersResult(status=ImapOutcomeStatus.TIMEOUT, error_detail=str(exc))
        except OSError as exc:
            return ImapFetchHeadersResult(status=_classify_transport_exception(exc), error_detail=str(exc))

        if typ != "OK":
            return ImapFetchHeadersResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=f"UID FETCH returned {typ!r}")

        messages = [
            ImapMessageHeaders(uid=uid, raw_headers=_headers_from_bytes(raw))
            for uid, raw in _parse_uid_fetch_response(data or ())
        ]
        return ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=tuple(messages))

    def uid_fetch_content(self, session: ImapSession, *, folder: str, uid: int) -> ImapFetchContentResult:
        examined = self.examine_folder(session, folder=folder)
        if examined.status != ImapOutcomeStatus.OK:
            return ImapFetchContentResult(status=examined.status, error_detail=examined.error_detail)

        try:
            # `BODY.PEEK[]` — the full message, never plain `BODY[]`.
            typ, data = session.connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
            return ImapFetchContentResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=str(exc))
        except (socket.timeout, TimeoutError) as exc:
            return ImapFetchContentResult(status=ImapOutcomeStatus.TIMEOUT, error_detail=str(exc))
        except OSError as exc:
            return ImapFetchContentResult(status=_classify_transport_exception(exc), error_detail=str(exc))

        if typ != "OK":
            return ImapFetchContentResult(status=ImapOutcomeStatus.PROVIDER_ERROR, error_detail=f"UID FETCH returned {typ!r}")

        parsed = _parse_uid_fetch_response(data or ())
        if not parsed:
            # The UID genuinely vanished between SEARCH and this FETCH —
            # an honest, real outcome (mirrors Graph's own 404 case).
            return ImapFetchContentResult(status=ImapOutcomeStatus.NOT_FOUND, error_detail=f"UID {uid} not found on FETCH")
        _, content = parsed[0]
        return ImapFetchContentResult(status=ImapOutcomeStatus.OK, content=content)

    def logout(self, session: ImapSession) -> None:
        """Never raises — logout on an already-broken connection is a
        best-effort cleanup, not a new failure mode (mirrors
        `services.mailbox.microsoft.secrets.delete_mailbox_tokens`'s own
        idempotent-safe discipline)."""
        try:
            session.connection.logout()
        except Exception:  # noqa: BLE001
            pass


def _response_int(connection: "imaplib.IMAP4_SSL", code: str) -> Optional[int]:
    try:
        typ, data = connection.response(code)
    except Exception:  # noqa: BLE001 - a missing response code is normal, never fatal
        return None
    if typ != code or not data or data[0] is None:
        return None
    try:
        return int(data[0])
    except (ValueError, TypeError):
        return None


def _format_cert_name(name_tuple) -> Optional[str]:
    """Render an `ssl`-parsed certificate subject/issuer (a tuple of
    tuples of `(key, value)` pairs) as a short, human-readable
    `CN=...` style string — diagnostic-only, no private key material
    (used by `scripts/imap_capability_probe.py`)."""
    if not name_tuple:
        return None
    parts = []
    for rdn in name_tuple:
        for key, value in rdn:
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else None
