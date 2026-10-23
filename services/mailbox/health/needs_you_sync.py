"""Provider-neutral ``MAILBOX_AUTH_REQUIRED`` Needs-You sync helper
(WO-BAGMAN-118-MAILBOX-HEALTH-ALERTING §6.A.4).

Generalised, byte-for-byte behaviourally, out of
``app/api/routers/mailboxes_microsoft.py``'s own pre-existing
``_sync_needs_you_for_connection_state`` (the exact function PID
§118.B's own ground-truth section names as "fully wired for Microsoft
... and wired for neither Gmail nor IMAP ... this exact gap is why the
real 2026-10-02 Gmail `invalid_grant` incident never reached the
operator"). Microsoft's own router now calls this shared helper instead
of keeping a private copy — same dedupe key, same idempotent-resolve
behaviour, same metadata shape, same question wording for Microsoft —
zero behavioural change for Microsoft's own existing call sites.

Why the new ``allowed_action_type`` constants for Gmail/IMAP live HERE,
not in ``services.needs_you.needs_you``
------------------------------------------------------------------------
See ``services.mailbox.health.incident``'s own module docstring, "Why
this is its own new Needs You item_type/allowed_action_type" section,
for the identical reasoning applied here: the governing Work Order
authorises exactly one narrow, additive change to
``services/needs_you/needs_you.py`` (an optional ``session`` parameter
on two existing methods) and no other. ``allowed_action_type`` is
deliberately open, non-enum vocabulary, so a plain string constant
declared in the module that owns each provider's own wiring is fully
equivalent at the contract level to one declared centrally.
"""
from __future__ import annotations

from services.mailbox.mailbox import (
    CONNECTION_STATE_CONNECTED,
    PROVIDER_GOOGLE_GMAIL,
    PROVIDER_IMAP,
    PROVIDER_MICROSOFT_GRAPH,
    MailboxSource,
)
from services.needs_you.needs_you import (
    ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX,
    ITEM_TYPE_MAILBOX_AUTH_REQUIRED,
    NeedsYouRepository,
)

#: New, provider-specific `allowed_action_type` values (see module
#: docstring) — Microsoft's own, pre-existing
#: `ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX` is reused unchanged.
ALLOWED_ACTION_CONNECT_GMAIL_MAILBOX = "CONNECT_GMAIL_MAILBOX"
ALLOWED_ACTION_RECONNECT_IMAP_MAILBOX = "RECONNECT_IMAP_MAILBOX"

#: Per-provider (allowed_action_type, operator-facing label) pair — the
#: label feeds the exact same `f"Connect {label} mailbox — ..."` wording
#: Microsoft's own original implementation already used
#: (`"Microsoft 365"` reproduces that original string byte-for-byte).
_PROVIDER_ACTION_AND_LABEL: dict[str, tuple[str, str]] = {
    PROVIDER_MICROSOFT_GRAPH: (ALLOWED_ACTION_CONNECT_MICROSOFT_MAILBOX, "Microsoft 365"),
    PROVIDER_GOOGLE_GMAIL: (ALLOWED_ACTION_CONNECT_GMAIL_MAILBOX, "Gmail"),
    PROVIDER_IMAP: (ALLOWED_ACTION_RECONNECT_IMAP_MAILBOX, "IMAP"),
}


def sync_needs_you_for_connection_state(
    needs_you_repository: NeedsYouRepository, mailbox: MailboxSource, *, actor_type: str, actor_id: str
) -> None:
    """Create exactly ONE unresolved `MAILBOX_AUTH_REQUIRED` item
    (idempotent on `(item_type, mailbox_id)`) when an `ACTIVE` mailbox is
    not `CONNECTED`, and auto-resolve it the moment the mailbox reaches
    `CONNECTED`. Never creates one for a `DISABLED`/`RETIRED` mailbox.
    Provider-neutral: dispatches the `allowed_action_type`/question
    wording via :data:`_PROVIDER_ACTION_AND_LABEL`, keyed on
    `mailbox.provider_kind` — see module docstring for why Microsoft's
    own exact original behaviour is preserved unchanged by this
    generalisation.
    """
    existing = needs_you_repository.find_by_dedupe_key(ITEM_TYPE_MAILBOX_AUTH_REQUIRED, mailbox.mailbox_id)
    if mailbox.connection_state == CONNECTION_STATE_CONNECTED:
        if existing is not None and existing.status == "OPEN":
            needs_you_repository.resolve_needs_you_item(
                existing.item_id,
                new_status="RESOLVED",
                resolution={"mailbox_id": mailbox.mailbox_id, "connected": True},
                actor_type=actor_type,
                actor_id=actor_id,
            )
        return

    if mailbox.status != "ACTIVE":
        return  # a disabled/retired mailbox never raises a fresh Needs You item

    if existing is not None and existing.status == "OPEN":
        return  # already open — never a duplicate unresolved item for the same mailbox

    allowed_action_type, label = _PROVIDER_ACTION_AND_LABEL[mailbox.provider_kind]
    needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_AUTH_REQUIRED,
        domain="MAILBOX",
        source_object_reference=mailbox.mailbox_id,
        question=f"Connect {label} mailbox — {mailbox.display_name} ({mailbox.email_address})",
        allowed_action_type=allowed_action_type,
        metadata={"mailbox_id": mailbox.mailbox_id, "email_address": mailbox.email_address},
    )
