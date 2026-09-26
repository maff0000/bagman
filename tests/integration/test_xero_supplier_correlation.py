"""Tests for ``services.xero.supplier_correlation`` (CD-6 bounded
Xero-assisted supplier-domain correlation, ahead of any bulk mailbox-
domain approval). Pure domain-level: `InMemoryNeedsYouRepository` +
`InMemoryEntityRepository` + `FakeXeroAccountingClient`, no HTTP layer,
no database, no Docker, no real network I/O anywhere (PID §61). Mirrors
`tests/integration/test_xero_domain.py`'s own fast, dependency-free
style.

CD-6 second-correlation-source WO: every scenario now queues a
`list_bank_transactions` result too (the function calls it
unconditionally, alongside `list_contacts`/`list_purchase_invoices`),
and an `entity_id`/`InMemoryEntityRepository` pair is threaded through
every call — see `services/xero/supplier_correlation.py`'s own module
docstring for why (the governed historical-window derivation).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import identity
from core.entity import InMemoryEntityRepository
from services.needs_you.needs_you import (
    ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
    InMemoryNeedsYouRepository,
)
from services.xero.client import (
    RawXeroBankTransaction,
    RawXeroContact,
    RawXeroPurchaseInvoice,
    XeroBankTransactionsResult,
    XeroContactsResult,
    XeroInvoicesResult,
    XeroOutcomeStatus,
)
from services.xero.fake_client import FakeXeroAccountingClient
from services.xero.supplier_correlation import (
    CORRELATION_CLASS_CONTACT_ONLY,
    CORRELATION_CLASS_NONE,
    CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW,
    CORRELATION_CLASS_STRONG_BANK_SPEND,
    CORRELATION_CLASS_STRONG_PURCHASE_BILL,
    EVIDENCE_SOURCE_ACCPAY_INVOICE,
    EVIDENCE_SOURCE_BANKTRANSACTION,
    EVIDENCE_SOURCE_BOTH,
    XeroSupplierCorrelationFailedError,
    correlate_xero_suppliers_for_open_domain_review_items,
)

MAILBOX_1 = "mbx-1"
MAILBOX_2 = "mbx-2"


def _eid() -> str:
    return identity.generate_id()


def _entity_repository_with_configured_entity() -> tuple[InMemoryEntityRepository, str]:
    """A governed entity with `fiscal_year_start_month_day` configured
    (required — `compute_entity_historical_bootstrap` raises `ValueError`
    without it) — mirrors `tests/app_api/test_mailbox_microsoft_endpoints
    .py::_connected_mailbox_with_entity_seeded`'s own fixture shape.
    `FakeXeroAccountingClient.list_bank_transactions` ignores its own
    `since` argument entirely (it just pops whatever was queued), so the
    EXACT derived date is not load-bearing for these tests — only that
    the derivation itself succeeds."""
    repo = InMemoryEntityRepository()
    entity = repo.register_entity(
        entity_type="COMPANY",
        canonical_name="SUPPLIER_CORRELATION_TEST_LTD",
        display_name="Supplier Correlation Test Ltd",
        status="ACTIVE",
        fiscal_year_start_month_day="01-01",
        historical_floor_override_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    return repo, entity.entity_id


def _make_domain_review_item(repo: InMemoryNeedsYouRepository, *, mailbox_id: str, sender_domain: str):
    return repo.create_needs_you_item(
        item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
        domain="MAILBOX",
        source_object_reference=identity.generate_id(),
        question=f"New invoice/accounting-document source detected — domain '{sender_domain}'",
        allowed_action_type=ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
        metadata={"mailbox_id": mailbox_id, "sender_domain": sender_domain},
    )


_CONTACTS = (
    RawXeroContact(
        contact_id="ct-strong",
        name="Strong Supplier Ltd",
        email_address="billing@strong-supplier.example",
        is_customer=False,
        is_supplier=True,
        contact_status="ACTIVE",
    ),
    RawXeroContact(
        contact_id="ct-draft",
        name="Draft Only Ltd",
        email_address="ap@draft-only-supplier.example",
        is_customer=False,
        is_supplier=True,
        contact_status="ACTIVE",
    ),
    RawXeroContact(
        contact_id="ct-shared",
        name="Shared Gmail Person",
        email_address="someone@gmail.com",
        is_customer=False,
        is_supplier=False,
        contact_status="ACTIVE",
    ),
    RawXeroContact(
        contact_id="ct-customer-only",
        name="Customer Only Ltd",
        email_address="ap@customer-only.example",
        is_customer=True,
        is_supplier=False,
        contact_status="ACTIVE",
    ),
)

_INVOICES = (
    RawXeroPurchaseInvoice(
        invoice_id="inv-1",
        contact_id="ct-strong",
        invoice_type="ACCPAY",
        invoice_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        status="AUTHORISED",
    ),
    RawXeroPurchaseInvoice(
        invoice_id="inv-2",
        contact_id="ct-strong",
        invoice_type="ACCPAY",
        invoice_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        status="PAID",
    ),
    RawXeroPurchaseInvoice(
        invoice_id="inv-3",
        contact_id="ct-draft",
        invoice_type="ACCPAY",
        invoice_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="DRAFT",
    ),
    RawXeroPurchaseInvoice(
        invoice_id="inv-4",
        contact_id="ct-draft",
        invoice_type="ACCPAY",
        invoice_date=datetime(2026, 1, 2, tzinfo=timezone.utc),
        status="VOIDED",
    ),
    RawXeroPurchaseInvoice(
        invoice_id="inv-5",
        contact_id="ct-shared",
        invoice_type="ACCPAY",
        invoice_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
        status="AUTHORISED",
    ),
)

#: Empty by default — most scenarios below care only about the
#: invoice-based path; the dedicated bank-spend tests further down
#: queue their own non-empty `RawXeroBankTransaction` tuples.
_NO_BANK_TRANSACTIONS: tuple[RawXeroBankTransaction, ...] = ()


def _queued_client(bank_transactions: tuple = _NO_BANK_TRANSACTIONS) -> FakeXeroAccountingClient:
    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_CONTACTS))
    client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=_INVOICES))
    client.queue_bank_transactions_result(
        XeroBankTransactionsResult(status=XeroOutcomeStatus.OK, bank_transactions=bank_transactions)
    )
    return client


def test_full_scenario_classifies_every_documented_case_correctly():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    item_strong = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")
    item_draft = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="draft-only-supplier.example")
    item_unknown = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="totally-unknown.example")
    item_shared = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="gmail.com")
    item_customer_only = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="customer-only.example")
    # A second mailbox's OPEN item, same domain as the STRONG case —
    # must be completely untouched by a correlation run scoped to
    # MAILBOX_1 (cross-mailbox isolation, proven below).
    other_mailbox_item = _make_domain_review_item(repo, mailbox_id=MAILBOX_2, sender_domain="strong-supplier.example")

    client = _queued_client()
    summary = correlate_xero_suppliers_for_open_domain_review_items(
        mailbox_id=MAILBOX_1,
        entity_id=entity_id,
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
        entity_repository=entity_repository,
    )

    # (a) real AUTHORISED/PAID ACCPAY invoices -> STRONG_PURCHASE_BILL
    updated_strong = repo.get_needs_you_item(item_strong.item_id)
    assert updated_strong.metadata["xero_correlation_class"] == CORRELATION_CLASS_STRONG_PURCHASE_BILL
    assert updated_strong.metadata["xero_contact_match"] is True
    assert updated_strong.metadata["xero_purchase_invoice_count"] == 2
    assert updated_strong.metadata["xero_most_recent_purchase_date"] is not None
    assert "Strong Supplier Ltd" in updated_strong.metadata["xero_supplier_reference"]
    assert updated_strong.metadata["xero_is_shared_public_domain"] is False
    assert updated_strong.metadata["xero_correlated_at"] is not None
    assert updated_strong.metadata["xero_evidence_source"] == EVIDENCE_SOURCE_ACCPAY_INVOICE
    assert updated_strong.metadata["xero_bank_spend_count"] == 0
    assert updated_strong.status == "OPEN"  # never resolved by correlation

    # (b) only DRAFT/VOIDED invoices -> CONTACT_ONLY, never STRONG_*
    updated_draft = repo.get_needs_you_item(item_draft.item_id)
    assert updated_draft.metadata["xero_correlation_class"] == CORRELATION_CLASS_CONTACT_ONLY
    assert updated_draft.metadata["xero_contact_match"] is True
    assert updated_draft.metadata["xero_purchase_invoice_count"] == 0
    assert updated_draft.metadata["xero_most_recent_purchase_date"] is None
    assert updated_draft.metadata["xero_evidence_source"] is None

    # (c) no Xero contact at all -> NONE
    updated_unknown = repo.get_needs_you_item(item_unknown.item_id)
    assert updated_unknown.metadata["xero_correlation_class"] == CORRELATION_CLASS_NONE
    assert updated_unknown.metadata["xero_contact_match"] is False

    # (d) shared/public domain WITH real purchase history -> never STRONG_*
    updated_shared = repo.get_needs_you_item(item_shared.item_id)
    assert updated_shared.metadata["xero_correlation_class"] == CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW
    assert updated_shared.metadata["xero_correlation_class"] not in (
        CORRELATION_CLASS_STRONG_PURCHASE_BILL,
        CORRELATION_CLASS_STRONG_BANK_SPEND,
    )
    assert updated_shared.metadata["xero_is_shared_public_domain"] is True
    assert updated_shared.metadata["xero_purchase_invoice_count"] == 1

    # (e) IsCustomer=True with no real purchase history -> excluded entirely
    updated_customer_only = repo.get_needs_you_item(item_customer_only.item_id)
    assert updated_customer_only.metadata["xero_correlation_class"] == CORRELATION_CLASS_NONE
    assert updated_customer_only.metadata["xero_contact_match"] is False

    # Cross-mailbox isolation: MAILBOX_2's item was never touched.
    untouched = repo.get_needs_you_item(other_mailbox_item.item_id)
    assert "xero_correlation_class" not in untouched.metadata

    # Never resolves anything, never creates a MailboxDomainRule (this
    # test never even constructs a MailboxDomainRuleRepository — the
    # function signature has no parameter for one at all).
    for item in (updated_strong, updated_draft, updated_unknown, updated_shared, updated_customer_only, untouched):
        assert item.status == "OPEN"

    # Summary counts.
    assert summary.contacts_read == 4
    assert summary.purchase_invoices_examined == 5  # raw ACCPAY count, before AUTHORISED/PAID filtering
    assert summary.bank_transactions_examined == 0
    assert summary.qualifying_authorised_spend_count == 0
    assert summary.distinct_contacts_in_qualifying_spend == 0
    assert summary.unique_supplier_domains_derived == 2  # strong-supplier.example + gmail.com
    assert summary.unique_contact_domains_with_spend_activity == 0
    assert summary.domain_review_items_updated == 5  # only MAILBOX_1's OPEN items
    assert summary.strong_purchase_bill_count == 1
    assert summary.strong_bank_spend_count == 0
    assert summary.contact_only_correlation_count == 1
    assert summary.no_correlation_count == 2  # unknown + customer-only
    assert summary.shared_domain_count == 1


def test_resolved_items_are_never_touched():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    item = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")
    repo.resolve_needs_you_item(
        item.item_id, new_status="DISMISSED", resolution={"decision": "IGNORE"}, actor_type="USER", actor_id="tester"
    )

    client = _queued_client()
    summary = correlate_xero_suppliers_for_open_domain_review_items(
        mailbox_id=MAILBOX_1,
        entity_id=entity_id,
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
        entity_repository=entity_repository,
    )

    assert summary.domain_review_items_updated == 0
    resolved = repo.get_needs_you_item(item.item_id)
    assert "xero_correlation_class" not in resolved.metadata


def test_non_ok_contacts_status_raises_typed_error_never_silently_proceeds():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")

    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.AUTH_ERROR, error_detail="token expired"))

    with pytest.raises(XeroSupplierCorrelationFailedError):
        correlate_xero_suppliers_for_open_domain_review_items(
            mailbox_id=MAILBOX_1,
            entity_id=entity_id,
            tenant_id="tenant-1",
            access_token="access-1",
            xero_client=client,
            needs_you_repository=repo,
            entity_repository=entity_repository,
        )


def test_non_ok_invoices_status_raises_typed_error_never_silently_proceeds():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")

    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_CONTACTS))
    client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.RATE_LIMITED, retry_after_seconds=30.0))

    with pytest.raises(XeroSupplierCorrelationFailedError):
        correlate_xero_suppliers_for_open_domain_review_items(
            mailbox_id=MAILBOX_1,
            entity_id=entity_id,
            tenant_id="tenant-1",
            access_token="access-1",
            xero_client=client,
            needs_you_repository=repo,
            entity_repository=entity_repository,
        )


def test_non_ok_bank_transactions_status_raises_typed_error_never_silently_proceeds():
    """The new third source's own equivalent honest-failure proof: a
    non-OK `list_bank_transactions` result raises, never silently
    proceeds as if the invoice-only correlation were complete."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")

    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_CONTACTS))
    client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=_INVOICES))
    client.queue_bank_transactions_result(
        XeroBankTransactionsResult(status=XeroOutcomeStatus.AUTH_ERROR, error_detail="insufficient scope")
    )

    with pytest.raises(XeroSupplierCorrelationFailedError):
        correlate_xero_suppliers_for_open_domain_review_items(
            mailbox_id=MAILBOX_1,
            entity_id=entity_id,
            tenant_id="tenant-1",
            access_token="access-1",
            xero_client=client,
            needs_you_repository=repo,
            entity_repository=entity_repository,
        )


