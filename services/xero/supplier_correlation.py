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
._synchronize_domain_review_aggregate`` already uses to recompute
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

Second correlation source — SPEND BankTransactions (real, live finding)
------------------------------------------------------------------------
The first real Xero-assisted correlation run against the live
Infosecurs Xero organisation returned 73 Contacts but ZERO ACCPAY
purchase invoices (0 STRONG, 1 CONTACT_ONLY, 89 NONE against the 90
real OPEN items) — Infosecurs apparently does not use Xero's Bills/
ACCPAY feature at all. The architect's hypothesis, now built here: real
purchase expenditure is instead recorded as direct ``SPEND``
BankTransactions. This module now reads a SECOND, independent,
read-only evidence source (:meth:`services.xero.client
.XeroAccountingClient.list_bank_transactions`, ``Status=="AUTHORISED"``,
``Type` EXACTLY equal to ``"SPEND"`` — never a `.startswith("SPEND")`/
substring match, which would silently fold `SPEND-TRANSFER`/
`SPEND-OVERPAYMENT`/`SPEND-PREPAYMENT` in as if they were ordinary
supplier expenditure) and correlates it through the transaction's own
``ContactID`` to the SAME Contacts list already fetched for the
existing invoice-based path (never fetched twice) — ``ContactID``-based
ONLY, never contact-name fuzzy matching (architect's own explicit,
emphatic instruction; this module has zero fuzzy-matching code
anywhere and stays that way). ``RECEIVE``/``RECEIVE-*`` transactions
NEVER contribute evidence (money coming IN, the opposite of supplier
expenditure) — excluded by the exact-`"SPEND"`-match rule alone, no
separate check needed. A ``DELETED``-status transaction never counts
as positive evidence regardless of its `Type`.

Correlation classes (STRONG split by evidence source, tie-break
documented)
------------------------------------------------------------------------
``CORRELATION_CLASS_STRONG`` is renamed to
:data:`CORRELATION_CLASS_STRONG_PURCHASE_BILL` (a real ACCPAY purchase-
invoice-history contact) and a new sibling,
:data:`CORRELATION_CLASS_STRONG_BANK_SPEND`, is added (a real,
qualifying SPEND-BankTransaction-history contact) — bank-spend evidence
is NEVER collapsed into `CONTACT_ONLY` (architect's own explicit
instruction). Production-data judgment call (recorded here, not
theoretical): the one real correlation run already executed against
the live Infosecurs org wrote the OLD literal `"STRONG"` string into
real, live `NeedsYouItem` metadata. No migration is performed for that
already-written value — the NEXT correlation run for that domain
simply overwrites it with the new class name (`STRONG_PURCHASE_BILL`/
`STRONG_BANK_SPEND` as appropriate); this module makes no persisted-
data-migration guarantee for `xero_correlation_class` (it was never a
contract-schema'd field — see "No new persisted canonical domain
model" above), and re-running correlation is already this feature's own
established, cheap, idempotent remedy for stale metadata.

Tie-break when a single Contact has BOTH real purchase-bill history
AND real qualifying bank-spend history: `STRONG_PURCHASE_BILL` wins the
single `xero_correlation_class` field, by convention — an ACCPAY
purchase invoice is the more traditionally "governed" accounting
artifact (proper AP workflow, a real bill on file) versus a bank feed
line, which is a weaker, less-reviewed signal even when qualifying. The
bank-spend facts (`xero_bank_spend_count`/`_first_date`/`_last_date`/
`_total_amount`/`_currency`) are recorded in their own separate
metadata keys regardless of which class wins — an operator opening
"Details" always sees BOTH evidence trails, never just the winning one.
The SAME convention resolves the sibling cross-contact case (two
DIFFERENT contacts sharing one domain, one with only purchase-bill
history, the other with only bank-spend history) via `_rank`'s own
class-rank ordering, kept consistent with this single-contact rule
rather than inventing a second, different priority order for what is
conceptually the same "which evidence type wins" question.

New per-item metadata (added alongside, never replacing, the existing
`xero_*` keys)
------------------------------------------------------------------------
`xero_bank_spend_count` (int), `xero_bank_spend_first_date`/
`xero_bank_spend_last_date` (nullable ISO dates), `xero_evidence_source`
(`"XERO_ACCPAY_INVOICE"` / `"XERO_BANKTRANSACTION"` /
`"XERO_ACCPAY_INVOICE+XERO_BANKTRANSACTION"` — see
:data:`EVIDENCE_SOURCE_ACCPAY_INVOICE` and siblings), and
`xero_bank_spend_total_amount`/`xero_bank_spend_currency` — populated
ONLY when every qualifying transaction for that contact shares the SAME
`CurrencyCode` (architect's own explicit "currency-safe" requirement:
summing GBP and USD rows into one number would be actively misleading,
never merely imprecise) — a genuinely multi-currency qualifying-spend
contact gets `None` for both fields rather than a silently-wrong sum
(a per-currency breakdown structure was considered and rejected as
more GUI/metadata complexity than this review-aid warrants; `None` is
an honest "cannot safely summarise" signal an operator can still drill
into via the underlying Xero contact directly).

Defense-in-depth deduplication
------------------------------------------------------------------------
`XeroAccountingClient.list_bank_transactions` already deduplicates by
`BankTransactionID` across every real HTTP page it fetches (see that
method's own docstring) — this module ALSO deduplicates independently
when aggregating by `ContactID` here, so the correlation math stays
deterministically correct even if a future caller of that client
method (or a scripted `Fake*` in a test) forgets to dedupe itself.

Historical window — governed, never a hardcoded literal
------------------------------------------------------------------------
`list_bank_transactions`'s own `since` bound is derived via
`services.mailbox.bootstrap_policy.compute_entity_historical_bootstrap`
against the REAL `GovernedEntity` this correlation run is for (a new,
required `entity_id`/`entity_repository` dependency threaded into
:func:`correlate_xero_suppliers_for_open_domain_review_items` for
exactly this — the function had no access to either before). No literal
date (e.g. the real `2024-11-01` Infosecurs floor) is ever hardcoded
here — the SAME governed derivation the mailbox sweep's own historical
bootstrap already uses, now reused a second time against the same
entity a correlation run is scoped to.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

from core.entity import EntityRepository
from core.errors import BagmanError
from core.timestamps import to_contract_string, utc_now
from services.mailbox.bootstrap_policy import compute_entity_historical_bootstrap
from services.mailbox.domain_rule import normalize_domain
from services.needs_you.needs_you import ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, NeedsYouRepository
from services.xero.client import (
    RawXeroBankTransaction,
    RawXeroContact,
    RawXeroPurchaseInvoice,
    XeroAccountingClientProtocol,
    XeroOutcomeStatus,
)

#: The correlation class an item's `xero_correlation_class` metadata
#: key is set to — a documented, non-exhaustive-in-form-but-closed-
#: today plain-string vocabulary (see module docstring). `STRONG` was
#: renamed to `STRONG_PURCHASE_BILL` and split from the new
#: `STRONG_BANK_SPEND` sibling (see module docstring's "Correlation
#: classes" section for the full reasoning and the real-production-data
#: no-migration judgment call).
CORRELATION_CLASS_STRONG_PURCHASE_BILL = "STRONG_PURCHASE_BILL"
CORRELATION_CLASS_STRONG_BANK_SPEND = "STRONG_BANK_SPEND"
CORRELATION_CLASS_CONTACT_ONLY = "CONTACT_ONLY"
CORRELATION_CLASS_NONE = "NONE"
#: The architect's own explicit rule: a shared/public email domain must
#: NEVER be classified STRONG (of either evidence source), however much
#: real purchase/bank-spend history a contact at that domain has — this
#: is the distinct class a would-otherwise-be-STRONG correlation is
#: capped at instead (chosen over a bare boolean-only flag so a future
#: consumer/GUI can filter/sort on the class alone without also
#: inspecting the shared-domain flag — either design was defensible;
#: this is the one taken).
CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW = "SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW"

CORRELATION_CLASSES = frozenset(
    {
        CORRELATION_CLASS_STRONG_PURCHASE_BILL,
        CORRELATION_CLASS_STRONG_BANK_SPEND,
        CORRELATION_CLASS_CONTACT_ONLY,
        CORRELATION_CLASS_NONE,
        CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW,
    }
)

#: `xero_evidence_source` vocabulary — which real Xero evidence type(s)
#: backed a domain's final correlation class (see module docstring's
#: "New per-item metadata" section). Plain strings, same closed-
#: vocabulary-but-plain-string doctrine as `CORRELATION_CLASSES` above.
EVIDENCE_SOURCE_ACCPAY_INVOICE = "XERO_ACCPAY_INVOICE"
EVIDENCE_SOURCE_BANKTRANSACTION = "XERO_BANKTRANSACTION"
EVIDENCE_SOURCE_BOTH = "XERO_ACCPAY_INVOICE+XERO_BANKTRANSACTION"

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
#: history" for STRONG_PURCHASE_BILL-correlation purposes. A
#: `DRAFT`/`VOIDED`-only (or zero-invoice) contact is deliberately never
#: elevated past CONTACT_ONLY, however many DRAFT/VOIDED rows it has —
#: an un-authorised or voided invoice is not evidence anything was ever
#: actually purchased from that contact.
_REAL_PURCHASE_INVOICE_STATUSES = frozenset({"AUTHORISED", "PAID"})

#: The equivalent judgment call for the new bank-spend evidence source:
#: only an `AUTHORISED`-status BankTransaction counts as "real spend
#: history" for STRONG_BANK_SPEND purposes — `DELETED` never counts
#: (architect's own explicit instruction), and BankTransactions have no
#: `PAID`/`DRAFT`/`VOIDED` status vocabulary of their own (unlike
#: Invoices) — `AUTHORISED` is genuinely the only positive-evidence
#: status this endpoint returns for a real transaction. Kept as its own
#: named constant (not a shared one with `_REAL_PURCHASE_INVOICE_STATUSES`)
#: because the two status vocabularies are genuinely different Xero
#: concepts that only coincidentally share the string `"AUTHORISED"`.
_QUALIFYING_BANK_SPEND_STATUSES = frozenset({"AUTHORISED"})

#: The exact `Type` value a BankTransaction must match to ever count as
#: qualifying supplier-spend evidence — an EXACT string match, never a
#: `.startswith("SPEND")`/substring check (architect's own explicit
#: instruction: `SPEND-TRANSFER`/`SPEND-OVERPAYMENT`/`SPEND-PREPAYMENT`
#: must never be silently folded in as ordinary supplier expenditure;
#: `RECEIVE`/`RECEIVE-*` must never count at all — excluded by this
#: exact-match rule alone, with no separate check needed since none of
#: them equal this literal).
_QUALIFYING_BANK_SPEND_TRANSACTION_TYPE = "SPEND"


class XeroSupplierCorrelationFailedError(BagmanError):
    """Raised when `xero_client.list_contacts`/`list_purchase_invoices`/
    `list_bank_transactions` returns a non-OK
    :class:`services.xero.client.XeroOutcomeStatus` — the WO's own
    "handle a non-OK status honestly" instruction, resolved here by
    RAISING rather than returning a summary with a `FAILED` status (a
    documented judgment call): a raised, typed exception cannot be
    accidentally ignored the way a caller forgetting to check a
    `summary.status == "FAILED"` field could be, and this function's
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
    JSON Schema exists for this shape, deliberately.

    `bank_transactions_examined` mirrors `purchase_invoices_examined`'s
    own "raw count before further filtering" doctrine — the number of
    (already client-side-deduplicated) SPEND BankTransaction rows
    `list_bank_transactions` returned, before this module's own
    per-contact `AUTHORISED`-status qualifying filter.
    `qualifying_authorised_spend_count`/`distinct_contacts_in_qualifying_spend`
    describe that qualifying set itself (across every contact, not just
    the ones that ended up winning a domain's own `_DomainCorrelation`).
    `unique_contact_domains_with_spend_activity` mirrors
    `unique_supplier_domains_derived`'s own "domains with real evidence,
    not just any Contact match" doctrine, for the bank-spend evidence
    source specifically. `strong_purchase_bill_count`/
    `strong_bank_spend_count` replace the old single
    `strong_correlation_count` field now that `STRONG` has split into
    two distinct classes (see module docstring)."""

    contacts_read: int
    purchase_invoices_examined: int
    bank_transactions_examined: int
    qualifying_authorised_spend_count: int
    distinct_contacts_in_qualifying_spend: int
    unique_supplier_domains_derived: int
    unique_contact_domains_with_spend_activity: int
    domain_review_items_updated: int
    strong_purchase_bill_count: int
    strong_bank_spend_count: int
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
    evidence_source: Optional[str]
    bank_spend_count: int
    bank_spend_first_date: Optional[datetime]
    bank_spend_last_date: Optional[datetime]
    bank_spend_total_amount: Optional[float]
    bank_spend_currency: Optional[str]


def _to_contract_string_utc_safe(dt: Optional[datetime]) -> Optional[str]:
    """PL-review finding: `core.timestamps.to_contract_string` raises
    `ValueError` on a naive `datetime` (PID §16). `services.xero.client
    ._parse_xero_wire_datetime` can legitimately return a naive value —
    Xero's own `Date` field (used for both invoice dates and bank-
    transaction dates, unlike `UpdatedDateUTC`) is not guaranteed to
    carry an explicit UTC offset — and `_parse_raw_bank_transaction`
    already coerces naive-to-UTC for its own transient `since`
    comparison, but the value actually STORED/RETURNED on
    `RawXeroPurchaseInvoice.invoice_date`/`RawXeroBankTransaction.date`
    was deliberately left exactly as parsed (see that module's own
    docstring). This was a real, live-blocking gap: the first live
    correlation run against Infosecurs returned zero ACCPAY invoices, so
    `xero_most_recent_purchase_date`'s own `to_contract_string` call was
    never actually exercised against a real naive value — but the
    BankTransactions correlation this fixes is about to run against
    real, non-empty Xero data for the first time, where a naive `Date`
    would otherwise crash this whole endpoint with an unhandled 500
    instead of a governed, honest result. Coerces naive to UTC (the same
    "Xero's own naive dates are effectively UTC" assumption
    `_parse_raw_bank_transaction`'s own comparison logic already makes)
    before serialising; `None` in, `None` out."""
    if dt is None:
        return None
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return to_contract_string(aware)


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
    never left to dict-iteration-order chance): `STRONG_PURCHASE_BILL`
    outranks `STRONG_BANK_SPEND` (the SAME "more traditionally governed
    accounting artifact wins by convention" rule module docstring's
    "Correlation classes" section documents for the single-contact-
    both-evidence-types case, applied consistently here to the sibling
    cross-contact-same-domain case), which outranks the shared-domain-
    capped class, which outranks CONTACT_ONLY; ties within the same
    class prefer more real evidence (purchase-invoice count, then
    bank-spend count)."""
    class_rank = {
        CORRELATION_CLASS_STRONG_PURCHASE_BILL: 3,
        CORRELATION_CLASS_STRONG_BANK_SPEND: 2,
        CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW: 1,
        CORRELATION_CLASS_CONTACT_ONLY: 0,
    }[correlation.correlation_class]
    return (class_rank, correlation.purchase_invoice_count, correlation.bank_spend_count)


def _qualifying_bank_spend_transactions(raw_transactions: list) -> list:
    """The ONE place this module applies the architect's exact-match
    qualifying-SPEND-evidence filter: `Type` EXACTLY `"SPEND"` (never
    `.startswith`/substring — see `_QUALIFYING_BANK_SPEND_TRANSACTION_TYPE`'s
    own docstring) and `Status` in `_QUALIFYING_BANK_SPEND_STATUSES`
    (`AUTHORISED` only — `DELETED` never counts). `RECEIVE`/`RECEIVE-*`
    are excluded by the exact-`Type`-match alone; `SPEND-TRANSFER`/
    `SPEND-OVERPAYMENT`/`SPEND-PREPAYMENT` likewise never equal the
    exact literal and are excluded the same way."""
    return [
        tx
        for tx in raw_transactions
        if tx.transaction_type == _QUALIFYING_BANK_SPEND_TRANSACTION_TYPE
        and tx.status in _QUALIFYING_BANK_SPEND_STATUSES
    ]


def _currency_safe_bank_spend_total(qualifying: list) -> tuple[Optional[float], Optional[str]]:
    """`(total_amount, currency_code)` — `(None, None)` unless every
    qualifying transaction that carries a `CurrencyCode` at all shares
    the SAME one (architect's own explicit "currency-safe" requirement
    — see module docstring's "New per-item metadata" section: summing
    mismatched currencies into one number would be actively misleading,
    never merely imprecise, so this returns an honest `None` instead of
    a silently-wrong total). A transaction with a `None` total is
    treated as contributing nothing to the sum (never fabricated), so
    the returned total is a best-effort sum of whatever amounts ARE
    present, not a claim every qualifying row had a real amount."""
    currencies = {tx.currency_code for tx in qualifying if tx.currency_code is not None}
    if len(currencies) != 1:
        return None, None
    currency = next(iter(currencies))
    total = sum(tx.total for tx in qualifying if tx.total is not None)
    return total, currency


def _build_domain_correlation_map(
    *,
    contacts: tuple,
    invoices_by_contact: Mapping[str, list],
    bank_transactions_by_contact: Mapping[str, list],
) -> dict[str, _DomainCorrelation]:
    """Derive `supplier_domain -> _DomainCorrelation` from every Xero
    Contact with a usable email address (step 3 of the module's own
    algorithm — see docstring). A contact contributes nothing here (is
    skipped entirely) when it is `IsCustomer=True` with zero real
    purchase-bill OR bank-spend history (architect's own explicit rule
    — see module docstring's "Priority ordering" section — generalised
    here to cover EITHER evidence source, not just invoices)."""
    domain_map: dict[str, _DomainCorrelation] = {}

    for contact in contacts:
        domain = _extract_email_domain(contact.email_address)
        if domain is None:
            continue

        real_invoices = [
            inv for inv in invoices_by_contact.get(contact.contact_id, []) if inv.status in _REAL_PURCHASE_INVOICE_STATUSES
        ]
        has_invoice_history = len(real_invoices) > 0

        qualifying_spend = _qualifying_bank_spend_transactions(bank_transactions_by_contact.get(contact.contact_id, []))
        has_bank_spend_history = len(qualifying_spend) > 0

        has_real_history = has_invoice_history or has_bank_spend_history

        if contact.is_customer and not has_real_history:
            # "Do not treat customers... as suppliers merely because
            # they exist in Xero" — a customer-flagged contact with no
            # real purchase-bill OR bank-spend history is excluded
            # entirely, never proposed as any correlation class (even
            # CONTACT_ONLY).
            continue

        is_shared = domain in SHARED_PUBLIC_EMAIL_DOMAINS
        if is_shared and has_real_history:
            correlation_class = CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW
        elif has_invoice_history:
            # Tie-break: purchase-bill wins over bank-spend by
            # convention when a contact has BOTH (see module
            # docstring's "Correlation classes" section) — checked
            # first, unconditionally, rather than as a branch of an
            # `elif has_bank_spend_history` chain, so this ordering is
            # the single, obvious source of the rule.
            correlation_class = CORRELATION_CLASS_STRONG_PURCHASE_BILL
        elif has_bank_spend_history:
            correlation_class = CORRELATION_CLASS_STRONG_BANK_SPEND
        else:
            correlation_class = CORRELATION_CLASS_CONTACT_ONLY

        evidence_sources = []
        if has_invoice_history:
            evidence_sources.append(EVIDENCE_SOURCE_ACCPAY_INVOICE)
        if has_bank_spend_history:
            evidence_sources.append(EVIDENCE_SOURCE_BANKTRANSACTION)
        evidence_source = "+".join(evidence_sources) if evidence_sources else None

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
        # already carries for `UpdatedDateUTC` (now also true of
        # `RawXeroBankTransaction.date`, parsed via the SAME shared
        # helper). Not fixed here — out of this WO's scope (no
        # client.py date-parsing behaviour change was requested),
        # documented instead.
        most_recent = None
        real_dates = [inv.invoice_date for inv in real_invoices if inv.invoice_date is not None]
        if real_dates:
            most_recent = max(real_dates)

        bank_spend_dates = [tx.date for tx in qualifying_spend if tx.date is not None]
        bank_spend_first_date = min(bank_spend_dates) if bank_spend_dates else None
        bank_spend_last_date = max(bank_spend_dates) if bank_spend_dates else None
        bank_spend_total_amount, bank_spend_currency = _currency_safe_bank_spend_total(qualifying_spend)

        candidate = _DomainCorrelation(
            correlation_class=correlation_class,
            purchase_invoice_count=len(real_invoices),
            most_recent_purchase_date=most_recent,
            supplier_reference=f"{contact.name} ({contact.contact_id})",
            evidence_source=evidence_source,
            bank_spend_count=len(qualifying_spend),
            bank_spend_first_date=bank_spend_first_date,
            bank_spend_last_date=bank_spend_last_date,
            bank_spend_total_amount=bank_spend_total_amount,
            bank_spend_currency=bank_spend_currency,
        )

        existing = domain_map.get(domain)
        if existing is None or _rank(candidate) > _rank(existing):
            domain_map[domain] = candidate

    return domain_map


def correlate_xero_suppliers_for_open_domain_review_items(
    *,
    mailbox_id: str,
    entity_id: str,
    tenant_id: str,
    access_token: str,
    xero_client: XeroAccountingClientProtocol,
    needs_you_repository: NeedsYouRepository,
    entity_repository: EntityRepository,
    now: Optional[datetime] = None,
) -> XeroSupplierCorrelationSummary:
    """See module docstring. Reads Xero Contacts/Invoices/BankTransactions
    ONCE each, derives a bounded `supplier_domain -> correlation` map
    from BOTH evidence sources, then enriches every currently-OPEN
    `MAILBOX_DOMAIN_REVIEW` item for `mailbox_id` with a review-aid
    `xero_*` metadata block. Never creates/upserts a
    `MailboxDomainRule`, never resolves any Needs You item.

    `entity_id` is the REAL `GovernedEntity` this correlation run is
    against (the SAME entity whose `XeroConnection` `tenant_id`/
    `access_token` belong to — the caller's own responsibility, exactly
    like the existing precondition that `tenant_id`/`access_token`
    already belong to one real, resolved entity) — used here ONLY to
    derive `list_bank_transactions`'s own governed `since` floor via
    `services.mailbox.bootstrap_policy.compute_entity_historical_bootstrap`
    (see module docstring's "Historical window" section: no literal
    date is ever hardcoded).

    Raises:
        XeroSupplierCorrelationFailedError: `xero_client.list_contacts`,
            `xero_client.list_purchase_invoices`, or
            `xero_client.list_bank_transactions` returned a non-OK
            status — never silently proceeds with partial/empty data as
            if it were complete (see that error's own docstring for why
            this is a raise, not a `FAILED`-status summary).
        ValueError: propagated from `compute_entity_historical_bootstrap`
            when `entity_id`'s `GovernedEntity` has no
            `fiscal_year_start_month_day` configured — this function
            invents no fallback bootstrap date, exactly like
            `services.mailbox.sweep.compute_bootstrap_floor`'s own
            identical refusal.
    """
    correlated_at = now if now is not None else utc_now()

    entity = entity_repository.get_entity(entity_id)
    bank_transactions_since = compute_entity_historical_bootstrap(entity, now=correlated_at)

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

    bank_transactions_result = xero_client.list_bank_transactions(
        tenant_id=tenant_id, access_token=access_token, since=bank_transactions_since
    )
    if bank_transactions_result.status != XeroOutcomeStatus.OK:
        raise XeroSupplierCorrelationFailedError(
            f"list_bank_transactions returned {bank_transactions_result.status.value} for "
            f"mailbox_id={mailbox_id!r} tenant_id={tenant_id!r}: {bank_transactions_result.error_detail}"
        )

    contacts: tuple[RawXeroContact, ...] = contacts_result.contacts
    invoices: tuple[RawXeroPurchaseInvoice, ...] = invoices_result.invoices
    bank_transactions: tuple[RawXeroBankTransaction, ...] = bank_transactions_result.bank_transactions

    invoices_by_contact: dict[str, list] = {}
    for invoice in invoices:
        if invoice.contact_id is None:
            continue
        invoices_by_contact.setdefault(invoice.contact_id, []).append(invoice)

    # Defense-in-depth dedup by `BankTransactionID` (see module
    # docstring's "Defense-in-depth deduplication" section) — the real
    # client already dedupes across its own real HTTP pages, but this
    # aggregation never assumes that happened; a globally-seen-id set
    # (rather than per-contact) is strictly stronger, since a
    # `BankTransactionID` is a real Xero-native identity unique across
    # the whole call, never merely within one contact's own rows.
    bank_transactions_by_contact: dict[str, list] = {}
    seen_bank_transaction_ids: set[str] = set()
    for tx in bank_transactions:
        if tx.contact_id is None:
            continue
        if tx.bank_transaction_id in seen_bank_transaction_ids:
            continue
        seen_bank_transaction_ids.add(tx.bank_transaction_id)
        bank_transactions_by_contact.setdefault(tx.contact_id, []).append(tx)

    qualifying_by_contact: dict[str, list] = {
        contact_id: qualifying
        for contact_id, txs in bank_transactions_by_contact.items()
        if (qualifying := _qualifying_bank_spend_transactions(txs))
    }
    qualifying_authorised_spend_count = sum(len(v) for v in qualifying_by_contact.values())
    distinct_contacts_in_qualifying_spend = len(qualifying_by_contact)

    domain_map = _build_domain_correlation_map(
        contacts=contacts, invoices_by_contact=invoices_by_contact, bank_transactions_by_contact=bank_transactions_by_contact
    )
    unique_supplier_domains_derived = sum(1 for c in domain_map.values() if c.purchase_invoice_count > 0)
    unique_contact_domains_with_spend_activity = sum(1 for c in domain_map.values() if c.bank_spend_count > 0)

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

    strong_purchase_bill_count = 0
    strong_bank_spend_count = 0
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
            evidence_source = None
            bank_spend_count = 0
            bank_spend_first_date = None
            bank_spend_last_date = None
            bank_spend_total_amount = None
            bank_spend_currency = None
        else:
            correlation_class = correlation.correlation_class
            contact_match = True
            purchase_invoice_count = correlation.purchase_invoice_count
            most_recent_purchase_date = correlation.most_recent_purchase_date
            supplier_reference = correlation.supplier_reference
            evidence_source = correlation.evidence_source
            bank_spend_count = correlation.bank_spend_count
            bank_spend_first_date = correlation.bank_spend_first_date
            bank_spend_last_date = correlation.bank_spend_last_date
            bank_spend_total_amount = correlation.bank_spend_total_amount
            bank_spend_currency = correlation.bank_spend_currency

        if correlation_class == CORRELATION_CLASS_STRONG_PURCHASE_BILL:
            strong_purchase_bill_count += 1
        elif correlation_class == CORRELATION_CLASS_STRONG_BANK_SPEND:
            strong_bank_spend_count += 1
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
                "xero_most_recent_purchase_date": _to_contract_string_utc_safe(most_recent_purchase_date),
                "xero_supplier_reference": supplier_reference,
                "xero_is_shared_public_domain": is_shared_domain,
                "xero_correlated_at": to_contract_string(correlated_at),
                "xero_evidence_source": evidence_source,
                "xero_bank_spend_count": bank_spend_count,
                "xero_bank_spend_first_date": _to_contract_string_utc_safe(bank_spend_first_date),
                "xero_bank_spend_last_date": _to_contract_string_utc_safe(bank_spend_last_date),
                "xero_bank_spend_total_amount": bank_spend_total_amount,
                "xero_bank_spend_currency": bank_spend_currency,
            },
        )

    return XeroSupplierCorrelationSummary(
        contacts_read=len(contacts),
        purchase_invoices_examined=len(invoices),
        bank_transactions_examined=len(bank_transactions),
        qualifying_authorised_spend_count=qualifying_authorised_spend_count,
        distinct_contacts_in_qualifying_spend=distinct_contacts_in_qualifying_spend,
        unique_supplier_domains_derived=unique_supplier_domains_derived,
        unique_contact_domains_with_spend_activity=unique_contact_domains_with_spend_activity,
        domain_review_items_updated=len(open_items),
        strong_purchase_bill_count=strong_purchase_bill_count,
        strong_bank_spend_count=strong_bank_spend_count,
        contact_only_correlation_count=contact_only_count,
        no_correlation_count=no_correlation_count,
        shared_domain_count=shared_domain_count,
    )
