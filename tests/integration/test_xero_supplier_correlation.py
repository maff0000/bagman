"""Tests for ``services.xero.supplier_correlation`` (CD-6 bounded
Xero-assisted supplier-domain correlation, ahead of any bulk mailbox-
domain approval). Pure domain-level: `InMemoryNeedsYouRepository` +
`FakeXeroAccountingClient`, no HTTP layer, no database, no Docker, no
real network I/O anywhere (PID §61). Mirrors
`tests/integration/test_xero_domain.py`'s own fast, dependency-free
style.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import identity
from services.needs_you.needs_you import (
    ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW,
    ITEM_TYPE_MAILBOX_DOMAIN_REVIEW,
    InMemoryNeedsYouRepository,
)
from services.xero.client import (
    RawXeroContact,
    RawXeroPurchaseInvoice,
    XeroContactsResult,
    XeroInvoicesResult,
    XeroOutcomeStatus,
)
from services.xero.fake_client import FakeXeroAccountingClient
from services.xero.supplier_correlation import (
    CORRELATION_CLASS_CONTACT_ONLY,
    CORRELATION_CLASS_NONE,
    CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW,
    CORRELATION_CLASS_STRONG,
    XeroSupplierCorrelationFailedError,
    correlate_xero_suppliers_for_open_domain_review_items,
)

MAILBOX_1 = "mbx-1"
MAILBOX_2 = "mbx-2"


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


def _queued_client() -> FakeXeroAccountingClient:
    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_CONTACTS))
    client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.OK, invoices=_INVOICES))
    return client


def test_full_scenario_classifies_every_documented_case_correctly():
    repo = InMemoryNeedsYouRepository()
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
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
    )

    # (a) real AUTHORISED/PAID ACCPAY invoices -> STRONG
    updated_strong = repo.get_needs_you_item(item_strong.item_id)
    assert updated_strong.metadata["xero_correlation_class"] == CORRELATION_CLASS_STRONG
    assert updated_strong.metadata["xero_contact_match"] is True
    assert updated_strong.metadata["xero_purchase_invoice_count"] == 2
    assert updated_strong.metadata["xero_most_recent_purchase_date"] is not None
    assert "Strong Supplier Ltd" in updated_strong.metadata["xero_supplier_reference"]
    assert updated_strong.metadata["xero_is_shared_public_domain"] is False
    assert updated_strong.metadata["xero_correlated_at"] is not None
    assert updated_strong.status == "OPEN"  # never resolved by correlation

    # (b) only DRAFT/VOIDED invoices -> CONTACT_ONLY, never STRONG
    updated_draft = repo.get_needs_you_item(item_draft.item_id)
    assert updated_draft.metadata["xero_correlation_class"] == CORRELATION_CLASS_CONTACT_ONLY
    assert updated_draft.metadata["xero_contact_match"] is True
    assert updated_draft.metadata["xero_purchase_invoice_count"] == 0
    assert updated_draft.metadata["xero_most_recent_purchase_date"] is None

    # (c) no Xero contact at all -> NONE
    updated_unknown = repo.get_needs_you_item(item_unknown.item_id)
    assert updated_unknown.metadata["xero_correlation_class"] == CORRELATION_CLASS_NONE
    assert updated_unknown.metadata["xero_contact_match"] is False

    # (d) shared/public domain WITH real purchase history -> never STRONG
    updated_shared = repo.get_needs_you_item(item_shared.item_id)
    assert updated_shared.metadata["xero_correlation_class"] == CORRELATION_CLASS_SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW
    assert updated_shared.metadata["xero_correlation_class"] != CORRELATION_CLASS_STRONG
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
    assert summary.unique_supplier_domains_derived == 2  # strong-supplier.example + gmail.com
    assert summary.domain_review_items_updated == 5  # only MAILBOX_1's OPEN items
    assert summary.strong_correlation_count == 1
    assert summary.contact_only_correlation_count == 1
    assert summary.no_correlation_count == 2  # unknown + customer-only
    assert summary.shared_domain_count == 1


def test_resolved_items_are_never_touched():
    repo = InMemoryNeedsYouRepository()
    item = _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")
    repo.resolve_needs_you_item(
        item.item_id, new_status="DISMISSED", resolution={"decision": "IGNORE"}, actor_type="USER", actor_id="tester"
    )

    client = _queued_client()
    summary = correlate_xero_suppliers_for_open_domain_review_items(
        mailbox_id=MAILBOX_1,
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
    )

    assert summary.domain_review_items_updated == 0
    resolved = repo.get_needs_you_item(item.item_id)
    assert "xero_correlation_class" not in resolved.metadata


def test_non_ok_contacts_status_raises_typed_error_never_silently_proceeds():
    repo = InMemoryNeedsYouRepository()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")

    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.AUTH_ERROR, error_detail="token expired"))

    with pytest.raises(XeroSupplierCorrelationFailedError):
        correlate_xero_suppliers_for_open_domain_review_items(
            mailbox_id=MAILBOX_1,
            tenant_id="tenant-1",
            access_token="access-1",
            xero_client=client,
            needs_you_repository=repo,
        )


def test_non_ok_invoices_status_raises_typed_error_never_silently_proceeds():
    repo = InMemoryNeedsYouRepository()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_1, sender_domain="strong-supplier.example")

    client = FakeXeroAccountingClient()
    client.queue_contacts_result(XeroContactsResult(status=XeroOutcomeStatus.OK, contacts=_CONTACTS))
    client.queue_invoices_result(XeroInvoicesResult(status=XeroOutcomeStatus.RATE_LIMITED, retry_after_seconds=30.0))

    with pytest.raises(XeroSupplierCorrelationFailedError):
        correlate_xero_suppliers_for_open_domain_review_items(
            mailbox_id=MAILBOX_1,
            tenant_id="tenant-1",
            access_token="access-1",
            xero_client=client,
            needs_you_repository=repo,
        )


def test_no_open_items_for_mailbox_is_a_clean_zero_run():
    repo = InMemoryNeedsYouRepository()
    _make_domain_review_item(repo, mailbox_id=MAILBOX_2, sender_domain="strong-supplier.example")  # different mailbox

    client = _queued_client()
    summary = correlate_xero_suppliers_for_open_domain_review_items(
        mailbox_id=MAILBOX_1,
        tenant_id="tenant-1",
        access_token="access-1",
        xero_client=client,
        needs_you_repository=repo,
    )
    assert summary.domain_review_items_updated == 0
    assert summary.strong_correlation_count == 0
    assert summary.contact_only_correlation_count == 0
    assert summary.no_correlation_count == 0
    assert summary.shared_domain_count == 0