def test_no_open_items_for_mailbox_is_a_clean_zero_run():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_2, sender_domain="strong-supplier.example")  # different mailbox

    client = _queued_client()
    summary = correlate_xero_suppliers_for_open_domain_review_items(
        mailbox_id=MAILBOX_1,
        entity_id=entity_id,
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
        entity_repository=entity_repository,
    )
    assert summary.domain_review_items_updated == 0
    assert summary.strong_purchase_bill_count == 0
    assert summary.strong_bank_spend_count == 0
    assert summary.contact_only_correlation_count == 0
    assert summary.no_correlation_count == 0
    assert summary.shared_domain_count == 0


# ---------------------------------------------------------------------
# SPEND BankTransaction correlation (CD-6 second-correlation-source WO)
# — the architect's own required test list, verbatim.
# ---------------------------------------------------------------------

_BANK_SPEND_CONTACTS = (
    RawXeroContact(
        contact_id="ct-bank-spend",
        name="Bank Spend Supplier Ltd",
        email_address="ap@bank-spend-supplier.example",
        is_customer=False,
        is_supplier=True,
        contact_status="ACTIVE",
    ),
    RawXeroContact(
        contact_id="ct-no-history",
        name="No History Ltd",
        email_address="ap@no-history-supplier.example",
        is_customer=False,
        is_supplier=True,
        contact_status="ACTIVE",
    ),
    RawXeroContact(
        contact_id="ct-shared-spend",
        name="Shared Gmail Spender",
        email_address="spender@gmail.com",
        is_customer=False,
        is_supplier=False,
        contact_status="ACTIVE",
    ),
)


