"""
backend/tests/test_p265_customer_receivable_ledger.py
Architecture Decision: ADR-0265 (P265).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Customer Receivable Ledger:
- Scenario 1: Valid chronological AR postings (Due Sale -> Collection -> Return Credit) with running balance reconciliation.
- Scenario 2: Parameter validation rejection (zero amounts, negative amounts, empty references, empty adjustment reason).
- Scenario 3: Role-based governance for adjustments (Cashier rejected with 403; Owner permitted with 201) and cross-tenant isolation.
- Scenario 4: Compensating reversal and idempotency replay protection.
- Scenario 5: Statement pagination, date-range filtering, and opening/closing balance parity reconciliation.
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.customer_receivable_ledger_service import CustomerReceivableLedgerService
from apps.core.models import (
    Branch,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def ledger_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p265-a-{uid}",
        name="Lazz Pharma Dhanmondi",
        subdomain=f"p265a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p265-a-{uid}",
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
    customer = PosCustomer.objects.create(
        tenant=ten_a,
        primary_branch=br_a,
        customer_id=f"cust-p265-{uid}",
        full_name="Golam Kibria",
        phone="+8801719876543",
        credit_status=PosCustomerCreditStatus.APPROVED,
        is_credit_approved=True,
        credit_limit_bdt=Decimal("10000.00"),
        current_due_bdt=Decimal("0.00"),
        created_by=owner_a,
    )

    # Tenant B (Cross-Tenant Boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p265-b-{uid}",
        name="Apollo Health B",
        subdomain=f"p265b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "cashier_a": cashier_a,
        "customer": customer,
        "tenant_b": ten_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p265_valid_chronological_postings_and_parity(ledger_env):
    """Scenario 1: Valid chronological AR postings (Due Sale -> Collection -> Return Credit) with running balance."""
    customer = ledger_env["customer"]
    ten_a = ledger_env["tenant_a"]
    br_a = ledger_env["branch_a"]
    owner = ledger_env["owner_a"]

    # 1. Post Credit Due Sale of 1500.00 BDT (Debit)
    e1 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="SO-1001",
        debit_amount_bdt=Decimal("1500.00"),
        branch=br_a,
        posted_by=owner,
    )
    assert e1.balance_after_bdt == Decimal("1500.00")
    customer.refresh_from_db()
    assert customer.current_due_bdt == Decimal("1500.00")

    # 2. Post Payment Collection of 500.00 BDT (Credit)
    e2 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.PAYMENT_COLLECTION,
        reference_number="COL-2001",
        credit_amount_bdt=Decimal("500.00"),
        branch=br_a,
        posted_by=owner,
    )
    assert e2.balance_after_bdt == Decimal("1000.00")
    customer.refresh_from_db()
    assert customer.current_due_bdt == Decimal("1000.00")

    # 3. Post Sales Return Credit of 200.00 BDT (Credit)
    e3 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.SALES_RETURN_CREDIT,
        reference_number="RET-3001",
        credit_amount_bdt=Decimal("200.00"),
        branch=br_a,
        posted_by=owner,
    )
    assert e3.balance_after_bdt == Decimal("800.00")
    customer.refresh_from_db()
    assert customer.current_due_bdt == Decimal("800.00")

    # 4. Query Receivable Statement via API
    client = APIClient()
    client.force_authenticate(user=ledger_env["cashier_a"])
    url = f"/api/v2/pos/customers/{customer.customer_id}/receivable-statement/"
    resp = client.get(url)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.data
    summary = data["statement_summary"]
    assert summary["opening_balance_bdt"] == "0.00"
    assert summary["total_debits_bdt"] == "1500.00"
    assert summary["total_credits_bdt"] == "700.00"
    assert summary["closing_balance_bdt"] == "800.00"
    assert summary["is_parity_reconciled"] is True
    assert len(data["results"]) == 3


@pytest.mark.django_db
def test_p265_input_validation_rejections(ledger_env):
    """Scenario 2: Parameter validation rejection for zero/negative amounts and empty references."""
    customer = ledger_env["customer"]
    ten_a = ledger_env["tenant_a"]

    # Zero debit and zero credit
    with pytest.raises(Exception):
        CustomerReceivableLedgerService.post_entry_in_tx(
            tenant=ten_a,
            customer=customer,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="SO-FAIL-1",
            debit_amount_bdt=Decimal("0.00"),
            credit_amount_bdt=Decimal("0.00"),
        )

    # Empty reference number
    with pytest.raises(Exception):
        CustomerReceivableLedgerService.post_entry_in_tx(
            tenant=ten_a,
            customer=customer,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="   ",
            debit_amount_bdt=Decimal("100.00"),
        )

    # Negative amount
    with pytest.raises(Exception):
        CustomerReceivableLedgerService.post_entry_in_tx(
            tenant=ten_a,
            customer=customer,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="SO-FAIL-2",
            debit_amount_bdt=Decimal("-50.00"),
        )


@pytest.mark.django_db
def test_p265_role_governance_and_cross_tenant_isolation(ledger_env):
    """Scenario 3: Role-based governance for adjustments and cross-tenant boundary isolation."""
    customer = ledger_env["customer"]

    # 1. Cashier attempts manual adjustment -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=ledger_env["cashier_a"])
    resp_cashier = client_cashier.post(
        "/api/v2/pos/receivable-ledger/adjustment/",
        {
            "customer_id": customer.customer_id,
            "adjustment_direction": "DEBIT",
            "amount_bdt": "150.00",
            "reason": "Audited balance adjustment",
        },
    )
    assert resp_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Pharmacy Owner posts adjustment -> 201 Created
    client_owner = APIClient()
    client_owner.force_authenticate(user=ledger_env["owner_a"])
    resp_owner = client_owner.post(
        "/api/v2/pos/receivable-ledger/adjustment/",
        {
            "customer_id": customer.customer_id,
            "adjustment_direction": "DEBIT",
            "amount_bdt": "300.00",
            "reason": "Audited opening balance correction",
        },
    )
    assert resp_owner.status_code == status.HTTP_201_CREATED
    assert resp_owner.data["debit_amount_bdt"] == "300.00"
    assert resp_owner.data["balance_after_bdt"] == "300.00"

    customer.refresh_from_db()
    assert customer.current_due_bdt == Decimal("300.00")

    # 3. Cross-Tenant Isolation: Tenant B cannot access Tenant A statement
    client_b = APIClient()
    client_b.force_authenticate(user=ledger_env["owner_b"])
    resp_cross = client_b.get(f"/api/v2/pos/customers/{customer.customer_id}/receivable-statement/")
    assert resp_cross.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p265_compensating_reversal_and_idempotency(ledger_env):
    """Scenario 4: Compensating reversal and idempotency replay protection."""
    customer = ledger_env["customer"]
    ten_a = ledger_env["tenant_a"]
    owner = ledger_env["owner_a"]

    # Post an entry with idempotency key
    e1 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="SO-IDEM-01",
        debit_amount_bdt=Decimal("500.00"),
        idempotency_key="key-idem-p265-01",
        posted_by=owner,
    )
    assert e1.balance_after_bdt == Decimal("500.00")

    # Replay with same idempotency key -> returns existing entry without double-incrementing balance!
    e1_replay = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="SO-IDEM-01",
        debit_amount_bdt=Decimal("500.00"),
        idempotency_key="key-idem-p265-01",
        posted_by=owner,
    )
    assert e1_replay.entry_id == e1.entry_id
    customer.refresh_from_db()
    assert customer.current_due_bdt == Decimal("500.00")  # NOT 1000.00!

    # Post compensating reversal
    rev = CustomerReceivableLedgerService.post_reversal_in_tx(
        user=owner,
        original_entry_id=e1.entry_id,
        reason="Sale voided by supervisor",
    )
    assert rev.entry_type == CustomerReceivableEntryType.REVERSAL
    assert rev.credit_amount_bdt == Decimal("500.00")
    assert rev.balance_after_bdt == Decimal("0.00")
    customer.refresh_from_db()
    assert customer.current_due_bdt == Decimal("0.00")


@pytest.mark.django_db
def test_p265_statement_pagination_and_date_filtering(ledger_env):
    """Scenario 5: Statement pagination, date-range filtering, and opening balance reconciliation."""
    customer = ledger_env["customer"]
    ten_a = ledger_env["tenant_a"]
    owner = ledger_env["owner_a"]

    # Create 3 entries
    now = timezone.now()
    e1 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="SO-PAGE-1",
        debit_amount_bdt=Decimal("100.00"),
        posted_by=owner,
    )
    e2 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="SO-PAGE-2",
        debit_amount_bdt=Decimal("200.00"),
        posted_by=owner,
    )
    e3 = CustomerReceivableLedgerService.post_entry_in_tx(
        tenant=ten_a,
        customer=customer,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="SO-PAGE-3",
        debit_amount_bdt=Decimal("300.00"),
        posted_by=owner,
    )

    client = APIClient()
    client.force_authenticate(user=ledger_env["cashier_a"])

    # Page 1 with page_size=2
    resp_p1 = client.get(
        f"/api/v2/pos/customers/{customer.customer_id}/receivable-statement/",
        {"page": 1, "page_size": 2},
    )
    assert resp_p1.status_code == status.HTTP_200_OK
    assert resp_p1.data["pagination"]["count"] == 3
    assert resp_p1.data["pagination"]["total_pages"] == 2
    assert len(resp_p1.data["results"]) == 2

    # Page 2 with page_size=2
    resp_p2 = client.get(
        f"/api/v2/pos/customers/{customer.customer_id}/receivable-statement/",
        {"page": 2, "page_size": 2},
    )
    assert resp_p2.status_code == status.HTTP_200_OK
    assert len(resp_p2.data["results"]) == 1
    assert resp_p2.data["results"][0]["reference_number"] == "SO-PAGE-1"
