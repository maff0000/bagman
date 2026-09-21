"""``GmailMailboxAdapter`` — the ONE place token-lifecycle orchestration
(refresh-on-expiry, reactive-refresh-on-401, connection-state side
effects) and Gmail-API-specific request shaping live (CD-6
GUI-operations-foundation follow-on WO — third mailbox provider).

Provider adapter vs. provider-neutral orchestration — the seam
--------------------------------------------------------------------------
``services/mailbox/sweep.py`` (the provider-neutral sweep engine) knows
nothing about OAuth tokens, Gmail label ids, or Gmail's own
`after:`/`nextPageToken` search mechanics — it only calls this adapter's
narrow surface (:meth:`fetch_folder_delta`/:meth:`fetch_message_content`/
:meth:`fetch_message_headers`/:meth:`report_connection_error`/
:meth:`discover_monitored_folders` — the EXACT SAME method names/
signatures ``services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter``/
``services.mailbox.imap.imap_adapter.ImapMailboxAdapter`` both expose,
since all three satisfy the SAME informal, duck-typed `_AdapterProtocol`
`services/mailbox/sweep.py` depends on — see that module's own
`_AdapterProtocol` definition) and reacts to their outcome, with ZERO
changes to that file's own logic beyond the one additive
`evaluate_message_authentication` provider dispatch branch this delivery
adds.

Token refresh discipline — mirrors
``services.mailbox.microsoft.adapter.MicrosoftGraphMailboxAdapter``
--------------------------------------------------------------------------
* Pre-emptive refresh: if the stored access token is within
  :data:`_REFRESH_SKEW_SECONDS` of `expires_at`, refresh BEFORE the
  first request of a sweep.
* Reactive refresh: if a request still comes back `AUTH_ERROR` despite a
  token this adapter believed was fresh, refresh once and retry the SAME
  request once — never more than once (bounded retry).
* Refresh-token preservation (a REAL, documented Gmail-specific
  difference from Microsoft — see `gmail_client.py`'s own module
  docstring "Token refresh" section): Google's own refresh response
  usually carries NO `refresh_token` at all (the existing one stays
  valid). Every write-back after a refresh call in this module therefore
  preserves the PREVIOUSLY stored `refresh_token` whenever the refresh
  response's own `refresh_token` is `None` — never overwrites a still-
  valid stored refresh token with an empty/missing value. See
  :meth:`_persist_refreshed_tokens` below — the ONE place this merge
  happens.
* A refresh that itself fails means Google revoked/expired consent —
  this adapter marks the mailbox `connection_state -> AUTH_REQUIRED` via
  `services.mailbox.mailbox.MailboxSourceRepository
  .mark_microsoft_auth_required` (same distinction Microsoft's own
  adapter draws — see that module's own docstring for why this is
  `AUTH_REQUIRED`, never `ERROR`).
* A 403 (`PERMISSION_ERROR`) or a genuinely unexpected provider fault
  instead marks the mailbox `connection_state -> ERROR` (see
  :meth:`report_connection_error`).

Two independent Gmail mailboxes — cross-account isolation
--------------------------------------------------------------------------
ONE shared app credential pair (`services.mailbox.gmail.secrets
.read_gmail_app_credentials`) governs BOTH Gmail accounts
(`mgs241171@gmail.com` and `matt.george.scott@gmail.com`), but every
OTHER piece of state this adapter touches is `mailbox_id`-scoped:
per-mailbox OAuth tokens (`services.mailbox.gmail.secrets`'s own
`tokens/<mailbox_id>/` directory), the durable per-`(mailbox_id,
provider_kind, folder)` sweep cursor (`services/mailbox/cursor.py` —
already generic, never provider-specific), and this delivery's own
`MailboxDomainRule`/Needs You items (already `mailbox_id`-scoped at the
repository layer, unchanged by this delivery). Two different
`mailbox_id`s therefore get fully independent connection state,
independent cursors, and independent policy rules automatically — never
because this module does anything special, but because it never mixes
`mailbox_id`s across a single call.

Gmail durable message identity — the provider's own `id`, directly
--------------------------------------------------------------------------
Gmail mints its own message `id`, globally unique and immutable within
ONE Gmail account (Gmail's own documented API contract) — unlike IMAP
(no equivalent single opaque id; `services.mailbox.imap.imap_adapter`
has to COMPOSE a `(folder, uidvalidity, uid)` tuple itself). This module
therefore uses Gmail's own message `id` DIRECTLY, unmodified, as
`MailboxMessage.immutable_provider_message_id` — see
:func:`_to_message_summary` below, the ONE place this identity is
produced. `MailboxMessageRepository.record_observation`'s own
`(mailbox_id, immutable_provider_message_id)` uniqueness already gives
this mailbox-scoping for free (see that repository's own docstring) —
this is exactly what guarantees the SAME message content observed in
BOTH Gmail mailboxes (e.g. a message CC'd to both addresses) produces
TWO separate `MailboxMessage`/provenance records, one per mailbox,
never collapsed: the composite key differs because `mailbox_id` differs,
even though `immutable_provider_message_id` (Gmail's own message id, a
value minted independently per-account by Gmail itself — a message
delivered to two different accounts gets two DIFFERENT Gmail message
ids in the first place) would already differ too. Thread id, the raw
`Message-ID` header, label, and pagination token are all separate,
secondary fields — never folded into identity (mirrors both other
providers' identical doctrine); `internet_message_id` is populated from
the `Message-ID` header when present, exactly like Microsoft/IMAP.

Label/folder normalisation — bounded to INBOX/SPAM/TRASH this delivery
--------------------------------------------------------------------------
Gmail uses LABELS, not folders — a message can carry several
simultaneously, and Gmail's own `messages.list` `labelIds` query
parameter is AND-semantics across multiple values ("has ALL of these
labels"), never OR — so this module queries GmailClient.list_messages
ONE label at a time (see `gmail_client.py`'s own module docstring),
exposing each monitored label to `services/mailbox/sweep.py` as its own
`MonitoredFolder`, exactly the same shape Microsoft's own
Inbox/Junk/DeletedItems and IMAP's own INBOX/Junk/Trash folders already
are.

:func:`compute_monitored_labels` is this delivery's own bounded,
DOCUMENTED first-provider-delivery scope decision: only the three
SYSTEM labels `INBOX`/`SPAM`/`TRASH` are monitored (mirrors the
"junk/trash coverage" doctrine already established for Microsoft/IMAP,
applied at the minimum this delivery's own WO names explicitly) —
`SENT`/`DRAFT` are always excluded, and no USER-created label (Gmail's
own arbitrary custom labels, or the built-in `CATEGORY_*`
promotions/social/updates/forums labels) is monitored by this delivery
at all, unlike Microsoft/IMAP's own "every custom/nested folder is
monitored" doctrine. This is a real, deliberate scope narrowing (not an
oversight) — Gmail's own label taxonomy has no equivalent of a plain
IMAP/Graph "user-created folder" that is unambiguously financially
relevant by construction; deciding which user labels (if any) should
also be monitored needs real label samples from a live mailbox this
delivery does not have. Extending monitored-label coverage is a natural,
narrow follow-up once real credentials/samples exist — flagged here
prominently, not silently chosen. Classification is by the label's own
`type == "system"` field and `id` — NEVER by `name` (mirrors Microsoft's
own "never guessed from display name" well-known-folder doctrine
exactly).

Not-processing-the-same-message-twice-per-pass
--------------------------------------------------------------------------
Within ONE `fetch_folder_delta` call (one label's own paginated
`messages.list` round), this module de-duplicates the page's own message
ids before fetching metadata for each (`list(dict.fromkeys(...))` — see
:meth:`fetch_folder_delta` below) — cheap, defensive, and never relies
solely on `MailboxMessageRepository`'s own idempotency to paper over a
redundant N-times-refetch (per this delivery's own explicit instruction).
The SAME message appearing under a DIFFERENT monitored label (a second,
separate `fetch_folder_delta` call, for a different `folder`) is instead
collapsed by `services/mailbox/sweep.py`'s own existing, provider-
neutral `find_by_provider_id`-then-skip-if-already-FINAL check (the SAME
mechanism that already collapses Microsoft's own Inbox/Deleted-Items
overlap, or a message an IMAP mailbox has already fully processed) —
this delivery adds no new cross-folder logic to `sweep.py` at all (and
is not permitted to — see this delivery's own instructions).

Pagination / resumability — Gmail's own mechanics, never a fabricated
UID/delta-token concept
--------------------------------------------------------------------------
Gmail's `messages.list` has no `SINCE <timestamp>`/delta-token primitive
as clean as Graph's own delta query or even IMAP's `UID SEARCH SINCE` —
this module instead uses Gmail's own `after:<epoch-seconds>` search
operator (Gmail's search grammar accepts either a `YYYY/MM/DD` date OR a
raw Unix-epoch-seconds integer for `after:`/`before:` — epoch seconds is
used here for finer-than-one-day granularity than IMAP's own date-only
`SINCE` limitation) as the bounded discovery-window lower bound, and
Gmail's own `nextPageToken` for in-round pagination — mirrors
`services.mailbox.imap.imap_adapter`'s own `_encode_delta_link`/
`_encode_next_link` JSON-based resumable-state encoding, adapted to
Gmail's actual API shape rather than inventing a fake UID/UIDVALIDITY
concept Gmail does not have:

* `delta_link` (a completed round's own durable cursor) encodes
  `{"since_epoch": <int>}` — the lower bound for the NEXT round's
  `after:` query.
* `next_link` (an IN-PROGRESS round, more pages remaining) encodes
  `{"since_epoch": <int>, "page_token": <str>, "running_max": <int>}` —
  `running_max` threads the highest `internalDate` (epoch seconds) seen
  so far THIS round across pages, mirroring
  `imap_adapter._encode_next_link`'s own `resolved_last_uid` threading
  exactly.
* On the round's LAST page (no `nextPageToken`), the new `delta_link`'s
  `since_epoch` is `running_max` minus a conservative one-second safety
  margin — deliberately OVER-inclusive (may re-observe up to a handful
  of messages at the boundary) rather than under-inclusive, exactly
  mirroring IMAP's own documented "over-inclusive is safe, idempotency
  at the message-repository layer absorbs it; under-inclusive would
  silently drop a message" doctrine.
* A message that vanishes between `messages.list` and the per-message
  metadata fetch (`GmailOutcomeStatus.NOT_FOUND`) is skipped silently
  within this ONE page — a documented judgment call: one message
  vanishing between two Gmail API calls (e.g. a concurrent delete) must
  never fail an entire folder round for every OTHER message already
  successfully observed this page.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Mapping, Optional, Sequence

from core.timestamps import utc_now
from services.mailbox.gmail.gmail_client import (
    DEFAULT_METADATA_HEADERS,
    GmailClientProtocol,
    GmailLabel,
    GmailMessageMetadata,
    GmailOAuthClientProtocol,
    GmailOutcomeStatus,
)
from services.mailbox.gmail.secrets import GmailTokenStoreProtocol
from services.mailbox.mailbox import MailboxSourceRepository

#: Same safety-margin reasoning as
#: `services.mailbox.microsoft.adapter._REFRESH_SKEW_SECONDS`.
_REFRESH_SKEW_SECONDS = 120.0

#: See module docstring's "Label/folder normalisation" section — the
#: ONLY three Gmail system labels this delivery monitors, in this fixed,
#: canonical order (a mailbox missing one of them, e.g. no SPAM label
#: yet observed, simply omits that entry — never a crash).
MONITORED_SYSTEM_LABEL_IDS: tuple[str, ...] = ("INBOX", "SPAM", "TRASH")

#: Gmail's own SYSTEM label type discriminator — see
#: `gmail_client.GmailLabel.label_type`'s own docstring.
_SYSTEM_LABEL_TYPE = "system"


@dataclass(frozen=True)
class MonitoredFolder:
    """Mirrors `services.mailbox.microsoft.adapter.MonitoredFolder`'s/
    `services.mailbox.imap.imap_adapter.MonitoredFolder`'s own shape
    exactly (`folder_id` + `display_name`) — the deliberately
    provider-neutral shape `services/mailbox/sweep.py` is allowed to
    know about a folder. `folder_id` is the real Gmail label id (e.g.
    `"INBOX"`) — the actual cursor/query identity key; `display_name` is
    Gmail's own label `name` (for system labels, identical to the id) —
    GUI/human-facing rendering only, never used for identity."""

    folder_id: str
    display_name: str


@dataclass(frozen=True)
class FolderDiscoveryResult:
    status: GmailOutcomeStatus
    folders: Sequence[MonitoredFolder] = field(default_factory=tuple)
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailMessageSummary:
    """Mirrors `services.mailbox.microsoft.graph_client.GraphMessageSummary`'s/
    `services.mailbox.imap.imap_adapter.ImapMessageSummary`'s own field
    shape exactly — every field `services/mailbox/sweep.py` reads from a
    delta-page message, so that file needs zero changes to consume
    this."""

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
class GmailDeltaPageResult:
    """Mirrors `GraphDeltaPageResult`'s/`ImapDeltaPageResult`'s own field
    shape exactly."""

    status: GmailOutcomeStatus
    messages: Sequence[GmailMessageSummary] = field(default_factory=tuple)
    next_link: Optional[str] = None
    delta_link: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailMessageContentResult:
    status: GmailOutcomeStatus
    content: Optional[bytes] = None
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass(frozen=True)
class GmailMessageHeadersResult:
    status: GmailOutcomeStatus
    raw_headers: Sequence[Mapping[str, Optional[str]]] = field(default_factory=tuple)
    retry_after_seconds: Optional[float] = None
    error_detail: Optional[str] = None


def compute_monitored_labels(labels: Sequence[GmailLabel]) -> tuple[MonitoredFolder, ...]:
    """Pure, HTTP-free classification — see module docstring's
    "Label/folder normalisation" section. Classifies by the label's own
    `label_type == "system"` field and `label_id`, NEVER by `name`."""
    by_id = {label.label_id: label for label in labels if label.label_type == _SYSTEM_LABEL_TYPE}
    return tuple(
        MonitoredFolder(folder_id=label_id, display_name=by_id[label_id].name)
        for label_id in MONITORED_SYSTEM_LABEL_IDS
        if label_id in by_id
    )


def _decode_mime_words(value: Optional[str]) -> Optional[str]:
    """RFC 2047-decode a header value (e.g. an encoded `Subject`) —
    mirrors `services.mailbox.imap.imap_adapter._decode_mime_words`
    exactly; returns the ORIGINAL string unchanged if it is not (or
    fails to parse as) encoded-word syntax; never raises."""
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


def _to_message_summary(metadata: GmailMessageMetadata) -> GmailMessageSummary:
    """The ONE place this module composes a `GmailMessageSummary` from a
    real (or faked) `GmailMessageMetadata` — and, critically, the ONE
    place `MailboxMessage.immutable_provider_message_id` is produced for
    Gmail: Gmail's own `message_id`, used DIRECTLY, unmodified — see
    module docstring's "Gmail durable message identity" section."""
    subject = _decode_mime_words(_header_lookup(metadata.raw_headers, "Subject"))
    message_id_header = _header_lookup(metadata.raw_headers, "Message-ID")
    from_header = _header_lookup(metadata.raw_headers, "From")
    sender_display_name, sender_address = (None, None)
    if from_header:
        display_name, address = parseaddr(from_header)
        sender_display_name = _decode_mime_words(display_name) or None
        sender_address = address.strip().lower() if address else None

    received_at: Optional[datetime] = None
    date_header = _header_lookup(metadata.raw_headers, "Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            if parsed is not None:
                received_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            received_at = None
    if received_at is None:
        # Gmail's own server-assigned `internalDate` — a reliable
        # fallback the (sender-controlled, occasionally absent/malformed)
        # `Date` header is not.
        received_at = metadata.internal_date

    # Bounded, best-effort proxy for "this message probably has an
    # attachment" from the TOP-LEVEL Content-Type header alone — mirrors
    # `services.mailbox.imap.imap_adapter`'s own identical, documented
    # scoped simplification (this module's own Stage-A fetch is
    # headers-only, never a full `payload.parts` structure walk, so no
    # real per-attachment filename/content-type/size list is available;
    # `attachment_metadata` therefore stays empty for every Gmail
    # message, exactly like IMAP's own identical choice).
    content_type = (_header_lookup(metadata.raw_headers, "Content-Type") or "").lower()
    has_attachments = "multipart/mixed" in content_type

    return GmailMessageSummary(
        immutable_id=metadata.message_id,
        internet_message_id=message_id_header,
        subject=subject,
        sender_address=sender_address,
        sender_display_name=sender_display_name,
        received_at=received_at,
        has_attachments=has_attachments,
        attachment_metadata=(),
        auth_signals={},
        raw_headers=tuple(metadata.raw_headers),
    )


def _encode_delta_link(*, since_epoch: int) -> str:
    return _json.dumps({"since_epoch": since_epoch}, separators=(",", ":"))


def _decode_delta_link(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        payload = _json.loads(value)
        return int(payload["since_epoch"])
    except (_json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _encode_next_link(*, since_epoch: int, page_token: str, running_max: int) -> str:
    return _json.dumps(
        {"since_epoch": since_epoch, "page_token": page_token, "running_max": running_max}, separators=(",", ":")
    )


def _decode_next_link(value: Optional[str]) -> Optional[tuple[int, str, int]]:
    if not value:
        return None
    try:
        payload = _json.loads(value)
        return int(payload["since_epoch"]), str(payload["page_token"]), int(payload["running_max"])
    except (_json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


class GmailMailboxAdapter:
    def __init__(
        self,
        *,
        oauth_client: GmailOAuthClientProtocol,
        gmail_client: GmailClientProtocol,
        token_store: GmailTokenStoreProtocol,
        mailbox_repository: MailboxSourceRepository,
    ) -> None:
        self._oauth_client = oauth_client
        self._gmail_client = gmail_client
        self._token_store = token_store
        self._mailbox_repository = mailbox_repository

    # -- token lifecycle -------------------------------------------------

    def _persist_refreshed_tokens(self, mailbox_id: str, *, previous_refresh_token: Optional[str], refreshed_tokens) -> None:
        """The ONE place a refresh result is written back — merges in
        the PREVIOUS refresh_token when Google's own refresh response
        omitted one (see module docstring's "Refresh-token preservation"
        section)."""
        new_refresh_token = refreshed_tokens.refresh_token or previous_refresh_token
        self._token_store.write(
            mailbox_id,
            access_token=refreshed_tokens.access_token,
            refresh_token=new_refresh_token,
            expires_at=refreshed_tokens.expires_at,
        )

    def _ensure_fresh_access_token(self, mailbox_id: str) -> tuple[Optional[str], Optional[str]]:
        """Returns (access_token, error_detail) — exactly one is
        non-None. On a refresh failure, marks the mailbox AUTH_REQUIRED
        as a side effect (see module docstring)."""
        tokens = self._token_store.read(mailbox_id)
        if tokens is None:
            return None, "no stored Gmail OAuth tokens for this mailbox — it must be (re)connected"

        now = utc_now()
        if (tokens.expires_at - now).total_seconds() > _REFRESH_SKEW_SECONDS:
            return tokens.access_token, None

        refreshed = self._oauth_client.refresh(refresh_token=tokens.refresh_token)
        if refreshed.status != GmailOutcomeStatus.OK or refreshed.tokens is None:
            detail = refreshed.error_detail or f"token refresh failed ({refreshed.status.value})"
            self._mailbox_repository.mark_microsoft_auth_required(mailbox_id, error_detail=detail)
            return None, detail

        self._persist_refreshed_tokens(mailbox_id, previous_refresh_token=tokens.refresh_token, refreshed_tokens=refreshed.tokens)
        return refreshed.tokens.access_token, None

    def _reactive_refresh(self, mailbox_id: str) -> tuple[Optional[str], Optional[str]]:
        """A request came back AUTH_ERROR despite what this adapter
        believed was a fresh token — refresh once (reactive path,
        mirrors `MicrosoftGraphMailboxAdapter._reactive_refresh`'s own
        identical reasoning) and return the new token, or the failure
        detail."""
        tokens = self._token_store.read(mailbox_id)
        refresh_token = tokens.refresh_token if tokens is not None else None
        if refresh_token is None:
            detail = "no stored refresh_token to attempt a reactive refresh with"
            self._mailbox_repository.mark_microsoft_auth_required(mailbox_id, error_detail=detail)
            return None, detail

        refreshed = self._oauth_client.refresh(refresh_token=refresh_token)
        if refreshed.status != GmailOutcomeStatus.OK or refreshed.tokens is None:
            detail = refreshed.error_detail or f"reactive token refresh failed ({refreshed.status.value})"
            self._mailbox_repository.mark_microsoft_auth_required(mailbox_id, error_detail=detail)
            return None, detail

        self._persist_refreshed_tokens(mailbox_id, previous_refresh_token=refresh_token, refreshed_tokens=refreshed.tokens)
        return refreshed.tokens.access_token, None

    def report_connection_error(self, mailbox_id: str, *, error_code: str, error_detail: str) -> None:
        """A genuine, non-auth provider fault (403, or an unexpected
        condition outside single-message handling) — see module
        docstring for why this is ERROR, not AUTH_REQUIRED."""
        self._mailbox_repository.mark_microsoft_connection_error(mailbox_id, error_code=error_code, error_detail=error_detail)

    # -- folder discovery --------------------------------------------------

    def discover_monitored_folders(self, *, mailbox_id: str) -> FolderDiscoveryResult:
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return FolderDiscoveryResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        labels_result = self._gmail_client.list_labels(access_token=access_token)
        if labels_result.status == GmailOutcomeStatus.AUTH_ERROR:
            retried_token, retry_error = self._reactive_refresh(mailbox_id)
            if retried_token is None:
                return FolderDiscoveryResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
            labels_result = self._gmail_client.list_labels(access_token=retried_token)
        if labels_result.status != GmailOutcomeStatus.OK:
            return FolderDiscoveryResult(status=labels_result.status, error_detail=labels_result.error_detail)

        return FolderDiscoveryResult(status=GmailOutcomeStatus.OK, folders=compute_monitored_labels(labels_result.labels))

    # -- delta / content / headers fetch, with bounded 401-retry-once -----

    def fetch_folder_delta(
        self,
        *,
        mailbox_id: str,
        folder: str,
        delta_link: Optional[str] = None,
        next_link: Optional[str] = None,
        bootstrap_timestamp: Optional[datetime] = None,
    ) -> GmailDeltaPageResult:
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return GmailDeltaPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        if next_link is not None:
            decoded_next = _decode_next_link(next_link)
            if decoded_next is None:
                return GmailDeltaPageResult(
                    status=GmailOutcomeStatus.MALFORMED_RESPONSE, error_detail=f"invalid Gmail next_link: {next_link!r}"
                )
            since_epoch, page_token, running_max = decoded_next
        else:
            decoded_delta = _decode_delta_link(delta_link)
            if decoded_delta is not None:
                since_epoch = decoded_delta
            elif bootstrap_timestamp is not None:
                since_epoch = int(bootstrap_timestamp.timestamp())
            else:
                since_epoch = 0
            page_token = None
            running_max = since_epoch

        query = f"after:{since_epoch}" if since_epoch else None
        list_result = self._gmail_client.list_messages(access_token=access_token, label_id=folder, query=query, page_token=page_token)
        if list_result.status == GmailOutcomeStatus.AUTH_ERROR:
            retried_token, retry_error = self._reactive_refresh(mailbox_id)
            if retried_token is None:
                return GmailDeltaPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
            access_token = retried_token
            list_result = self._gmail_client.list_messages(access_token=access_token, label_id=folder, query=query, page_token=page_token)
        if list_result.status != GmailOutcomeStatus.OK:
            return GmailDeltaPageResult(
                status=list_result.status, retry_after_seconds=list_result.retry_after_seconds, error_detail=list_result.error_detail
            )

        # De-duplicate message ids WITHIN this one page before fetching
        # metadata for each — see module docstring's "Not processing the
        # same message twice per pass" section. Preserves list order
        # (`dict.fromkeys` — insertion-ordered).
        unique_message_ids = tuple(dict.fromkeys(list_result.message_ids))

        messages: list[GmailMessageSummary] = []
        new_running_max = running_max
        for message_id in unique_message_ids:
            metadata_result = self._gmail_client.fetch_message_metadata(access_token=access_token, message_id=message_id)
            if metadata_result.status == GmailOutcomeStatus.AUTH_ERROR:
                retried_token, retry_error = self._reactive_refresh(mailbox_id)
                if retried_token is None:
                    return GmailDeltaPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
                access_token = retried_token
                metadata_result = self._gmail_client.fetch_message_metadata(access_token=access_token, message_id=message_id)
            if metadata_result.status == GmailOutcomeStatus.NOT_FOUND:
                # Vanished between list and get — skip this ONE message,
                # never fail the whole page (see module docstring).
                continue
            if metadata_result.status != GmailOutcomeStatus.OK or metadata_result.metadata is None:
                return GmailDeltaPageResult(
                    status=metadata_result.status,
                    retry_after_seconds=metadata_result.retry_after_seconds,
                    error_detail=metadata_result.error_detail,
                )

            messages.append(_to_message_summary(metadata_result.metadata))
            if metadata_result.metadata.internal_date is not None:
                new_running_max = max(new_running_max, int(metadata_result.metadata.internal_date.timestamp()))

        if list_result.next_page_token:
            return GmailDeltaPageResult(
                status=GmailOutcomeStatus.OK,
                messages=tuple(messages),
                next_link=_encode_next_link(since_epoch=since_epoch, page_token=list_result.next_page_token, running_max=new_running_max),
            )

        # Round complete — a conservative one-second safety margin, over-
        # inclusive rather than under-inclusive (see module docstring's
        # "Pagination / resumability" section).
        final_since = max(new_running_max - 1, 0)
        return GmailDeltaPageResult(status=GmailOutcomeStatus.OK, messages=tuple(messages), delta_link=_encode_delta_link(since_epoch=final_since))

    def fetch_message_content(self, *, mailbox_id: str, immutable_message_id: str) -> GmailMessageContentResult:
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return GmailMessageContentResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        result = self._gmail_client.fetch_message_raw(access_token=access_token, message_id=immutable_message_id)
        if result.status != GmailOutcomeStatus.AUTH_ERROR:
            return GmailMessageContentResult(
                status=result.status, content=result.content, retry_after_seconds=result.retry_after_seconds, error_detail=result.error_detail
            )

        retried_token, retry_error = self._reactive_refresh(mailbox_id)
        if retried_token is None:
            return GmailMessageContentResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
        result = self._gmail_client.fetch_message_raw(access_token=retried_token, message_id=immutable_message_id)
        return GmailMessageContentResult(
            status=result.status, content=result.content, retry_after_seconds=result.retry_after_seconds, error_detail=result.error_detail
        )

    def fetch_message_headers(self, *, mailbox_id: str, immutable_message_id: str) -> GmailMessageHeadersResult:
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return GmailMessageHeadersResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        result = self._gmail_client.fetch_message_metadata(
            access_token=access_token, message_id=immutable_message_id, metadata_headers=DEFAULT_METADATA_HEADERS
        )
        if result.status == GmailOutcomeStatus.AUTH_ERROR:
            retried_token, retry_error = self._reactive_refresh(mailbox_id)
            if retried_token is None:
                return GmailMessageHeadersResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
            result = self._gmail_client.fetch_message_metadata(
                access_token=retried_token, message_id=immutable_message_id, metadata_headers=DEFAULT_METADATA_HEADERS
            )
        if result.status != GmailOutcomeStatus.OK or result.metadata is None:
            return GmailMessageHeadersResult(status=result.status, retry_after_seconds=result.retry_after_seconds, error_detail=result.error_detail)
        return GmailMessageHeadersResult(status=GmailOutcomeStatus.OK, raw_headers=result.metadata.raw_headers)

    # -- OAuth begin / callback identity verification ---------------------

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        return self._oauth_client.build_authorize_url(state=state, redirect_uri=redirect_uri)

    def is_configured(self) -> bool:
        return self._oauth_client.is_configured()

    def exchange_code_and_verify_identity(self, *, mailbox_id: str, expected_email_address: str, code: str, redirect_uri: str):
        """The full callback-time sequence: exchange the authorization
        code, then verify the real, server-fetched Gmail identity
        against `expected_email_address` — mirrors
        `MicrosoftGraphMailboxAdapter.exchange_code_and_verify_identity`'s
        own "never trust login_hint/browser-supplied address; compare
        the real verified claim" doctrine exactly. This is the ONE place
        a completed OAuth callback is allowed to mark a mailbox
        `CONNECTED` — and, critically, which `mailbox_id` gets those
        tokens is decided entirely by the CALLER (via the `mailbox_id`
        the consumed `GmailOAuthState` was bound to — see
        `services/mailbox/gmail/oauth_state.py`'s own module docstring),
        never guessed from "the one Gmail mailbox" — this method itself
        has no notion of "the" Gmail mailbox at all, only ever the one
        `mailbox_id` its caller explicitly names, which is exactly what
        lets two independent Gmail connect flows be safely in-flight at
        once. Returns a dataclass the router renders into an honest
        outcome; NEVER writes a token to the token store, and NEVER
        changes connection_state, unless identity verification actually
        passes.
        """
        token_result = self._oauth_client.exchange_code(code=code, redirect_uri=redirect_uri)
        if token_result.status != GmailOutcomeStatus.OK or token_result.tokens is None:
            return _CallbackOutcome(
                ok=False,
                reason="token_exchange_failed",
                detail=token_result.error_detail or token_result.status.value,
                provider_operation="token_exchange",
                provider_status=token_result.status.value,
            )

        # Identity is verified via the Gmail API's own `users.getProfile`
        # (a `GmailClientProtocol` call, not an OAuth-identity call — see
        # `gmail_client.py`'s own module docstring for why) — the ONE
        # identity-verification code path.
        profile_result = self._gmail_client.get_profile(access_token=token_result.tokens.access_token)
        if profile_result.status != GmailOutcomeStatus.OK or profile_result.identity is None:
            return _CallbackOutcome(
                ok=False,
                reason="identity_lookup_failed",
                detail=profile_result.error_detail or profile_result.status.value,
                provider_operation="users.getProfile",
                provider_status=profile_result.status.value,
            )

        identity_email = (profile_result.identity.email or "").strip().lower()
        if not identity_email or identity_email != expected_email_address.strip().lower():
            return _CallbackOutcome(
                ok=False,
                reason="wrong_account",
                detail=(
                    f"authenticated Gmail identity ({profile_result.identity.email!r}) does not match "
                    f"this mailbox's own address ({expected_email_address!r})"
                ),
                provider_operation="users.getProfile",
                provider_status=GmailOutcomeStatus.OK.value,
            )

        # Identity verified — persist tokens and mark CONNECTED.
        self._token_store.write(
            mailbox_id,
            access_token=token_result.tokens.access_token,
            refresh_token=token_result.tokens.refresh_token,
            expires_at=token_result.tokens.expires_at,
        )
        self._mailbox_repository.mark_microsoft_connected(mailbox_id)
        return _CallbackOutcome(
            ok=True, reason="connected", detail="", provider_operation="users.getProfile", provider_status=GmailOutcomeStatus.OK.value
        )


@dataclass(frozen=True)
class _CallbackOutcome:
    ok: bool
    reason: str
    detail: str
    #: The Gmail API/OAuth call this outcome concerns (a small closed
    #: set, e.g. `"token_exchange"`/`"users.getProfile"`) and its
    #: already-bounded `GmailOutcomeStatus` value — threaded through to
    #: `app/api/routers/mailboxes_gmail.py`'s own audit-event payload
    #: INSTEAD OF the free-text `detail` above (which stays for the
    #: operator-facing HTML result page's own wording only, never for
    #: durable storage — see that router's own module docstring, section
    #: H of this delivery's own instructions).
    provider_operation: Optional[str] = None
    provider_status: Optional[str] = None