def _bank_tx(
    *,
    bank_transaction_id: str,
    transaction_type: str = "SPEND",
    status: str = "AUTHORISED",
    contact_id: str = "ct-bank-spend",
    date=datetime(2026, 3, 1, tzinfo=timezone.utc),
    total: float = 100.0,
    currency_code: str = "GBP",
) -> RawXeroBankTransaction:
    return RawXeroBankTransaction(
        bank_transaction_id=bank_transaction_id,
        transaction_type=transaction_type,
        status=status,
        date=date,
        contact_id=contact_id,
        reference="ref",
        total=total,
        currency_code=currency_code,
        is_reconciled=True,
    )


def _bank_spend_client(bank_transactions: tuple) -> FakeXeroAccountingClient:
    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_BANK_SPEND_CONTACTS))
    client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=()))
    client.queue_bank_transactions_result(
        XeroBankTransactionsResult(status=XeroOutcomeStatus.OK, bank_transactions=bank_transactions)
    )
    return client


def _run_bank_spend_correlation(repo, entity_repository, entity_id, client, *, sender_domain: str):
    item = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain=sender_domain)
    summary = correlate_xero_suppliers_for_open_domain_review_items(
        mailbox_id=MAILBOX_1,
        entity_id=entity_id,
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
        entity_repository=entity_repository,
    )
    return repo.get_needs_you_item(item.item_id), summary


