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

Label/folder normalisation — ONE synthetic BAGMAN discovery stream,
replacing the original three-system-label model entirely
--------------------------------------------------------------------------
Gmail uses LABELS, not folders — a message can carry SEVERAL
simultaneously (many-to-many), unlike IMAP/Microsoft's own folder
hierarchy. This module's reasoning, in the order it was actually
reached (CD-6 follow-on delivery):

1. Initial provider delivery (this package's first Gmail slice) used a
   bounded first implementation: exactly three Gmail SYSTEM labels
   (`INBOX`/`SPAM`/`TRASH`), one `MonitoredFolder` per label, one
   `fetch_folder_delta` round per label per sweep — a deliberate,
   documented, bounded first scope, not an oversight.
2. A pre-historical-sweep review (this delivery) found this
   structurally OMITTED archived mail — any message carrying NONE of
   those three labels (e.g. archived, or carrying only a user/category
   label) was never discovered at all. Historical financial discovery
   must see archived mail too — an old invoice/receipt is exactly the
   kind of thing an operator routinely archives.
3. Gmail's label model is many-to-many, unlike IMAP/Graph's own folder
   hierarchy — a message can carry multiple labels at once, so the old
   per-label query model also risked the SAME message being enumerated
   once per matching monitored label within one sweep round (even
   though `sweep.py`'s own existing `find_by_provider_id`-then-skip-
   if-already-FINAL check already collapsed that safely at the
   provider-neutral layer — see that module's own docstring; this
   delivery never depended solely on that mechanism for correctness,
   only for cheapness).
4. The fix: ONE complete inbound logical stream —
   :data:`GMAIL_ALL_RECEIVED_STREAM_ID` (`"ALL_RECEIVED"`, paired with
   :data:`GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME` — a stable,
   BAGMAN-logical cursor-identity constant, NOT a real Gmail label id,
   and NEVER sent to a real Gmail API request as a `labelIds` value —
   see `GmailClient.list_messages`'s own `label_id=None` handling) —
   gives correct historical coverage AND avoids label-driven duplicate
   enumeration entirely: there is now only ONE `fetch_folder_delta`
   call per sweep round, not three, so a message can no longer be
   enumerated twice within one round at all (the class of duplication
   `sweep.py`'s own dedup mechanism used to also absorb for Gmail
   specifically is now structurally impossible, not merely absorbed).
   `GmailClient.list_messages`'s `include_spam_trash=True` is what
   keeps Spam/Trash coverage for this stream — Gmail's `messages.list`
   otherwise excludes those dispositions from normal results even with
   no `labelIds` restriction at all.
5. `SENT`/`DRAFT` exclusion (the ONLY exclusion this stream applies) is
   based ENTIRELY on Gmail's own real, provider-authored `labelIds` on
   each message's own metadata response (`GmailMessageMetadata
   .label_ids`) — NEVER subject/from heuristics. A message carrying
   `SENT` is excluded even if it ALSO carries `INBOX`/`TRASH`/anything
   else — SENT/DRAFT exclusion always wins, checked independently, and
   is applied strictly AFTER this round's watermark
   (`running_max`/the resulting `delta_link`) has already advanced past
   that message — see :meth:`fetch_folder_delta`'s own watermark-then-
   exclude ordering below. Getting this order wrong would let a page
   whose newest messages are all outbound leave the cursor stuck behind
   them, re-enumerating the same excluded messages on every future
   sweep forever.

This is a CLEAN REPLACEMENT, never an addition — there is exactly ONE
Gmail discovery model in this codebase, never two competing ones (the
old `MONITORED_SYSTEM_LABEL_IDS`/`compute_monitored_labels` doctrine is
gone entirely, not layered under/alongside this one).
:meth:`discover_monitored_folders` therefore no longer needs a
`list_labels()` API round-trip at all (nothing is being discovered FROM
real Gmail labels for this purpose any more) — it still calls
`_ensure_fresh_access_token` first (so a genuinely broken/expired
connection still surfaces `AUTH_ERROR` at the same whole-sweep-
precondition point `sweep.py` already expects — see that module's own
"Folder discovery" section), then returns the single, fixed
:data:`_ALL_RECEIVED_MONITORED_FOLDERS` tuple. `GmailClient.list_labels`
itself is UNCHANGED and NOT removed — it remains available for
connection/health diagnostics elsewhere; this module simply no longer
calls it for folder discovery.

Not-processing-the-same-message-twice-per-pass
--------------------------------------------------------------------------
Within ONE `fetch_folder_delta` call (the single `ALL_RECEIVED` stream's
own paginated `messages.list` round), this module de-duplicates the
page's own message ids before fetching metadata for each
(`list(dict.fromkeys(...))` — see :meth:`fetch_folder_delta` below) —
cheap, defensive, and never relies solely on
`MailboxMessageRepository`'s own idempotency to paper over a redundant
N-times-refetch. Since this delivery collapsed the old three-label model
into ONE stream (see "Label/folder normalisation" above), there is no
longer a SEPARATE second-folder pass a message could also appear under
at all within one sweep round. `services/mailbox/sweep.py`'s own
existing, provider-neutral `find_by_provider_id`-then-skip-if-already-
FINAL check still exists and still protects against a message re-
observed across DIFFERENT sweep ROUNDS (e.g. a page re-read after a
restart, or a resumed `next_link`) — but the WITHIN-one-round
cross-folder duplication class this mechanism used to also absorb for
Gmail specifically is now structurally impossible, not merely absorbed
(this delivery adds no new cross-folder logic to `sweep.py` at all, and
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

Proactive Gmail quota pacing (CD-6 follow-on fix) — a shared governor,
never a reactive-only backoff
--------------------------------------------------------------------------
A real Gmail historical sweep once hit Google's own per-user/per-project
`messages.get` quota mid-round; `gmail_client.py`'s own CD-6 fix now
correctly classifies that condition as `GmailOutcomeStatus.RATE_LIMITED`
(never `PERMISSION_ERROR` — see that module's own docstring) so
`services/mailbox/sweep.py`'s existing bounded retry-then-fail policy
handles it REACTIVELY, but this adapter additionally paces itself
PROACTIVELY: :class:`_GmailMetadataFetchGovernor` enforces a minimum
interval (`_DEFAULT_METADATA_FETCH_MIN_INTERVAL_SECONDS`, ~0.30s) between
sequential `fetch_message_metadata` (`messages.get`) calls, scoped PER
`mailbox_id` (BAGMAN's two independent Gmail mailboxes pace completely
independently — never a globally shared governor). ONE governor instance
lives on `GmailMailboxAdapter`, consulted from BOTH
:meth:`fetch_folder_delta`'s own per-message loop (the PRIMARY pacing
point) and :meth:`fetch_message_headers` (the SECONDARY pacing point,
used by historical-candidate reprocessing) — never two separate
mechanisms. Pacing is applied ONLY to `fetch_message_metadata` — never to
`list_messages`/`list_labels`/`get_profile`/OAuth token exchange/refresh/
`fetch_message_raw`. This never touches page/cursor semantics
(`after:`/`pageToken`/`running_max`/`bootstrap_timestamp`/`delta_link`) —
the governor only inserts a `sleep_fn` call before an existing API call.
"""
from __future__ import annotations

import json as _json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable, Mapping, Optional, Sequence

from core.timestamps import utc_now
from services.mailbox.gmail.gmail_client import (
    DEFAULT_METADATA_HEADERS,
    GmailClientProtocol,
    GmailMessageMetadata,
    GmailOAuthClientProtocol,
    GmailOutcomeStatus,
)
from services.mailbox.gmail.secrets import GmailTokenStoreProtocol
from services.mailbox.mailbox import MailboxSourceRepository

#: Same safety-margin reasoning as
#: `services.mailbox.microsoft.adapter._REFRESH_SKEW_SECONDS`.
_REFRESH_SKEW_SECONDS = 120.0

#: Proactive Gmail quota pacing (CD-6 follow-on fix) — Google's real
#: quota for this project is 6,000 units/minute/user/project;
#: `messages.get` (`GmailClient.fetch_message_metadata`) costs 20 units.
#: Target: no more than ~200 `messages.get` calls per minute per Gmail
#: mailbox -> a minimum interval of ~0.30s between sequential
#: `fetch_message_metadata` calls, enforced PER MAILBOX (never globally
#: shared across the two connected Gmail accounts — see
#: `_GmailMetadataFetchGovernor`'s own docstring). This is proactive
#: SPACING between calls this adapter was always going to make, never a
#: retry mechanism — the existing bounded single-retry-then-fail policy
#: in `services/mailbox/sweep.py` (`_MAX_RATE_LIMIT_BACKOFF_SECONDS`)
#: remains the only retry mechanism, untouched by this constant.
_DEFAULT_METADATA_FETCH_MIN_INTERVAL_SECONDS = 0.30

#: See module docstring's "Label/folder normalisation" section —
#: BAGMAN's own single, synthetic, BAGMAN-logical discovery stream
#: identity: "every Gmail message in the mailbox within the governed
#: time window, including Inbox, archived, Spam, Trash, snoozed/custom-
#: labelled mail, except messages carrying Gmail's own `SENT`/`DRAFT`
#: labels." NOT a real Gmail label — never sent to a real Gmail API
#: request as a `labelIds` value (see `GmailClient.list_messages`'s own
#: `label_id=None` handling in `fetch_folder_delta` below).
GMAIL_ALL_RECEIVED_STREAM_ID: str = "ALL_RECEIVED"

#: GUI/human-facing rendering only for the stream above — never used for
#: identity (mirrors `MonitoredFolder.display_name`'s own doctrine).
GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME: str = "All received mail"

#: Gmail's own real, provider-authored `labelIds` values that mark a
#: message OUTBOUND — checked against each message's own metadata
#: response (`GmailMessageMetadata.label_ids`), NEVER subject/from
#: heuristics. Always Gmail's own uppercase canonical ids — case-
#: sensitive exact match. See module docstring's "Label/folder
#: normalisation" section, point 5.
_OUTBOUND_EXCLUDED_LABEL_IDS: frozenset[str] = frozenset({"SENT", "DRAFT"})


@dataclass(frozen=True)
class MonitoredFolder:
    """Mirrors `services.mailbox.microsoft.adapter.MonitoredFolder`'s/
    `services.mailbox.imap.imap_adapter.MonitoredFolder`'s own shape
    exactly (`folder_id` + `display_name`) — the deliberately
    provider-neutral shape `services/mailbox/sweep.py` is allowed to
    know about a folder. For Gmail, `folder_id` is now always
    :data:`GMAIL_ALL_RECEIVED_STREAM_ID` — BAGMAN's own synthetic
    cursor/query identity key, NOT a real Gmail label id (see module
    docstring's "Label/folder normalisation" section); `display_name` is
    :data:`GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME` — GUI/human-facing
    rendering only, never used for identity."""

    folder_id: str
    display_name: str


#: The single, fixed monitored-folder list `discover_monitored_folders`
#: now returns — see module docstring's "Label/folder normalisation"
#: section for why this no longer needs a `list_labels()` API round-trip
#: at all. A plain module-level constant, not a function — there is
#: nothing left to discover FROM real Gmail labels for this purpose.
_ALL_RECEIVED_MONITORED_FOLDERS: tuple[MonitoredFolder, ...] = (
    MonitoredFolder(folder_id=GMAIL_ALL_RECEIVED_STREAM_ID, display_name=GMAIL_ALL_RECEIVED_STREAM_DISPLAY_NAME),
)


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


#: Top-level media types (no parameters) that, on their own, are a
#: strong signal the message body itself IS an attachment-shaped part —
#: deliberately the same bounded set
#: `services.mailbox.discovery_signals._CANDIDATE_ATTACHMENT_CONTENT_TYPES`
#: uses (kept in sync by hand — that name is private to its own module,
#: so this is a separate, intentionally identical constant rather than a
#: cross-module import of a private symbol).
_ATTACHMENT_SHAPED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/pdf", "image/jpeg", "image/jpg", "image/png", "image/heic"}
)


def _derive_has_attachments(*, content_type: Optional[str], content_disposition: Optional[str]) -> bool:
    """Bounded, metadata-only ATTACHMENT-PRESENCE signal for Gmail's
    `format=metadata` Stage-A fetch — NOT real MIME attachment
    enumeration.

    Honest limits: this module's Stage-A fetch never walks a real
    `payload.parts` tree (no such tree exists at `format=metadata`), so
    nested/multi-part attachment filenames and content-types remain
    permanently unavailable here without requesting materially richer
    message payload data (`format=full`/raw MIME) — a boundary this
    adapter deliberately never crosses (see module docstring). This
    function only answers "does the TOP-LEVEL metadata this module
    already has give any honest reason to believe an attachment is
    present" — a conservative presence signal, nothing more.

    True when ANY of:
    * top-level `Content-Type` CONTAINS `multipart/mixed` (a multipart
      envelope commonly, though not exclusively, carrying attachments);
    * top-level `Content-Disposition` CONTAINS `attachment`;
    * top-level `Content-Type` IS (exact/prefix match on the media type
      itself, tolerating trailing `; parameter=...` text, e.g.
      `application/pdf; name="invoice.pdf"`) one of
      :data:`_ATTACHMENT_SHAPED_CONTENT_TYPES` — the message's own body
      IS an attachment-shaped part.

    Case-insensitive throughout — header value casing is never
    guaranteed."""
    lowered_content_type = (content_type or "").strip().lower()
    lowered_disposition = (content_disposition or "").strip().lower()

    if "multipart/mixed" in lowered_content_type:
        return True
    if "attachment" in lowered_disposition:
        return True

    # Strip any trailing `; parameter=...` text before the exact/prefix
    # match — `Content-Type` values are a media type optionally followed
    # by parameters (e.g. `application/pdf; name="invoice.pdf"`).
    media_type = lowered_content_type.split(";", 1)[0].strip()
    if media_type in _ATTACHMENT_SHAPED_CONTENT_TYPES:
        return True

    return False


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
                received_at = (
                    parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                )
        except (TypeError, ValueError):
            received_at = None
    if received_at is None:
        # Gmail's own server-assigned `internalDate` — a reliable
        # fallback the (sender-controlled, occasionally absent/malformed)
        # `Date` header is not.
        received_at = metadata.internal_date

    has_attachments = _derive_has_attachments(
        content_type=_header_lookup(metadata.raw_headers, "Content-Type"),
        content_disposition=_header_lookup(metadata.raw_headers, "Content-Disposition"),
    )

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


class _GmailMetadataFetchGovernor:
    """Enforces a minimum interval between sequential Gmail
    `messages.get` (`fetch_message_metadata`) calls, scoped PER
    `mailbox_id` — see module-level `_DEFAULT_METADATA_FETCH_MIN_INTERVAL_SECONDS`
    for the quota reasoning this paces against.

    Uses a monotonic clock (never wall-clock — immune to system clock
    adjustments) and injectable `monotonic_fn`/`sleep_fn` so tests can
    drive this deterministically with zero real sleeping. State is
    `mailbox_id`-scoped and instance-scoped (persists across pages
    within one `GmailMailboxAdapter` instance, never reset per-page) —
    this is what gives BAGMAN's two independent Gmail mailboxes
    (`mgs241171@gmail.com`/`matt.george.scott@gmail.com`) fully
    independent pacing: a call for one `mailbox_id` never consults or
    mutates another `mailbox_id`'s own last-call timestamp."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = _DEFAULT_METADATA_FETCH_MIN_INTERVAL_SECONDS,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval_seconds = min_interval_seconds
        self._monotonic_fn = monotonic_fn
        self._sleep_fn = sleep_fn
        self._last_call_at: dict[str, float] = {}

    def wait(self, mailbox_id: str) -> None:
        """Blocks (via `sleep_fn`) only long enough to keep this
        `mailbox_id`'s own calls at least `min_interval_seconds` apart.
        The FIRST call for a given `mailbox_id` never sleeps (no prior
        timestamp exists yet). Always records `mailbox_id`'s own
        last-call timestamp AFTER any sleep, so the next call's own
        elapsed-time measurement starts from here, not from before the
        sleep."""
        now = self._monotonic_fn()
        last = self._last_call_at.get(mailbox_id)
        if last is not None:
            elapsed = now - last
            remaining = self._min_interval_seconds - elapsed
            if remaining > 0:
                self._sleep_fn(remaining)
                now = self._monotonic_fn()
        self._last_call_at[mailbox_id] = now


class GmailMailboxAdapter:
    def __init__(
        self,
        *,
        oauth_client: GmailOAuthClientProtocol,
        gmail_client: GmailClientProtocol,
        token_store: GmailTokenStoreProtocol,
        mailbox_repository: MailboxSourceRepository,
        metadata_fetch_min_interval_seconds: float = _DEFAULT_METADATA_FETCH_MIN_INTERVAL_SECONDS,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._oauth_client = oauth_client
        self._gmail_client = gmail_client
        self._token_store = token_store
        self._mailbox_repository = mailbox_repository
        # See module docstring's own "Proactive Gmail quota pacing"
        # section: ONE shared governor object, consulted from BOTH
        # `fetch_folder_delta`'s per-message loop AND
        # `fetch_message_headers` — never two separate mechanisms.
        self._metadata_fetch_governor = _GmailMetadataFetchGovernor(
            min_interval_seconds=metadata_fetch_min_interval_seconds, monotonic_fn=monotonic_fn, sleep_fn=sleep_fn
        )

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
        """See module docstring's "Label/folder normalisation" section —
        this no longer calls `GmailClient.list_labels()` at all: there is
        nothing left to discover FROM real Gmail labels for this
        purpose, since the monitored-folder list is now the single,
        fixed :data:`_ALL_RECEIVED_MONITORED_FOLDERS` tuple. Still
        validates/refreshes the access token first, so a genuinely
        broken/expired connection still surfaces `AUTH_ERROR` at this
        same whole-sweep-precondition point `sweep.py` already
        expects."""
        access_token, error_detail = self._ensure_fresh_access_token(mailbox_id)
        if access_token is None:
            return FolderDiscoveryResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=error_detail)

        return FolderDiscoveryResult(status=GmailOutcomeStatus.OK, folders=_ALL_RECEIVED_MONITORED_FOLDERS)

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
        """`folder` is always `sweep.py`'s own `monitored_folder.folder_id`
        — for Gmail, always :data:`GMAIL_ALL_RECEIVED_STREAM_ID` — but is
        DELIBERATELY NEVER forwarded to `GmailClient.list_messages` as a
        real `labelIds` value (it is not a real Gmail label — see module
        docstring's "Label/folder normalisation" section); this method
        always calls `list_messages` with `label_id=None,
        include_spam_trash=True` regardless of `folder`'s own value."""
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
        # `label_id=None, include_spam_trash=True` — the `ALL_RECEIVED`
        # stream's own real request shape (see this method's own
        # docstring above and module docstring's "Label/folder
        # normalisation" section). `folder` is deliberately never passed
        # through here.
        list_result = self._gmail_client.list_messages(access_token=access_token, label_id=None, include_spam_trash=True, query=query, page_token=page_token)
        if list_result.status == GmailOutcomeStatus.AUTH_ERROR:
            retried_token, retry_error = self._reactive_refresh(mailbox_id)
            if retried_token is None:
                return GmailDeltaPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
            access_token = retried_token
            list_result = self._gmail_client.list_messages(access_token=access_token, label_id=None, include_spam_trash=True, query=query, page_token=page_token)
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
            # Proactive quota pacing (see module docstring's "Proactive
            # Gmail quota pacing" section) — ONE governor.wait() call
            # immediately before every `fetch_message_metadata` call,
            # including the reactive-refresh retry below (a retried call
            # is still one more `messages.get` round trip against the
            # SAME mailbox's own quota).
            self._metadata_fetch_governor.wait(mailbox_id)
            metadata_result = self._gmail_client.fetch_message_metadata(access_token=access_token, message_id=message_id)
            if metadata_result.status == GmailOutcomeStatus.AUTH_ERROR:
                retried_token, retry_error = self._reactive_refresh(mailbox_id)
                if retried_token is None:
                    return GmailDeltaPageResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
                access_token = retried_token
                self._metadata_fetch_governor.wait(mailbox_id)
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

            metadata = metadata_result.metadata

            # Cursor watermark subtlety (module docstring, "Label/folder
            # normalisation" point 5) — the watermark MUST advance for
            # EVERY successfully-fetched message, REGARDLESS of whether
            # it goes on to be excluded below. If this were conditioned
            # on inclusion, a page whose newest messages are all outbound
            # (SENT/DRAFT) would leave `running_max` behind those
            # messages, and every future sweep would re-enumerate them
            # forever. This ordering — watermark update, THEN exclusion
            # decision — is load-bearing; never swap it.
            if metadata.internal_date is not None:
                new_running_max = max(new_running_max, int(metadata.internal_date.timestamp()))

            # THEN decide exclusion — SENT/DRAFT always wins, checked
            # independently of any other label the message also carries
            # (e.g. SENT + INBOX is still excluded). Based entirely on
            # Gmail's own real `labelIds`, never subject/from heuristics.
            if _OUTBOUND_EXCLUDED_LABEL_IDS.intersection(metadata.label_ids):
                continue

            messages.append(_to_message_summary(metadata))

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

        # Proactive quota pacing (see module docstring's "Proactive Gmail
        # quota pacing" section) — the SAME shared governor
        # `fetch_folder_delta`'s own per-message loop uses, not a second
        # unrelated mechanism (historical-candidate reprocessing's own
        # secondary pacing point).
        self._metadata_fetch_governor.wait(mailbox_id)
        result = self._gmail_client.fetch_message_metadata(
            access_token=access_token, message_id=immutable_message_id, metadata_headers=DEFAULT_METADATA_HEADERS
        )
        if result.status == GmailOutcomeStatus.AUTH_ERROR:
            retried_token, retry_error = self._reactive_refresh(mailbox_id)
            if retried_token is None:
                return GmailMessageHeadersResult(status=GmailOutcomeStatus.AUTH_ERROR, error_detail=retry_error)
            self._metadata_fetch_governor.wait(mailbox_id)
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
