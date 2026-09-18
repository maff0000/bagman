"""Bounded, on-demand Xero-assisted supplier-domain correlation (CD-6
architect-authorized addendum, ahead of any bulk mailbox-domain
approval).

Background — what this is for
------------------------------
Phase A historical mailbox discovery against `matt@infosecurs.com`
produced 90 OPEN ``MAILBOX_DOMAIN_REVIEW`` Needs You items (one per
distinct candidate-bearing sender domain) and zero evidence — nothing
has been approved yet. Reviewing 90 domains one at a time, with no
extra context beyond "this domain sent N candidate messages", is slow.
This module reads Xero's own Contacts/Invoices (transiently, via
:class:`services.xero.client.XeroAccountingClientProtocol`) and
attaches a bounded, deterministic, NON-AI correlation verdict to each
still-OPEN item's own ``metadata`` — a REVIEW AID only, never an
auto-decision (see "Never auto-approves" below).

Why this lives in ``services/xero/``, not ``services/mailbox/`` or
``services/needs_you/`` — a real, manifest-backed decision, not a coin
flip
------------------------------------------------------------------------
This function straddles two components (it reads Xero data AND writes
mailbox-produced Needs You metadata), exactly the kind of cross-
component orchestration ``services/mailbox/sweep.py`` itself already
performs by importing from ``services.needs_you`` — so "pick a home and
document why" (the WO's own framing) is a real judgment call. The
answer here is NOT arbitrary: both ``services/mailbox/component.yaml``
and ``services/needs_you/component.yaml`` explicitly list
``direct_xero_access`` under their own ``prohibited:`` section — this
module makes a REAL, live-shaped call to Xero's Contacts/Invoices
endpoints (via the real client in production; a fake in every test), so
placing it under ``services/mailbox/`` or ``services/needs_you/`` would
directly contradict an existing, deliberate architectural boundary.
``services/xero/`` has no such prohibition — Xero access is precisely
its job — so this module lives here, consuming
``services.mailbox.domain_rule.normalize_domain`` (the SAME
normalisation every domain rule/candidate message already uses) and
``services.needs_you.needs_you.NeedsYouRepository`` the same way
``services/mailbox/sweep.py`` already consumes
``services.needs_you`` from the opposite direction. See
``services/xero/component.yaml``'s own updated ``consumes:`` list.

No new persisted canonical domain model (read before changing)
------------------------------------------------------------------------
This is a bounded, ON-DEMAND correlation computation, never a new
canonical Xero domain object BAGMAN owns/persists. Xero remains the
system of record; this module reads its Contacts/Invoices transiently,
correlates them against the mailbox's own ALREADY-EXISTING
``MAILBOX_DOMAIN_REVIEW`` Needs You items, and then discards the raw
Xero data — nothing new is written to a `xero_contacts`/`xero_invoices`
table anywhere. Correlation RESULTS are attached to the EXISTING open
Needs You items' own ``metadata`` via the narrow
``NeedsYouRepository.update_item_metadata`` method (the exact same
mechanism ``services/mailbox/sweep.py
._create_or_reuse_domain_review_item`` already uses to accumulate
``candidate_message_count``/``first_seen_at``/etc.). Because nothing
here is persisted/API-boundary-crossing, :class:`XeroSupplierCorrelationSummary`
has no contract schema either — a documented judgment call, not an
oversight (mirrors ``services/xero/client.py``'s own identical
"no contract for a transient Raw* record" decision — see that module's
docstring).

Never auto-approves anything (architect's own explicit constraint)
------------------------------------------------------------------------
This function NEVER creates/upserts a ``MailboxDomainRule`` and NEVER
resolves any Needs You item — it only enriches the ``metadata`` of a
still-OPEN item. Every ``xero_*`` metadata key this function writes is
clearly a review aid: an operator still has to make the real ALLOW/
IGNORE decision (individually, or batched via
``app/api/routers/mailboxes_microsoft.py``'s
``POST .../domain-review/batch-resolve``).

Priority ordering (architect's own explicit instruction, applied
throughout)
------------------------------------------------------------------------
Real, `AUTHORISED`/`PAID` `ACCPAY` purchase-invoice history is the
STRONG signal. A Contact's own self-reported ``IsSupplier``/
``IsCustomer`` flags are NEVER weighted directly — only used to decide
whether a contact is even eligible to be proposed at all (a
customer-only contact, `IsCustomer=True` with zero real purchase
history, is excluded entirely — "Do not treat customers... as suppliers
merely because they exist in Xero"). A contact with real purchase
history is never elevated past a distinct, capped classification when
its email domain is a common shared/public provider (`gmail.com` and
similar) — "the domain cannot safely represent one supplier" — see
:data:`SHARED_PUBLIC_EMAIL_DOMAINS` and
:data:`CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW`.

Closed-vocabulary-but-plain-string doctrine
------------------------------------------------------------------------
``xero_correlation_class`` is a small, DOCUMENTED set of plain string
constants (below), not a rigid Python `enum` — mirrors
``services.mailbox.domain_rule.MailboxDomainRule.source``'s own "open
string with documented known values" convention used elsewhere in this
codebase, applied here even though this particular vocabulary happens
to be closed today (kept as plain strings for consistency with that
established convention, not because a future 5th class is expected).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

from core.errors import BagmanError
from core.timestamps import to_contract_string, utc_now
from services.mailbox.domain_rule import normalize_domain
from services.needs_you.needs_you import ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, NeedsYouRepository
from services.xero.client import (
    RawXeroContact,
    RawXeroPurchaseInvoice,
    XeroAccountingClientProtocol,
    XeroOutcomeStatus,
)

#: The correlation class an item's `xero_correlation_class` metadata
#: key is set to — a documented, non-exhaustive-in-form-but-closed-
#: today plain-string vocabulary (see module docstring).
CORRELATION_CLASS_STRONG = "STRONG"
CORRELATION_CLASS_CONTACT_ONLY = "CONTACT_ONLY"
CORRELATION_CLASS_NONE = "NONE"
#: The architect's own explicit rule: a shared/public email domain must
#: NEVER be classified STRONG, however much real purchase history a
#: contact at that domain has — this is the distinct 4th class a
#: would-otherwise-be-STRONG correlation is capped at instead (chosen
#: over a bare boolean-only flag so a future consumer/GUI can filter/
#: sort on the class alone without also inspecting the shared-domain
#: flag — either design was defensible; this is the one taken).
CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW = "SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW"

CORRELATION_CLASSES = frozenset(
    {
        CORRELATION_CLASS_STRONG,
        CORRELATION_CLASS_CONTACT_ONLY,
        CORRELATION_CLASS_NONE,
        CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW,
    }
)

#: A bounded, DOCUMENTED, deliberately non-exhaustive list of common
#: shared/public email domains (architect's own explicit rule: such a
#: domain "cannot safely represent one supplier"). Never used to
#: auto-decide anything — only to cap a correlation classification and
#: to flag `xero_is_shared_public_domain` honestly for the operator.
#: Extending this list later is a plain data change, never a logic
#: change.
SHARED_PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.uk",
        "icloud.com",
        "aol.com",
        "protonmail.com",
    }
)

#: The judgment call the WO asks to be documented explicitly: only
#: these two Xero `Invoice.Status` values count as "real purchase
#: history" for STRONG-correlation purposes. A `DRAFT`/`VOIDED`-only
#: (or zero-invoice) contact is deliberately never elevated past
#: CONTACT_ONLY, however many DRAFT/VOIDED rows it has — an
#: un-authorised or voided invoice is not evidence anything was ever
#: actually purchased from that contact.
_REAL_PURCHASE_INVOICE_STATUSES = frozenset({"AUTHORISED", "PAID"})


class XeroSupplierCorrelationFailedError(BagmanError):
    """Raised when `xero_client.list_contacts`/`list_purchase_invoices`
    returns a non-OK :class:`services.xero.client.XeroOutcomeStatus` —
    the WO's own "handle a non-OK status honestly" instruction, resolved
    here by RAISING rather than returning a summary with a `FAILED`
    status (a documented judgment call): a raised, typed exception
    cannot be accidentally ignored the way a caller forgetting to check
    a `summary.status == "FAILED"` field could be, and this function's
    contract (a real, complete `XeroSupplierCorrelationSummary`) never
    has to grow a "this summary might be a lie" second reading. Not
    currently mapped to an HTTP status in `app/api/main.py` — no HTTP
    endpoint calls this function in this delivery (backend-only; the
    GUI WO's own endpoint should add a mapping when it wires one)."""

    error_code = "XERO_SUPPLIER_CORRELATION_FAILED"


@dataclass(frozen=True)
class XeroSupplierCorrelationSummary:
    """Return value of :func:`correlate_xero_suppliers_for_open_domain_review_items`.
    Not a persisted/API-boundary contract (see module docstring) — no
    JSON Schema exists for this shape, deliberately."""

    contacts_read: int
    purchase_invoices_examined: int
    unique_supplier_domains_derived: int
    domain_review_items_updated: int
    strong_correlation_count: int
    contact_only_correlation_count: int
    no_correlation_count: int
    shared_domain_count: int


@dataclass(frozen=True)
class _DomainCorrelation:
    """Internal, per-domain aggregation result — never returned to a
    caller directly (folded into the per-item metadata update and the
    summary counts)."""

    correlation_class: str
    purchase_invoice_count: int
    most_recent_purchase_date: Optional[datetime]
    supplier_reference: str


def _extract_email_domain(email_address: Optional[str]) -> Optional[str]:
    if not email_address or "@" not in email_address:
        return None
    domain = email_address.rsplit("@", 1)[-1].strip()
    if not domain:
        return None
    return normalize_domain(domain)


def _rank(correlation: _DomainCorrelation) -> tuple:
    """Deterministic tie-break when more than one Xero Contact shares
    the same email domain (an edge case, not the common shape, but
    never left to dict-iteration-order chance): STRONG outranks the
    shared-domain-capped class, which outranks CONTACT_ONLY; ties within
    the same class prefer more real purchase-invoice history."""
    class_rank = {
        CORRELATION_CLASS_STRONG: 2,
        CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW: 1,
        CORRELATION_CLASS_CONTACT_ONLY: 0,
    }[correlation.correlation_class]
    return (class_rank, correlation.purchase_invoice_count)


def _build_domain_correlation_map(
    *, contacts: tuple, invoices_by_contact: Mapping[str, list]
) -> dict[str, _DomainCorrelation]:
    """Derive `supplier_domain -> _DomainCorrelation` from every Xero
    Contact with a usable email address (step 3 of the module's own
    algorithm — see docstring). A contact contributes nothing here (is
    skipped entirely) when it is `IsCustomer=True` with zero real
    purchase history (architect's own explicit rule — see module
    docstring's "Priority ordering" section)."""
    domain_map: dict[str, _DomainCorrelation] = {}

    for contact in contacts:
        domain = _extract_email_domain(contact.email_address)
        if domain is None:
            continue

        real_invoices = [
            inv for inv in invoices_by_contact.get(contact.contact_id, []) if inv.status in _REAL_PURCHASE_INVOICE_STATUSES
        ]
        has_real_history = len(real_invoices) > 0

        if contact.is_customer and not has_real_history:
            # "Do not treat customers... as suppliers merely because
            # they exist in Xero" — a customer-flagged contact with no
            # real purchase history is excluded entirely, never
            # proposed as any correlation class (even CONTACT_ONLY).
            continue

        is_shared = domain in SHARED_PUBLIC_EMAIL_DOMAINS
        if is_shared and has_real_history:
            correlation_class = CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW
        elif has_real_history:
            correlation_class = CORRELATION_CLASS_STRONG
        else:
            correlation_class = CORRELATION_CLASS_CONTACT_ONLY

        # Note (inherited, pre-existing risk — not introduced here): a
        # Xero `Date` string with no explicit UTC offset parses to a
        # NAIVE `datetime` via `_parse_xero_wire_datetime` (the WO's own
        # explicit instruction was to reuse that exact helper, unchanged
        # — see `services/xero/client.py`'s own docstring). This
        # module's `to_contract_string` call below requires a
        # timezone-aware UTC value (PID §16) and will raise if it ever
        # receives a naive one — an honest failure, never a silently
        # wrong timestamp, and the SAME pre-existing exposure
        # `XeroAccount.source_updated_date_utc`'s own identical
        # `to_contract_string(self.source_updated_date_utc)` call
        # already carries for `UpdatedDateUTC`. Not fixed here — out of
        # this WO's scope (no client.py date-parsing behaviour change
        # was requested), documented instead.
        most_recent = None
        real_dates = [inv.invoice_date for inv in real_invoices if inv.invoice_date is not None]
        if real_dates:
            most_recent = max(real_dates)

        candidate = _DomainCorrelation(
            correlation_class=correlation_class,
            purchase_invoice_count=len(real_invoices),
            most_recent_purchase_date=most_recent,
            supplier_reference=f"{contact.name} ({contact.contact_id})",
        )

        existing = domain_map.get(domain)
        if existing is None or _rank(candidate) > _rank(existing):
            domain_map[domain] = candidate

    return domain_map


def correlate_xero_suppliers_for_open_domain_review_items(
    *,
    mailbox_id: str,
    tenant_id: str,
    access_token: str,
    xero_client: XeroAccountingClientProtocol,
    needs_you_repository: NeedsYouRepository,
    now: Optional[datetime] = None,
) -> XeroSupplierCorrelationSummary:
    """See module docstring. Reads Xero Contacts/Invoices ONCE, derives
    a bounded `supplier_domain -> correlation` map, then enriches every
    currently-OPEN `MAILBOX_DOMAIN_REVIEW` item for `mailbox_id` with a
    review-aid `xero_*` metadata block. Never creates/upserts a
    `MailboxDomainRule`, never resolves any Needs You item.

    Raises:
        XeroSupplierCorrelationFailedError: `xero_client.list_contacts`
            or `xero_client.list_purchase_invoices` returned a non-OK
            status — never silently proceeds with partial/empty data as
            if it were complete (see that error's own docstring for why
            this is a raise, not a `FAILED`-status summary).
    """
    correlated_at = now if now is not None else utc_now()

    contacts_result = xero_client.list_contacts(tenant_id=tenant_id, access_token=access_token)
    if contacts_result.status != XeroOutcomeStatus.OK:
        raise XeroSupplierCorrelationFailedError(
            f"list_contacts returned {contacts_result.status.value} for mailbox_id={mailbox_id!r} "
            f"tenant_id={tenant_id!r}: {contacts_result.error_detail}"
        )

    invoices_result = xero_client.list_purchase_invoices(tenant_id=tenant_id, access_token=access_token)
    if invoices_result.status != XeroOutcomeStatus.OK:
        raise XeroSupplierCorrelationFailedError(
            f"list_purchase_invoices returned {invoices_result.status.value} for mailbox_id={mailbox_id!r} "
            f"tenant_id={tenant_id!r}: {invoices_result.error_detail}"
        )

    contacts: tuple[RawXeroContact, ...] = contacts_result.contacts
    invoices: tuple[RawXeroPurchaseInvoice, ...] = invoices_result.invoices

    invoices_by_contact: dict[str, list] = {}
    for invoice in invoices:
        if invoice.contact_id is None:
            continue
        invoices_by_contact.setdefault(invoice.contact_id, []).append(invoice)

    domain_map = _build_domain_correlation_map(contacts=contacts, invoices_by_contact=invoices_by_contact)
    unique_supplier_domains_derived = sum(1 for c in domain_map.values() if c.purchase_invoice_count > 0)

    # Step 5 — every currently-OPEN MAILBOX_DOMAIN_REVIEW item for THIS
    # mailbox_id (mirrors `services/mailbox/sweep.py
    # ._find_open_domain_review_item`'s own `status="OPEN"` query +
    # `metadata["mailbox_id"]` filter exactly — a second mailbox's open
    # items are never even fetched, let alone touched).
    open_items = [
        item
        for item in needs_you_repository.list_needs_you_items(
            item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, domain="MAILBOX", status="OPEN"
        )
        if item.metadata.get("mailbox_id") == mailbox_id
    ]

    strong_count = 0
    contact_only_count = 0
    no_correlation_count = 0
    shared_domain_count = 0

    for item in open_items:
        sender_domain = item.metadata.get("sender_domain")
        normalized_domain = normalize_domain(sender_domain) if sender_domain else None
        is_shared_domain = normalized_domain in SHARED_PUBLIC_EMAIL_DOMAINS if normalized_domain else False
        correlation = domain_map.get(normalized_domain) if normalized_domain else None

        if correlation is None:
            correlation_class = CORRELATION_CLASS_NONE
            contact_match = False
            purchase_invoice_count = 0
            most_recent_purchase_date = None
            supplier_reference = None
        else:
            correlation_class = correlation.correlation_class
            contact_match = True
            purchase_invoice_count = correlation.purchase_invoice_count
            most_recent_purchase_date = correlation.most_recent_purchase_date
            supplier_reference = correlation.supplier_reference

        if correlation_class == CORRELATION_CLASS_STRONG:
            strong_count += 1
        elif correlation_class == CORRELATION_CLASS_CONTACT_ONLY:
            contact_only_count += 1
        elif correlation_class == CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW:
            shared_domain_count += 1
        else:
            no_correlation_count += 1

        # `update_item_metadata` itself raises `core.errors.ConflictError`
        # if `item` is no longer OPEN — impossible in practice here
        # (this function is synchronous, single-threaded, and the query
        # above already filtered to `status="OPEN"` with no other writer
        # able to run between that query and this update within one
        # call), so this is deliberately NOT caught/skipped: a real
        # occurrence would mean a genuine, surprising concurrent write
        # this function should surface honestly, never silently paper
        # over (see module docstring's "never auto-approves" doctrine —
        # the same "never silently proceed" discipline applies here).
        needs_you_repository.update_item_metadata(
            item.item_id,
            metadata_updates={
                "xero_correlation_class": correlation_class,
                "xero_contact_match": contact_match,
                "xero_purchase_invoice_count": purchase_invoice_count,
                "xero_most_recent_purchase_date": (
                    to_contract_string(most_recent_purchase_date) if most_recent_purchase_date is not None else None
                ),
                "xero_supplier_reference": supplier_reference,
                "xero_is_shared_public_domain": is_shared_domain,
                "xero_correlated_at": to_contract_string(correlated_at),
            },
        )

    return XeroSupplierCorrelationSummary(
        contacts_read=len(contacts),
        purchase_invoices_examined=len(invoices),
        unique_supplier_domains_derived=unique_supplier_domains_derived,
        domain_review_items_updated=len(open_items),
        strong_correlation_count=strong_count,
        contact_only_correlation_count=contact_only_count,
        no_correlation_count=no_correlation_count,
        shared_domain_count=shared_domain_count,
    )