def test_authorised_spend_contributes_supplier_evidence_and_becomes_strong_bank_spend():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-1"),))

    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_STRONG_BANK_SPEND
    assert updated.metadata["xero_contact_match"] is True
    assert updated.metadata["xero_bank_spend_count"] == 1
    assert updated.metadata["xero_bank_spend_first_date"] is not None
    assert updated.metadata["xero_bank_spend_last_date"] is not None
    assert updated.metadata["xero_bank_spend_total_amount"] == 100.0
    assert updated.metadata["xero_bank_spend_currency"] == "GBP"
    assert updated.metadata["xero_evidence_source"] == EVIDENCE_SOURCE_BANKTRANSACTION
    assert summary.strong_bank_spend_count == 1
    assert summary.qualifying_authorised_spend_count == 1
    assert summary.distinct_contacts_in_qualifying_spend == 1
    assert summary.unique_contact_domains_with_spend_activity == 1


def test_receive_transaction_never_contributes_evidence():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-receive", transaction_type="RECEIVE"),))

    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    # A real Contact IS matched by domain (the RECEIVE row's own
    # ContactID resolves fine) — but a RECEIVE row contributes ZERO
    # qualifying evidence, so the contact is never elevated past
    # CONTACT_ONLY (matched, but no real evidence), never NONE (which
    # means "no Contact match at all" — a different, stronger claim).
    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_CONTACT_ONLY
    assert updated.metadata["xero_contact_match"] is True
    assert updated.metadata["xero_bank_spend_count"] == 0
    assert summary.qualifying_authorised_spend_count == 0
    assert summary.strong_bank_spend_count == 0


