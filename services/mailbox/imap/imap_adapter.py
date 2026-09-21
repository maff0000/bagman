"""``ImapMailboxAdapter`` — the IMAP equivalent of
``services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter``: the
ONE place connection lifecycle (login/logout per call — there is no
OAuth token to refresh for a static username/password pair) and
IMAP-specific request shaping live (CD-6 GUI-operations-foundation
follow-on WO — second mailbox provider).

Provider adapter vs. provider-neutral orchestration — the seam
--------------------------------------------------------------------------
``services/mailbox/sweep.py`` (the provider-neutral sweep engine) knows
nothing about IMAP UIDs/UIDVALIDITY/folder flags — it only calls this
adapter's narrow surface (:meth:`fetch_folder_delta`/
:meth:`fetch_message_content`/:meth:`fetch_message_headers`/
:meth:`report_connection_error`/:meth:`discover_monitored_folders` —
the EXACT SAME method names/signatures
``services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter``
exposes, since both satisfy the SAME informal, duck-typed
``_AdapterProtocol`` `services/mailbox/sweep.py` depends on — see that
module's own ``_AdapterProtocol`` definition) and reacts to their
outcome, with ZERO changes to that file's own logic. This adapter is
the seam that keeps ``services/mailbox/sweep.py`` genuinely
provider-neutral for IMAP too.

Status-vocabulary compatibility — read before touching anything here
------------------------------------------------------------------------
``services/mailbox/sweep.py`` compares a result's own ``.status``
against ``services.mailbox.microsoft.graph_client.GraphOutcomeStatus``
members (e.g. ``page.status == GraphOutcomeStatus.RATE_LIMITED``). This
adapter's own results carry
``services.mailbox.imap.imap_client.ImapOutcomeStatus`` instead — a
SEPARATE ``(str, enum.Enum)`` type with the SAME member names/string
values for every status the two providers share (see
``imap_client.py``'s own module docstring, "Outcome-status pattern").
Because both are string-mixin enums, ``ImapOutcomeStatus.AUTH_ERROR ==
GraphOutcomeStatus.AUTH_ERROR`` is `True` via plain string equality —
this is what lets every existing status comparison in
``services/mailbox/sweep.py`` keep working, completely UNCHANGED, for an
IMAP-sourced result. This is the "provider-neutral abstraction point"
the WO asked this delivery to look for in ``sweep.py`` — it already
existed, implicitly, in how that module compares statuses; no change to
that file's own status-comparison logic was needed to make it work for
a second provider.

Message identity — ``(folder, UIDVALIDITY, UID)``, never a bare UID
------------------------------------------------------------------------
:func:`compose_immutable_message_id` is the ONE place this adapter
composes ``MailboxMessage.immutable_provider_message_id`` — always the
FULL ``(folder, uidvalidity, uid)`` tuple. This value is passed straight
through, UNCHANGED, as the object-store key component
(``persistence.objects.store.object_key``/``_validate_key_component``
— see ``services/mailbox/microsoft/evidence_ingest.py``'s own
``object_store.put(immutable_provider_message_id, ...)`` call, shared
by BOTH providers), which is PID §19-governed and accepts ONLY
``[A-Za-z0-9._-]`` — critically, NOT an arbitrary byte like a raw folder
name might contain (spaces, ``/``, unicode are all common in real IMAP
folder names, e.g. ``"Deleted Items"``, ``"INBOX/Clients"``). The folder
name component is therefore hex-encoded (``folder.encode("utf-8").hex()``
— every output character is one of ``0-9a-f``, always inside the safe
set) before being joined with ``.`` (also in the safe set) —
:func:`decompose_immutable_message_id` is the exact inverse. NEVER the
bare UID alone. This is the durable identity discipline the architect
required (module docstring of the WO this delivers):

* UID alone is NOT globally unique — the identical UID number can be
  reused for a completely different message after a real ``UIDVALIDITY``
  change (RFC 3501 §2.3.1.1 — a UID is only guaranteed unique WITHIN one
  UIDVALIDITY epoch of one folder).
* Folder alone is obviously not a message identity.
* So the full triple is the durable key — this mirrors exactly how
  Microsoft's own ``immutable_id`` (a single opaque Graph-assigned
  string) already IS a durable, provider-minted identity; IMAP has no
  single opaque id of that kind, so this adapter constructs the
  provider-neutral equivalent itself, explicitly, rather than pretending
  a bare UID is durable.

**If a folder's ``UIDVALIDITY`` ever changes between two sweeps**, every
UID previously observed under the OLD ``UIDVALIDITY`` is, by
construction, now a DIFFERENT ``immutable_provider_message_id`` string
from the (numerically identical) UID under the NEW ``UIDVALIDITY`` — see
:func:`_search_criteria_for_round` below for how a detected epoch change
is treated as a fresh search from the whole folder, never a silent
continuation of the old cursor. `services.mailbox.message
.MailboxMessageRepository.record_observation`'s own (mailbox_id,
immutable_provider_message_id) idempotency then does the rest
automatically: a message re-discovered under a NEW epoch is, correctly,
treated as a brand-new, never-before-seen message — never silently
collapsed with whatever the old epoch's numerically-identical UID used
to mean.

``internet_message_id`` — the secondary durable hint (mirrors Microsoft)
------------------------------------------------------------------------
This adapter also populates ``MailboxMessage.internet_message_id`` from
the message's own ``Message-ID`` header when present — the SAME field
Microsoft's adapter already populates from Graph's own
``internetMessageId``, reused here rather than inventing a new field
(architect spec, verbatim, in the WO's own item 10).

Folder identity — a name, not an opaque id (a real provider difference,
documented rather than papered over)
------------------------------------------------------------------------
Unlike Microsoft Graph (which mints a real, immutable, opaque
``folder_id`` distinct from a folder's own display name — see
``services.mailbox.microsoft.graph_client``'s own "well-known folder
identity" doctrine), plain IMAP has no separate opaque folder id at all
— a folder's OWN NAME (e.g. ``"INBOX"``, ``"Junk"``) is simultaneously
its identity and its display label (renaming an IMAP folder IS changing
its identity, protocol-wide — there is no stable id that survives a
rename). This adapter's own ``MonitoredFolder``-shaped entries therefore
use the SAME string for both ``folder_id`` and ``display_name`` — a
documented, real provider difference, never a bug.

Folder discovery / monitored-set doctrine (WO item 4 / architect's own
instruction)
------------------------------------------------------------------------
:meth:`discover_monitored_folders` always includes ``INBOX``. For every
other folder returned by ``LIST``, it is monitored if-and-only-if it is
selectable (no ``\\Noselect`` flag) AND is classified as a junk/trash
equivalent:

* When the server advertises the ``SPECIAL-USE`` extension (RFC 6154 —
  detected via a live ``CAPABILITY`` fetch, see
  :meth:`_folder_list_with_capabilities`), a folder carrying a real
  ``\\Junk`` or ``\\Trash`` flag is monitored; ``\\Sent``/``\\Drafts``/
  ``\\All``/``\\Archive``/``\\Flagged`` and any folder with none of these
  flags are excluded (outbound-shaped or not junk/trash-equivalent).
* When ``SPECIAL-USE`` is NOT advertised, :data:`_FALLBACK_JUNK_TRASH_NAMES`
  is used instead — a small, conservative, case-insensitive exact-name
  match (``Junk``/``Spam``/``Trash``/``Deleted Items``/``Deleted
  Messages``) — never a substring/fuzzy match (a folder merely
  CONTAINING one of these words, e.g. a user-created "Trash Talk"
  folder, is NOT matched).

A non-``OK`` outcome at connect/capability/list-folders time is a
WHOLE-DISCOVERY-CALL failure (mirrors
``MicrosoftGraphMailboxAdapter.discover_monitored_folders``'s own
identical "without a real folder list, nothing can be safely attempted"
doctrine) — ``services/mailbox/sweep.py`` already treats any non-``OK``
``discover_monitored_folders`` outcome as a whole-sweep precondition
failure with ZERO changes needed here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Mapping, Optional, Sequence

from services.mailbox.imap.imap_client import (
    DEFAULT_IMAP_PORT,
    ImapClientProtocol,
    ImapOutcomeStatus,
    ImapSession,
)
from services.mailbox.imap.secrets import NoustAIImapCredentials, read_noustai_imap_credentials
from services.mailbox.mailbox import MailboxSourceRepository

#: Overridable purely for tests/a disposable dev instance — mirrors
#: `app/api/routers/mailboxes_microsoft.py::_microsoft_redirect_uri`'s
#: own env-var-override-for-tests discipline exactly. Production never
#: sets the override; the real value is `mail.noust.ai`.
_HOST_ENV_VAR = "BAGMAN_NOUSTAI_IMAP_HOST"
_PORT_ENV_VAR = "BAGMAN_NOUSTAI_IMAP_PORT"
_DEFAULT_HOST = "mail.noust.ai"

#: `INBOX` is a case-INSENSITIVE reserved name per RFC 3501 §5.1, but
#: this adapter always renders/compares the canonical uppercase spelling
#: for identity purposes (mirrors every real IMAP client's own
#: convention).
_INBOX = "INBOX"

#: RFC 6154 SPECIAL-USE flags this adapter recognises for monitoring —
#: see module docstring's "Folder discovery" section. `\Sent`/`\Drafts`/
#: `\All`/`\Archive`/`\Flagged` are recognised ONLY to be explicitly
#: excluded (never monitored) when SPECIAL-USE is advertised.
_SPECIAL_USE_MONITORED = frozenset({"\\junk", "\\trash"})
_SPECIAL_USE_EXCLUDED = frozenset({"\\sent", "\\drafts", "\\all", "\\archive", "\\flagged"})

#: Conservative fallback (SPECIAL-USE not advertised) — exact,
#: case-insensitive name match only, never substring/fuzzy (see module
#: docstring).
_FALLBACK_JUNK_TRASH_NAMES = frozenset({"junk", "spam", "trash", "deleted items", "deleted messages"})

_SPECIAL_USE_CAPABILITY = "SPECIAL-USE"

#: `.` — safely inside the object-storage key charset `[A-Za-z0-9._-]`
#: (see module docstring's "Message identity" section) — unlike an
#: arbitrary/control-character separator, which the storage layer's own
#: `persistence.objects.store._validate_key_component` would reject
#: outright.
_ID_SEPARATOR = "."
_ID_PREFIX = "IMAP"

#: Bounded page size for one `UID FETCH` headers round — see module
#: docstring's own "Bounded, observable, resumable" doctrine (mirrors
#: Microsoft's own delta-page pagination, applied here to a single UID
#: SEARCH result set instead of a server-paginated delta feed).
_PAGE_SIZE = 50


def _imap_host() -> str:
    return os.environ.get(_HOST_ENV_VAR, _DEFAULT_HOST)


def _imap_port() -> int:
    raw = os.environ.get(_PORT_ENV_VAR)
    if not raw:
        return DEFAULT_IMAP_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_IMAP_PORT


def compose_immutable_message_id(*, folder: str, uidvalidity: int, uid: int) -> str:
    """The ONE place this adapter composes the full, durable
    `(folder, uidvalidity, uid)` identity tuple — see module docstring's
    "Message identity" section. `folder` is hex-encoded (never used
    verbatim — a real folder name commonly contains a space, `/`, or
    non-ASCII character, none of which are safe object-storage key
    characters) so the composed string is ALWAYS a valid
    `persistence.objects.store` key component: every character is one of
    `A-Za-z0-9._-` by construction (`IMAP` + hex digits + decimal digits
    + `.` separators)."""
    folder_hex = folder.encode("utf-8").hex()
    return _ID_SEPARATOR.join((_ID_PREFIX, folder_hex, str(uidvalidity), str(uid)))


@dataclass(frozen=True)
class DecomposedImapMessageId:
    folder: str
    uidvalidity: int
    uid: int


def decompose_immutable_message_id(immutable_provider_message_id: str) -> Optional[DecomposedImapMessageId]:
    """The inverse of :func:`compose_immutable_message_id` — returns
    `None` (never raises) for a string that is not one of this
    adapter's own compositions (e.g. a Microsoft-provider id, if ever
    handed the wrong value by a caller's bug)."""
    parts = immutable_provider_message_id.split(_ID_SEPARATOR)
    if len(parts) != 4 or parts[0] != _ID_PREFIX:
        return None
    try:
        folder = bytes.fromhex(parts[1]).decode("utf-8")
        return DecomposedImapMessageId(folder=folder, uidvalidity=int(parts[2]), uid=int(parts[3]))
    except (ValueError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class MonitoredFolder:
    """Mirrors `services.mailbox.microsoft.adapter.MonitoredFolder`'s
    own shape exactly (`folder_id` + `display_name`) — see module
    docstring's "Folder identity" section for why both fields carry the
    SAME string for IMAP."""

    folder_id: str
    display_name: str


@dataclass(frozen=True)
class FolderDiscoveryResult:
    status: ImapOutcomeStatus
    folders: Sequence[MonitoredFolder] = field(default_factory=tuple)
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapMessageSummary:
    """Mirrors `services.mailbox.microsoft.graph_client.GraphMessageSummary`'s
    own field shape exactly — every field `services/mailbox/sweep.py`
    reads from a delta-page message, so that file needs zero changes to
    consume this instead."""

    immutable_id: str
    internet_message_id: Optional[str]
    subject: Optional[str]
    sender_address: Optional[str]
    sender_display_name: Optional[str]
    received_at: Optional[datetime]
    has_attachments: bool
    removed: bool = False
    attachment_metadata: Sequence[Mapping[str, Optional[object]]] = field(default_factory=tuple)
    auth_signals: Mapping[str, Optional[str]] = field(default_factory=dict)
    raw_headers: Sequence[Mapping[str, Optional[str]]] = field(default_factory=tuple)


@dataclass(frozen=True)
class ImapDeltaPageResult:
    """Mirrors `GraphDeltaPageResult`'s own field shape exactly."""

    status: ImapOutcomeStatus
    messages: Sequence[ImapMessageSummary] = field(default_factory=tuple)
    next_link: Optional[str] = None
    delta_link: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapMessageContentResult:
    status: ImapOutcomeStatus
    content: Optional[bytes] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapMessageHeadersResult:
    status: ImapOutcomeStatus
    raw_headers: Sequence[Mapping[str, Optional[str]]] = field(default_factory=tuple)
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class ImapLoginOutcome:
    """The result of a bare connect+login attempt — used by
    `app/api/routers/mailboxes_imap.py`'s own "connect" endpoint (see
    module docstring's own note on `connect` meaning "attempt a real
    login now", not an OAuth redirect)."""

    status: ImapOutcomeStatus
    error_detail: Optional[str] = None


def _decode_mime_words(value: Optional[str]) -> Optional[str]:
    """RFC 2047-decode a header value (e.g. an encoded `Subject`) —
    returns the ORIGINAL string unchanged if it is not (or fails to
    parse as) encoded-word syntax; never raises."""
    if not value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a malformed encoded-word must never crash discovery
        return value


def _header_lookup(raw_headers: Sequence[Mapping[str, Optional[str]]], name: str) -> Optional[str]:
    lowered = name.lower()
    for header in raw_headers:
        if str(header.get("name") or "").strip().lower() == lowered:
            value = header.get("value")
            return value.strip() if isinstance(value, str) else value
    return None


def _extract_message_summary(
    *, folder: str, uidvalidity: int, uid: int, raw_headers: Sequence[Mapping[str, Optional[str]]]
) -> ImapMessageSummary:
    subject = _decode_mime_words(_header_lookup(raw_headers, "Subject"))
    message_id_header = _header_lookup(raw_headers, "Message-ID")
    from_header = _header_lookup(raw_headers, "From")
    sender_display_name, sender_address = (None, None)
    if from_header:
        display_name, address = parseaddr(from_header)
        sender_display_name = _decode_mime_words(display_name) or None
        sender_address = address.strip().lower() if address else None

    received_at: Optional[datetime] = None
    date_header = _header_lookup(raw_headers, "Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed is not None:
                received_at = (
                    parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                )
        except (TypeError, ValueError):
            received_at = None

    # Bounded, best-effort proxy for "this message probably has an
    # attachment" from the TOP-LEVEL Content-Type header alone (this
    # adapter's own Stage-A fetch is headers-only — see module docstring
    # — never a `BODYSTRUCTURE` fetch, so no real per-attachment
    # filename/content-type/size list is available, unlike Microsoft's
    # own `$expand=attachments(...)`; `attachment_metadata` therefore
    # stays empty for every IMAP message — a documented, scoped
    # simplification, never a fabricated attachment list).
    content_type = (_header_lookup(raw_headers, "Content-Type") or "").lower()
    has_attachments = "multipart/mixed" in content_type

    return ImapMessageSummary(
        immutable_id=compose_immutable_message_id(folder=folder, uidvalidity=uidvalidity, uid=uid),
        internet_message_id=message_id_header,
        subject=subject,
        sender_address=sender_address,
        sender_display_name=sender_display_name,
        received_at=received_at,
        has_attachments=has_attachments,
        attachment_metadata=(),
        auth_signals={},
        raw_headers=tuple(raw_headers),
    )


def _encode_delta_link(*, uidvalidity: int, last_uid: int) -> str:
    return json.dumps({"uidvalidity": uidvalidity, "last_uid": last_uid}, separators=(",", ":"))


def _decode_delta_link(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    try:
        payload = json.loads(value)
        return int(payload["uidvalidity"]), int(payload["last_uid"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _encode_next_link(*, uidvalidity: int, remaining_uids: Sequence[int], resolved_last_uid: int) -> str:
    return json.dumps(
        {"uidvalidity": uidvalidity, "remaining_uids": list(remaining_uids), "resolved_last_uid": resolved_last_uid},
        separators=(",", ":"),
    )


def _decode_next_link(value: Optional[str]) -> Optional[tuple[int, list[int], int]]:
    if not value:
        return None
    try:
        payload = json.loads(value)
        return int(payload["uidvalidity"]), [int(u) for u in payload["remaining_uids"]], int(payload["resolved_last_uid"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


class ImapMailboxAdapter:
    def __init__(
        self,
        *,
        client: ImapClientProtocol,
        mailbox_repository: MailboxSourceRepository,
        credentials_provider=None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self._client = client
        self._mailbox_repository = mailbox_repository
        self._credentials_provider = credentials_provider or read_noustai_imap_credentials
        self._host = host
        self._port = port

    def is_configured(self) -> bool:
        return self._credentials_provider() is not None

    def _resolved_host(self) -> str:
        return self._host if self._host is not None else _imap_host()

    def _resolved_port(self) -> int:
        return self._port if self._port is not None else _imap_port()

    def _connect(self) -> tuple[Optional[ImapSession], Optional[ImapOutcomeStatus], Optional[str]]:
        """Returns `(session, None, None)` on success, or
        `(None, status, error_detail)` on any failure — including the
        honest `CONFIG_ERROR` case (no credentials on disk yet)."""
        credentials: Optional[NoustAIImapCredentials] = self._credentials_provider()
        if credentials is None:
            return None, ImapOutcomeStatus.CONFIG_ERROR, (
                "matt@noust.ai IMAP is not configured yet — no username/password found at "
                "/opt/bagman/secrets/mail/noustai/ (the operator has not placed credentials there yet). "
                "This is expected until the PL provisions real credentials."
            )
        result = self._client.connect_and_login(
            host=self._resolved_host(),
            port=self._resolved_port(),
            username=credentials.username,
            password=credentials.password,
        )
        if result.status != ImapOutcomeStatus.OK or result.session is None:
            return None, result.status, result.error_detail
        return result.session, None, None

    def report_connection_error(self, mailbox_id: str, *, error_code: str, error_detail: str) -> None:
        """See `services.mailbox.microsoft.adapter
        .MicrosoftGraphMailboxAdapter.report_connection_error`'s own
        docstring — deliberately reused here (the underlying
        `MailboxSourceRepository` method transitions the SAME
        provider-neutral `connection_state` field regardless of which
        provider observed the fault; its "microsoft"-prefixed name is a
        documented, unchanged naming artefact of that repository's own
        Slice-4 origin, not Microsoft-specific behaviour — see
        `services/mailbox/mailbox.py`'s own `ALLOWED_CONNECTION_TRANSITIONS`,
        which has no provider-specific logic in it at all)."""
        self._mailbox_repository.mark_microsoft_connection_error(mailbox_id, error_code=error_code, error_detail=error_detail)

    def attempt_login(self, mailbox_id: str) -> ImapLoginOutcome:
        """A bare connect+login probe — no folder operations. Used by
        `app/api/routers/mailboxes_imap.py`'s own "connect" endpoint
        (module docstring: "connect" means "attempt a real login now").
        Always logs out immediately afterwards; never leaves a
        connection open."""
        session, status, detail = self._connect()
        if session is None:
            return ImapLoginOutcome(status=status, error_detail=detail)
        self._client.logout(session)
        return ImapLoginOutcome(status=ImapOutcomeStatus.OK)

    # -- folder discovery ---------------------------------------------

    def discover_monitored_folders(self, *, mailbox_id: str) -> FolderDiscoveryResult:
        session, status, detail = self._connect()
        if session is None:
            return FolderDiscoveryResult(status=status, error_detail=detail)

        try:
            capability_result = self._client.capability(session)
            if capability_result.status != ImapOutcomeStatus.OK:
                return FolderDiscoveryResult(status=capability_result.status, error_detail=capability_result.error_detail)
            special_use_advertised = any(
                cap.strip().upper() == _SPECIAL_USE_CAPABILITY for cap in capability_result.capabilities
            )

            folder_list_result = self._client.list_folders(session)
            if folder_list_result.status != ImapOutcomeStatus.OK:
                return FolderDiscoveryResult(status=folder_list_result.status, error_detail=folder_list_result.error_detail)
        finally:
            self._client.logout(session)

        monitored: list[MonitoredFolder] = []
        seen_names: set[str] = set()
        for info in folder_list_result.folders:
            name = info.name
            lowered_flags = {f.lower() for f in info.flags}
            if "\\noselect" in lowered_flags:
                continue
            is_inbox = name.strip().upper() == _INBOX
            if is_inbox:
                canonical = _INBOX
            elif special_use_advertised:
                if lowered_flags & _SPECIAL_USE_EXCLUDED:
                    continue
                if not (lowered_flags & _SPECIAL_USE_MONITORED):
                    continue
                canonical = name
            else:
                if name.strip().lower() not in _FALLBACK_JUNK_TRASH_NAMES:
                    continue
                canonical = name

            if canonical in seen_names:
                continue
            seen_names.add(canonical)
            monitored.append(MonitoredFolder(folder_id=canonical, display_name=canonical))

        if _INBOX not in seen_names:
            # A real IMAP server always has an INBOX (RFC 3501 §5.1) —
            # defensive only; never crash discovery if `LIST` somehow
            # omitted it.
            monitored.insert(0, MonitoredFolder(folder_id=_INBOX, display_name=_INBOX))

        return FolderDiscoveryResult(status=ImapOutcomeStatus.OK, folders=tuple(monitored))

    # -- delta / content / headers fetch --------------------------------

    def fetch_folder_delta(
        self,
        *,
        mailbox_id: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ) -> ImapDeltaPageResult:
        session, status, detail = self._connect()
        if session is None:
            return ImapDeltaPageResult(status=status, error_detail=detail)

        try:
            next_state = _decode_next_link(next_link)
            if next_state is not None:
                uidvalidity, remaining, resolved_last_uid = next_state
                return self._fetch_uid_batch(
                    session, folder=folder, uidvalidity=uidvalidity, remaining=remaining,
                    resolved_last_uid=resolved_last_uid,
                )

            search_result = self._search_for_round(
                session, folder=folder, delta_link=delta_link, bootstrap_timestamp=bootstrap_timestamp
            )
            if search_result.status != ImapOutcomeStatus.OK:
                return ImapDeltaPageResult(status=search_result.status, error_detail=search_result.error_detail)

            uidvalidity = search_result.uidvalidity or 0
            uids = list(search_result.uids)
            previous = _decode_delta_link(delta_link)
            fallback_last_uid = previous[1] if previous is not None and previous[0] == uidvalidity else 0
            resolved_last_uid = max(uids) if uids else fallback_last_uid
            return self._fetch_uid_batch(
                session, folder=folder, uidvalidity=uidvalidity, remaining=uids, resolved_last_uid=resolved_last_uid
            )
        finally:
            self._client.logout(session)

    def _search_for_round(
        self, session: ImapSession, *, folder: str, delta_link: Optional[str], bootstrap_timestamp: Optional[datetime]
    ):
        previous = _decode_delta_link(delta_link)
        if previous is not None:
            previous_uidvalidity, previous_last_uid = previous
            # Probe the folder's CURRENT uidvalidity first via a cheap,
            # always-valid search so an epoch change can be detected —
            # `UID SEARCH ALL` also gives us the current uidvalidity for
            # free (see `ImapSearchResult.uidvalidity`).
            probe = self._client.uid_search(session, folder=folder, criteria="ALL")
            if probe.status != ImapOutcomeStatus.OK:
                return probe
            if probe.uidvalidity == previous_uidvalidity:
                # Same epoch — only messages newer than the last-seen
                # UID are a genuinely new observation this round.
                criteria = f"UID {previous_last_uid + 1}:*"
                return self._client.uid_search(session, folder=folder, criteria=criteria)
            # UIDVALIDITY changed — a fresh epoch (see module docstring's
            # "Message identity" section): every UID in this folder is,
            # by construction, a brand-new `immutable_provider_message_id`
            # under the new epoch, so search the WHOLE folder rather than
            # silently continuing the old, now-meaningless `last_uid`
            # boundary.
            return probe

        if bootstrap_timestamp is not None:
            # IMAP `SEARCH SINCE` is DATE-only (no time-of-day component
            # — a real, documented RFC 3501 §6.4.4 limitation, unlike
            # Graph's exact-timestamp `$filter`). This is conservatively
            # OVER-inclusive (may re-observe up to ~1 extra day of
            # messages at the boundary) rather than under-inclusive —
            # idempotency at the message-repository layer makes an
            # over-inclusive bootstrap safe; a missed message would not
            # be.
            since = bootstrap_timestamp.astimezone(timezone.utc).strftime("%d-%b-%Y")
            return self._client.uid_search(session, folder=folder, criteria=f"SINCE {since}")

        return self._client.uid_search(session, folder=folder, criteria="ALL")

    def _fetch_uid_batch(
        self, session: ImapSession, *, folder: str, uidvalidity: int, remaining: Sequence[int], resolved_last_uid: int
    ) -> ImapDeltaPageResult:
        if not remaining:
            return ImapDeltaPageResult(
                status=ImapOutcomeStatus.OK, messages=(), delta_link=_encode_delta_link(uidvalidity=uidvalidity, last_uid=resolved_last_uid)
            )

        batch = list(remaining[:_PAGE_SIZE])
        rest = list(remaining[_PAGE_SIZE:])

        headers_result = self._client.uid_fetch_headers(session, folder=folder, uids=batch)
        if headers_result.status != ImapOutcomeStatus.OK:
            return ImapDeltaPageResult(status=headers_result.status, error_detail=headers_result.error_detail)

        by_uid = {m.uid: m.raw_headers for m in headers_result.messages}
        messages = tuple(
            _extract_message_summary(folder=folder, uidvalidity=uidvalidity, uid=uid, raw_headers=by_uid.get(uid, ()))
            for uid in batch
            if uid in by_uid
        )

        if rest:
            return ImapDeltaPageResult(
                status=ImapOutcomeStatus.OK,
                messages=messages,
                next_link=_encode_next_link(uidvalidity=uidvalidity, remaining_uids=rest, resolved_last_uid=resolved_last_uid),
            )
        return ImapDeltaPageResult(
            status=ImapOutcomeStatus.OK,
            messages=messages,
            delta_link=_encode_delta_link(uidvalidity=uidvalidity, last_uid=resolved_last_uid),
        )

    def fetch_message_content(self, *, mailbox_id: str, immutable_message_id: str) -> ImapMessageContentResult:
        decomposed = decompose_immutable_message_id(immutable_message_id)
        if decomposed is None:
            return ImapMessageContentResult(
                status=ImapOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"'{immutable_message_id}' is not a valid IMAP immutable_provider_message_id",
            )

        session, status, detail = self._connect()
        if session is None:
            return ImapMessageContentResult(status=status, error_detail=detail)
        try:
            result = self._client.uid_fetch_content(session, folder=decomposed.folder, uid=decomposed.uid)
            return ImapMessageContentResult(status=result.status, content=result.content, error_detail=result.error_detail)
        finally:
            self._client.logout(session)

    def fetch_message_headers(self, *, mailbox_id: str, immutable_message_id: str) -> ImapMessageHeadersResult:
        decomposed = decompose_immutable_message_id(immutable_message_id)
        if decomposed is None:
            return ImapMessageHeadersResult(
                status=ImapOutcomeStatus.MALFORMED_RESPONSE,
                error_detail=f"'{immutable_message_id}' is not a valid IMAP immutable_provider_message_id",
            )

        session, status, detail = self._connect()
        if session is None:
            return ImapMessageHeadersResult(status=status, error_detail=detail)
        try:
            result = self._client.uid_fetch_headers(session, folder=decomposed.folder, uids=[decomposed.uid])
            if result.status != ImapOutcomeStatus.OK:
                return ImapMessageHeadersResult(status=result.status, error_detail=result.error_detail)
            if not result.messages:
                return ImapMessageHeadersResult(status=ImapOutcomeStatus.NOT_FOUND, error_detail=f"UID {decomposed.uid} not found")
            return ImapMessageHeadersResult(status=ImapOutcomeStatus.OK, raw_headers=result.messages[0].raw_headers)
        finally:
            self._client.logout(session)
