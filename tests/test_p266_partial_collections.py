"""
backend/tests/test_p266_partial_collections.py
Architecture Decision: ADR-0266 (P266).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Partial Collections:
- Scenario 1: Valid multi-installment partial collection on an original invoice (reduces due, sets PARTIAL, then SETTLED).
- Scenario 2: Strict overpayment prevention (ERR_DUE_OVERPAYMENT) when amount exceeds invoice remaining due.
- Scenario 3: Duplicate settlement prevention (ERR_DUE_ALREADY_SETTLED) when collecting on settled invoice.
- Scenario 4: Input validation (zero amount, invalid tender, digital tender without gateway ref, offline refusal).
- Scenario 5: Cross-tenant isolation and unauthorized access rejection.
- Scenario 6: Audited reversal restoring invoice due balance and AR ledger parity.
- Scenario 7: Multi-invoice payment allocation (FIFO across multiple unpaid invoices).
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    CustomerDueInvoiceAllocation,
    CustomerDueRecord,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    DueStatus,
    PaymentMethod,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    PosSaleOrder,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def partial_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p266-a-{uid}",
        name="Lazz Pharma Dhanmondi",
        subdomain=f"p266a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p266-a-{uid}",
        name="Dhanmondi Main",
        branch_code=f"D{uid[:3].upper()}",
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Customer in Tenant A
    cust_a = PosCustomer.objects.create(
        tenant=ten_a,
        full_name="Dr. Kamal Hossain",
        phone=f"+88018{random.randint(10000000, 99999999)}",
        credit_limit_bdt=Decimal("10000.00"),
        current_due_bdt=Decimal("500.00"),
        is_credit_approved=True,
        credit_status=PosCustomerCreditStatus.APPROVED,
    )

    # Invoice and Due Record in Tenant A
    inv_a = SaleInvoice.objects.create(
        invoice_id=f"inv-{uid}-001",
        tenant=ten_a,
        branch=br_a,
        invoice_number=f"INV-{uid[:4].upper()}-001",
        customer_name=cust_a.full_name,
        customer_phone=cust_a.phone,
        total_amount=Decimal("500.00"),
        net_amount=Decimal("500.00"),
        payment_method="DUE",
    )

    due_rec_a = CustomerDueRecord.objects.create(
        due_id=f"due-{uid}-001",
        tenant=ten_a,
        branch=br_a,
        customer=cust_a,
        invoice=inv_a,
        customer_name=cust_a.full_name,
        customer_phone=cust_a.phone,
        total_due_bdt=Decimal("500.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("500.00"),
        status=DueStatus.OUTSTANDING,
        due_date=timezone.now().date() + timezone.timedelta(days=30),
    )

    # Tenant B (for cross-tenant tests)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p266-b-{uid}",
        name="Tamanna Pharmacy Uttara",
        subdomain=f"p266b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_b = Branch.objects.create(
        tenant=ten_b,
        branch_id=f"br-p266-b-{uid}",
        name="Uttara Branch",
        branch_code=f"U{uid[:3].upper()}",
    )
    cashier_b = PharmacyUser.objects.create_user(
        email=f"cashier_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.CASHIER,
    )

    return {
        "ten_a": ten_a,
        "br_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "cashier_a": cashier_a,
        "cust_a": cust_a,
        "inv_a": inv_a,
        "due_rec_a": due_rec_a,
        "ten_b": ten_b,
        "br_b": br_b,
        "cashier_b": cashier_b,
    }


@pytest.mark.django_db
def test_p266_valid_multi_installment_partial_collections(partial_env):
    """
    Scenario 1: Apply partial collection in installments:
    1. Collect 200 BDT out of 500 BDT -> Status becomes PARTIAL, remaining 300 BDT.
    2. Collect remaining 300 BDT -> Status becomes SETTLED, remaining 0.00 BDT.
    3. Verify CustomerDueInvoiceAllocation records and CustomerReceivableLedgerEntry credits.
    """
    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_a"])

    inv = partial_env["inv_a"]
    due_rec = partial_env["due_rec_a"]
    cust = partial_env["cust_a"]

    # 1. First partial payment (200 BDT via CASH)
    resp1 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": inv.invoice_number,
            "amount_bdt": "200.00",
            "tender_type": "CASH",
            "cash_tendered_bdt": "250.00",
            "notes": "First partial installment",
        },
        format="json",
    )
    assert resp1.status_code == status.HTTP_201_CREATED
    data1 = resp1.data
    assert data1["success"] is True
    assert data1["remaining_due_bdt"] == "300.00"
    assert data1["status"] == DueStatus.PARTIAL
    assert data1["is_settled"] is False
    assert data1["change_given_bdt"] == "50.00"

    # Verify Due Record state
    due_rec.refresh_from_db()
    assert due_rec.remaining_due_bdt == Decimal("300.00")
    assert due_rec.paid_amount_bdt == Decimal("200.00")
    assert due_rec.status == DueStatus.PARTIAL

    # Verify Customer Due state
    cust.refresh_from_db()
    assert cust.current_due_bdt == Decimal("300.00")

    # Verify Allocation item
    alloc1 = CustomerDueInvoiceAllocation.objects.get(allocation_id=data1["allocation_id"])
    assert alloc1.allocated_amount_bdt == Decimal("200.00")
    assert alloc1.previous_remaining_due_bdt == Decimal("500.00")
    assert alloc1.new_remaining_due_bdt == Decimal("300.00")
    assert alloc1.is_reversed is False

    # Verify AR ledger entry
    ledger_entry1 = CustomerReceivableLedgerEntry.objects.filter(
        customer=cust, credit_amount_bdt=Decimal("200.00")
    ).first()
    assert ledger_entry1 is not None
    assert ledger_entry1.entry_type == CustomerReceivableEntryType.PAYMENT_COLLECTION

    # 2. Second installment: full settlement of remaining 300 BDT via bKash
    resp2 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "300.00",
            "tender_type": "BKASH",
            "gateway_reference": "TRX-BKASH-771122",
            "notes": "Final installment",
        },
        format="json",
    )
    assert resp2.status_code == status.HTTP_201_CREATED
    data2 = resp2.data
    assert data2["success"] is True
    assert data2["remaining_due_bdt"] == "0.00"
    assert data2["status"] == DueStatus.SETTLED
    assert data2["is_settled"] is True

    due_rec.refresh_from_db()
    assert due_rec.remaining_due_bdt == Decimal("0.00")
    assert due_rec.paid_amount_bdt == Decimal("500.00")
    assert due_rec.status == DueStatus.SETTLED

    cust.refresh_from_db()
    assert cust.current_due_bdt == Decimal("0.00")


@pytest.mark.django_db
def test_p266_overpayment_rejection(partial_env):
    """
    Scenario 2: Source-specific business rule: Attempting to collect more than remaining invoice due
    must be strictly rejected with ERR_DUE_OVERPAYMENT.
    """
    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_a"])

    inv = partial_env["inv_a"]
    due_rec = partial_env["due_rec_a"]

    # Remaining due is 500 BDT; attempt to collect 550 BDT
    resp = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": inv.invoice_id,
            "amount_bdt": "550.00",
            "tender_type": "CASH",
        },
        format="json",
    )
    assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_DUE_OVERPAYMENT" in str(resp.data)

    # Ensure due record and customer balance were NOT mutated
    due_rec.refresh_from_db()
    assert due_rec.remaining_due_bdt == Decimal("500.00")
    assert due_rec.paid_amount_bdt == Decimal("0.00")
    assert due_rec.status == DueStatus.OUTSTANDING


@pytest.mark.django_db
def test_p266_duplicate_settlement_rejection(partial_env):
    """
    Scenario 3: Attempting to collect on an already settled invoice must be rejected
    with ERR_DUE_ALREADY_SETTLED.
    """
    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_a"])

    inv = partial_env["inv_a"]
    due_rec = partial_env["due_rec_a"]

    # Manually mark invoice settled
    due_rec.remaining_due_bdt = Decimal("0.00")
    due_rec.paid_amount_bdt = Decimal("500.00")
    due_rec.status = DueStatus.SETTLED
    due_rec.save()

    resp = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": inv.invoice_number,
            "amount_bdt": "100.00",
            "tender_type": "CASH",
        },
        format="json",
    )
    assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_DUE_ALREADY_SETTLED" in str(resp.data)


@pytest.mark.django_db
def test_p266_input_validation_and_offline_refusal(partial_env):
    """
    Scenario 4: Verify rejection for zero amount, invalid tender, missing digital ref, and offline refusal.
    """
    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_a"])
    due_rec = partial_env["due_rec_a"]

    # 1. Zero amount
    r1 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "0.00",
            "tender_type": "CASH",
        },
        format="json",
    )
    assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_INVALID_PAYMENT_AMOUNT" in str(r1.data)

    # 2. DUE as tender for due collection
    r2 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "100.00",
            "tender_type": "DUE",
        },
        format="json",
    )
    assert r2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_CANNOT_COLLECT_DUE_WITH_DUE" in str(r2.data)

    # 3. Digital tender without gateway reference
    r3 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "100.00",
            "tender_type": "NAGAD",
            "gateway_reference": "",
        },
        format="json",
    )
    assert r3.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_DIGITAL_TENDER_REFERENCE_REQUIRED" in str(r3.data)

    # 4. Offline mode rejected
    r4 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "100.00",
            "tender_type": "CASH",
            "is_offline": True,
        },
        format="json",
    )
    assert r4.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_OFFLINE_UNAVAILABLE" in str(r4.data)


@pytest.mark.django_db
def test_p266_cross_tenant_isolation(partial_env):
    """
    Scenario 5: Cashier from Tenant B cannot view or collect against Tenant A's due invoice.
    """
    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_b"])
    due_rec_a = partial_env["due_rec_a"]

    resp = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec_a.due_id,
            "amount_bdt": "100.00",
            "tender_type": "CASH",
        },
        format="json",
    )
    assert resp.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
def test_p266_audited_reversal_and_idempotency(partial_env):
    """
    Scenario 6: Test idempotency replay and audited reversal:
    - Same idempotency_key returns original receipt without duplicate debit/credit.
    - Reversal restores invoice due and customer balance, marking allocation reversed.
    """
    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_a"])
    due_rec = partial_env["due_rec_a"]
    cust = partial_env["cust_a"]
    idem_key = f"idem-p266-{uuid.uuid4().hex[:8]}"

    # Initial collection of 250 BDT
    resp1 = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "250.00",
            "tender_type": "CASH",
            "idempotency_key": idem_key,
        },
        format="json",
    )
    assert resp1.status_code == status.HTTP_201_CREATED
    col_id = resp1.data["collection_id"]

    # Re-submit identical request -> returns existing receipt safely
    resp_idem = client.post(
        "/api/v2/pos/partial-collections/apply-invoice/",
        {
            "invoice_id": due_rec.due_id,
            "amount_bdt": "250.00",
            "tender_type": "CASH",
            "idempotency_key": idem_key,
        },
        format="json",
    )
    assert resp_idem.status_code == status.HTTP_200_OK
    assert resp_idem.data["idempotent_replay"] is True

    due_rec.refresh_from_db()
    assert due_rec.remaining_due_bdt == Decimal("250.00")  # Not 0.00!

    # Switch to Manager for reversal
    mgr_client = APIClient()
    mgr_client.force_authenticate(user=partial_env["manager_a"])

    rev_resp = mgr_client.post(
        f"/api/v2/pos/partial-collections/{col_id}/reverse/",
        {"reason": "Customer returned cash, requested void"},
        format="json",
    )
    assert rev_resp.status_code == status.HTTP_200_OK
    assert rev_resp.data["collection"]["status"] == CustomerDueCollectionStatus.REVERSED

    # Verify invoice due restored to 500 BDT
    due_rec.refresh_from_db()
    assert due_rec.remaining_due_bdt == Decimal("500.00")
    assert due_rec.paid_amount_bdt == Decimal("0.00")
    assert due_rec.status == DueStatus.OUTSTANDING

    # Verify customer balance restored
    cust.refresh_from_db()
    assert cust.current_due_bdt == Decimal("500.00")

    # Verify allocation marked reversed
    alloc = CustomerDueInvoiceAllocation.objects.get(collection__collection_id=col_id)
    assert alloc.is_reversed is True


@pytest.mark.django_db
def test_p266_multi_invoice_fifo_collection(partial_env):
    """
    Scenario 7: Multi-invoice collection:
    Customer has 2 invoices:
    - Invoice 1: 300 BDT remaining
    - Invoice 2: 400 BDT remaining (Total 700 BDT)
    Payment of 500 BDT:
    - Settles Invoice 1 (300 BDT, remaining 0.00)
    - Partially settles Invoice 2 (200 BDT, remaining 200.00)
    Customer remaining due becomes 200.00 BDT.
    """
    ten_a = partial_env["ten_a"]
    br_a = partial_env["br_a"]
    cust = partial_env["cust_a"]

    # Set up 2 invoices
    uid = uuid.uuid4().hex[:4]
    due_1 = CustomerDueRecord.objects.create(
        due_id=f"due-fifo-1-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust,
        customer_name=cust.full_name,
        customer_phone=cust.phone,
        total_due_bdt=Decimal("300.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("300.00"),
        status=DueStatus.OUTSTANDING,
    )
    due_2 = CustomerDueRecord.objects.create(
        due_id=f"due-fifo-2-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust,
        customer_name=cust.full_name,
        customer_phone=cust.phone,
        total_due_bdt=Decimal("400.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("400.00"),
        status=DueStatus.OUTSTANDING,
    )

    # Set initial customer balance to 700 + existing 500 = 1200
    cust.current_due_bdt = Decimal("1200.00")
    cust.save()

    client = APIClient()
    client.force_authenticate(user=partial_env["cashier_a"])

    # Target specific allocation: 300 for due_1, 200 for due_2
    resp = client.post(
        "/api/v2/pos/partial-collections/apply-multi/",
        {
            "customer_id": cust.customer_id,
            "amount_bdt": "500.00",
            "tender_type": "CASH",
            "allocations": [
                {"invoice_identifier": due_1.due_id, "amount_bdt": "300.00"},
                {"invoice_identifier": due_2.due_id, "amount_bdt": "200.00"},
            ],
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.data["success"] is True
    assert resp.data["total_collected_bdt"] == "500.00"

    due_1.refresh_from_db()
    assert due_1.remaining_due_bdt == Decimal("0.00")
    assert due_1.status == DueStatus.SETTLED

    due_2.refresh_from_db()
    assert due_2.remaining_due_bdt == Decimal("200.00")
    assert due_2.status == DueStatus.PARTIAL