def test_deleted_transaction_never_counts_as_positive_evidence():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-deleted", status="DELETED"),))

    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    # Matched Contact, zero qualifying evidence (DELETED never counts)
    # -> CONTACT_ONLY, never STRONG_BANK_SPEND and never NONE.
    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_CONTACT_ONLY
    assert updated.metadata["xero_contact_match"] is True
    assert updated.metadata["xero_bank_spend_count"] == 0
    assert summary.qualifying_authorised_spend_count == 0
    assert summary.strong_bank_spend_count == 0


@pytest.mark.parametrize("transfer_type", ["SPEND-OVERPAYMENT", "SPEND-PREPAYMENT", "SPEND-TRANSFER"])
def test_spend_variant_types_never_count_as_qualifying_spend_exact_match_proof(transfer_type):
    """The exact-string-match proof: a real Contact/amount-bearing row
    whose `Type` is a SPEND *variant* (never exactly `"SPEND"`) must
    never be folded in as ordinary supplier expenditure — proves the
    filter is `== "SPEND"`, never `.startswith("SPEND")`."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-variant", transaction_type=transfer_type),))

    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    # Matched Contact, zero qualifying evidence (a SPEND *variant* never
    # exact-matches `"SPEND"`) -> CONTACT_ONLY, never STRONG_BANK_SPEND
    # and never NONE.
    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_CONTACT_ONLY
    assert updated.metadata["xero_contact_match"] is True
    assert updated.metadata["xero_bank_spend_count"] == 0
    assert summary.qualifying_authorised_spend_count == 0
    assert summary.strong_bank_spend_count == 0


def test_duplicate_bank_transaction_id_across_two_synthetic_pages_never_inflates_the_count():
    """Defense-in-depth dedup proof at the correlation layer itself: two
    rows sharing the SAME `BankTransactionID` (simulating what two real
    HTTP pages — or a future caller of `list_bank_transactions` that
    forgot to dedupe — could hand this module) must count as ONE, never
    two."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    page_1_row = _bank_tx(bank_transaction_id="bt-dup", total=100.0)
    page_2_row = _bank_tx(bank_transaction_id="bt-dup", total=100.0)  # same id, as if re-seen on another page
    client = _bank_spend_client((page_1_row, page_2_row))

    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_STRONG_BANK_SPEND
    assert updated.metadata["xero_bank_spend_count"] == 1  # NOT doubled
    assert updated.metadata["xero_bank_spend_total_amount"] == 100.0  # NOT doubled
    assert summary.qualifying_authorised_spend_count == 1


def test_contact_id_to_contact_domain_correlation_works():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-1", contact_id="ct-bank-spend"),))

    updated, _summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )
    assert updated.metadata["xero_contact_match"] is True
    assert "Bank Spend Supplier Ltd" in updated.metadata["xero_supplier_reference"]


def test_shared_domain_stays_manual_review_even_with_qualifying_bank_spend_evidence():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-shared", contact_id="ct-shared-spend"),))

    updated, summary = _run_bank_spend_correlation(repo, entity_repository, entity_id, client, sender_domain="gmail.com")

    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW
    assert updated.metadata["xero_correlation_class"] != CORRELATION_CLASS_STRONG_BANK_SPEND
    assert updated.metadata["xero_is_shared_public_domain"] is True
    assert summary.strong_bank_spend_count == 0
    assert summary.shared_domain_count == 1


def test_contact_with_no_spend_and_no_invoice_history_remains_contact_only():
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client(())  # ct-no-history has zero qualifying rows of either kind

    updated, _summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="no-history-supplier.example"
    )
    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_CONTACT_ONLY
    assert updated.metadata["xero_evidence_source"] is None


def test_no_mailbox_domain_rule_or_evidence_ingestion_from_bank_spend_correlation():
    """Mirrors the existing invoice-based "correlation never touches
    evidence" test — the function's own signature has no
    `MailboxDomainRuleRepository`/evidence-intake dependency at all, so
    this is provable structurally too, but this test proves it
    behaviourally: the item stays OPEN, only its own metadata changes."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client((_bank_tx(bank_transaction_id="bt-1"),))

    updated, _summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )
    assert updated.status == "OPEN"
    assert set(updated.metadata.keys()) >= {
        "mailbox_id",
        "sender_domain",
        "xero_correlation_class",
        "xero_bank_spend_count",
    }


def test_multi_currency_qualifying_spend_never_silently_sums_mismatched_currencies():
    """The architect's own explicit 'currency-safe' requirement: a
    contact with qualifying SPEND evidence in TWO different currencies
    gets `None` for both the total amount and currency fields, never a
    silently-wrong summed number."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = _bank_spend_client(
        (
            _bank_tx(bank_transaction_id="bt-gbp", total=100.0, currency_code="GBP"),
            _bank_tx(bank_transaction_id="bt-usd", total=50.0, currency_code="USD"),
        )
    )

    updated, _summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_STRONG_BANK_SPEND
    assert updated.metadata["xero_bank_spend_count"] == 2  # both rows still counted as real evidence
    assert updated.metadata["xero_bank_spend_total_amount"] is None  # never a silently-summed mismatched total
    assert updated.metadata["xero_bank_spend_currency"] is None


def test_contact_with_both_purchase_bill_and_bank_spend_evidence_prefers_purchase_bill_class():
    """The documented tie-break: purchase-bill wins the single
    `xero_correlation_class` field by convention when a contact has
    BOTH real evidence types — but the bank-spend facts are still
    recorded in their own separate metadata keys regardless."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_BANK_SPEND_CONTACTS))
    client.queue_invoices_result(
        XeroInvoicesResult(
            status=XeroOutcomeStatus.OK,
            invoices=(
                RawXeroPurchaseInvoice(
                    invoice_id="inv-both",
                    contact_id="ct-bank-spend",
                    invoice_type="ACCPAY",
                    invoice_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
                    status="AUTHORISED",
                ),
            ),
        )
    )
    client.queue_bank_transactions_result(
        XeroBankTransactionsResult(status=XeroOutcomeStatus.OK, bank_transactions=(_bank_tx(bank_transaction_id="bt-1"),))
    )

    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    assert updated.metadata["xero_correlation_class"] == CORRELATION_CLASS_STRONG_PURCHASE_BILL
    assert updated.metadata["xero_purchase_invoice_count"] == 1
    assert updated.metadata["xero_bank_spend_count"] == 1  # still recorded, even though it did not win the class
    assert updated.metadata["xero_evidence_source"] == EVIDENCE_SOURCE_BOTH
    assert summary.strong_purchase_bill_count == 1
    assert summary.strong_bank_spend_count == 0  # this contact's win is counted under purchase-bill, not bank-spend


def test_naive_xero_date_never_crashes_correlation_pl_review_finding():
    """PL-review finding: Xero's own `Date` field (used for both
    invoice dates and bank-transaction dates) is not guaranteed to
    carry an explicit UTC offset — `services.xero.client
    ._parse_xero_wire_datetime` can legitimately return a naive
    `datetime`, and `core.timestamps.to_contract_string` raises
    `ValueError` on one (PID §16). The first real live correlation run
    against the actual Infosecurs Xero organisation returned ZERO
    ACCPAY invoices, so this path was never actually exercised against
    real non-null data before now — the BankTransactions correlation
    this test accompanies is the first time a real, non-empty date
    reaches this code. Proves `correlate_xero_suppliers_for_open_domain_review_items`
    completes successfully (never raises) and correctly serialises both
    a naive bank-spend date AND a naive invoice date, coercing each to
    UTC rather than crashing the whole correlation run over one
    timezone-less Xero date string."""
    repo = InMemoryNeedsYouRepository()
    entity_repository, entity_id = _entity_repository_with_configured_entity()
    naive_bank_spend_date = datetime(2026, 3, 1)  # deliberately no tzinfo
    naive_invoice_date = datetime(2026, 2, 1)  # deliberately no tzinfo

    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_BANK_SPEND_CONTACTS))
    client.queue_invoices_result(
        XeroInvoicesResult(
            status=XeroOutcomeStatus.OK,
            invoices=(
                RawXeroPurchaseInvoice(
                    invoice_id="inv-naive",
                    contact_id="ct-bank-spend",
                    invoice_type="ACCPAY",
                    invoice_date=naive_invoice_date,
                    status="AUTHORISED",
                ),
            ),
        )
    )
    client.queue_bank_transactions_result(
        XeroBankTransactionsResult(
            status=XeroOutcomeStatus.OK,
            bank_transactions=(_bank_tx(bank_transaction_id="bt-naive", date=naive_bank_spend_date),),
        )
    )

    # Must not raise ValueError("naive datetime is not permitted...").
    updated, summary = _run_bank_spend_correlation(
        repo, entity_repository, entity_id, client, sender_domain="bank-spend-supplier.example"
    )

    assert summary.strong_purchase_bill_count == 1
    assert updated.metadata["xero_most_recent_purchase_date"] == "2026-02-01T00:00:00Z"
    assert updated.metadata["xero_bank_spend_first_date"] == "2026-03-01T00:00:00Z"
    assert updated.metadata["xero_bank_spend_last_date"] == "2026-03-01T00:00:00Z"
