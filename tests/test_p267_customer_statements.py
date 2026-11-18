"""
backend/tests/test_p267_customer_statements.py
Architecture Decision: ADR-0267 (P267).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Customer Statements:
- Scenario 1: Valid statement generation (Opening Balance + Charges - Payments == Closing Balance).
- Scenario 2: 4-Tier Due Aging Engine (Current, 31-60d, 61-90d, 91d+) parity verification.
- Scenario 3: Input validation (missing customer, invalid date range, start_date > end_date).
- Scenario 4: Cross-tenant isolation (Tenant B cannot access Tenant A's statement).
- Scenario 5: Printable format rendering (HTML layout and text slip).
- Scenario 6: Statement reprint logging (print_count increments, last_printed_at updates).
"""

import datetime
from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerDueRecord,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    CustomerStatementAudit,
    DueStatus,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def statement_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p267-a-{uid}",
        name="Lazz Pharma Dhanmondi",
        subdomain=f"p267a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p267-a-{uid}",
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

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    cust_a = PosCustomer.objects.create(
        tenant=ten_a,
        primary_branch=br_a,
        full_name="Al-Haj Rafiqul Islam",
        phone=f"+88018{random.randint(10000000, 99999999)}",
        credit_limit_bdt=Decimal("20000.00"),
        current_due_bdt=Decimal("1500.00"),
        is_credit_approved=True,
        credit_status=PosCustomerCreditStatus.APPROVED,
    )

    # Post initial ledger entries for period calculation:
    # 1. Prior entry (45 days ago): 500 BDT Due Sale (Opening balance component)
    past_45d = timezone.now() - datetime.timedelta(days=45)
    CustomerReceivableLedgerEntry.objects.create(
        tenant=ten_a,
        customer=cust_a,
        branch=br_a,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="INV-OLD-01",
        debit_amount_bdt=Decimal("500.00"),
        credit_amount_bdt=Decimal("0.00"),
        balance_after_bdt=Decimal("500.00"),
        posted_at=past_45d,
        posted_by=cashier_a,
    )

    # 2. In-period Charge (10 days ago): 1200 BDT Due Sale
    past_10d = timezone.now() - datetime.timedelta(days=10)
    CustomerReceivableLedgerEntry.objects.create(
        tenant=ten_a,
        customer=cust_a,
        branch=br_a,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="INV-NEW-02",
        debit_amount_bdt=Decimal("1200.00"),
        credit_amount_bdt=Decimal("0.00"),
        balance_after_bdt=Decimal("1700.00"),
        posted_at=past_10d,
        posted_by=cashier_a,
    )

    # 3. In-period Payment (5 days ago): 200 BDT Repayment
    past_5d = timezone.now() - datetime.timedelta(days=5)
    CustomerReceivableLedgerEntry.objects.create(
        tenant=ten_a,
        customer=cust_a,
        branch=br_a,
        entry_type=CustomerReceivableEntryType.PAYMENT_COLLECTION,
        reference_number="COL-001",
        debit_amount_bdt=Decimal("0.00"),
        credit_amount_bdt=Decimal("200.00"),
        balance_after_bdt=Decimal("1500.00"),
        posted_at=past_5d,
        posted_by=cashier_a,
    )

    # Setup due records for Aging test
    today = timezone.now().date()
    # Bucket 1: Current (due in 5 days) -> 500 BDT
    CustomerDueRecord.objects.create(
        due_id=f"due-aging-cur-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust_a,
        customer_name=cust_a.full_name,
        customer_phone=cust_a.phone,
        total_due_bdt=Decimal("500.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("500.00"),
        status=DueStatus.OUTSTANDING,
        due_date=today + datetime.timedelta(days=5),
    )

    # Bucket 2: 31-60d overdue (overdue 40 days) -> 400 BDT
    CustomerDueRecord.objects.create(
        due_id=f"due-aging-40d-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust_a,
        customer_name=cust_a.full_name,
        customer_phone=cust_a.phone,
        total_due_bdt=Decimal("400.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("400.00"),
        status=DueStatus.OUTSTANDING,
        due_date=today - datetime.timedelta(days=40),
    )

    # Bucket 3: 61-90d overdue (overdue 70 days) -> 300 BDT
    CustomerDueRecord.objects.create(
        due_id=f"due-aging-70d-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust_a,
        customer_name=cust_a.full_name,
        customer_phone=cust_a.phone,
        total_due_bdt=Decimal("300.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("300.00"),
        status=DueStatus.OUTSTANDING,
        due_date=today - datetime.timedelta(days=70),
    )

    # Bucket 4: 91+d overdue (overdue 100 days) -> 300 BDT
    CustomerDueRecord.objects.create(
        due_id=f"due-aging-100d-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust_a,
        customer_name=cust_a.full_name,
        customer_phone=cust_a.phone,
        total_due_bdt=Decimal("300.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("300.00"),
        status=DueStatus.OUTSTANDING,
        due_date=today - datetime.timedelta(days=100),
    )
    # Total aging sum = 500 + 400 + 300 + 300 = 1500.00 == cust_a.current_due_bdt!

    # Tenant B (for cross-tenant tests)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p267-b-{uid}",
        name="Tamanna Pharmacy Uttara",
        subdomain=f"p267b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    cashier_b = PharmacyUser.objects.create_user(
        email=f"cashier_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.CASHIER,
    )

    return {
        "ten_a": ten_a,
        "br_a": br_a,
        "cashier_a": cashier_a,
        "owner_a": owner_a,
        "cust_a": cust_a,
        "ten_b": ten_b,
        "cashier_b": cashier_b,
    }


@pytest.mark.django_db
def test_p267_valid_statement_generation_and_parity(statement_env):
    """
    Scenario 1: Generate statement for past 30 days.
    Verify:
    - Opening balance = 500.00 (from 45 days ago)
    - Total charges = 1200.00 (from 10 days ago)
    - Total payments = 200.00 (from 5 days ago)
    - Closing balance = 500 + 1200 - 200 = 1500.00
    - is_parity_reconciled is True
    """
    client = APIClient()
    client.force_authenticate(user=statement_env["cashier_a"])

    cust = statement_env["cust_a"]
    today = timezone.now().date()
    start_30d = (today - datetime.timedelta(days=30)).isoformat()

    resp = client.get(
        f"/api/v2/pos/statements/generate/?customer_id={cust.customer_id}&start_date={start_30d}&end_date={today.isoformat()}"
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.data

    assert data["customer"]["customer_id"] == cust.customer_id
    summary = data["summary"]
    assert summary["opening_balance_bdt"] == "500.00"
    assert summary["total_charges_bdt"] == "1200.00"
    assert summary["total_payments_bdt"] == "200.00"
    assert summary["closing_balance_bdt"] == "1500.00"
    assert summary["is_parity_reconciled"] is True
    assert data["transactions_count"] == 2

    # Verify persistent audit log was saved
    audit = CustomerStatementAudit.objects.filter(statement_id=data["statement_id"]).first()
    assert audit is not None
    assert audit.opening_balance_bdt == Decimal("500.00")
    assert audit.closing_balance_bdt == Decimal("1500.00")
    assert audit.print_count == 1


@pytest.mark.django_db
def test_p267_due_aging_engine_parity(statement_env):
    """
    Scenario 2: Source-specific business rule: Verify 4-tier due aging engine
    Current (500), 31-60d (400), 61-90d (300), 91+d (300).
    Total aging sum = 1500.00.
    """
    client = APIClient()
    client.force_authenticate(user=statement_env["cashier_a"])

    cust = statement_env["cust_a"]
    resp = client.get(f"/api/v2/pos/statements/generate/?customer_id={cust.customer_id}")
    assert resp.status_code == status.HTTP_200_OK

    aging = resp.data["aging"]
    assert aging["current_bdt"] == "500.00"
    assert aging["days_31_60_bdt"] == "400.00"
    assert aging["days_61_90_bdt"] == "300.00"
    assert aging["days_91_plus_bdt"] == "300.00"
    assert aging["total_aging_bdt"] == "1500.00"
    assert Decimal(aging["total_aging_bdt"]) == cust.current_due_bdt


@pytest.mark.django_db
def test_p267_input_validation(statement_env):
    """
    Scenario 3: Input validation for missing customer, invalid dates, and inverted date range.
    """
    client = APIClient()
    client.force_authenticate(user=statement_env["cashier_a"])
    cust = statement_env["cust_a"]

    # 1. Missing customer_id
    r1 = client.get("/api/v2/pos/statements/generate/")
    assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_CUSTOMER_REQUIRED" in str(r1.data)

    # 2. Non-existent customer_id
    r2 = client.get("/api/v2/pos/statements/generate/?customer_id=non-existent-cust-99")
    assert r2.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST)

    # 3. Inverted date range (start > end)
    r3 = client.get(
        f"/api/v2/pos/statements/generate/?customer_id={cust.customer_id}&start_date=2026-03-01&end_date=2026-01-01"
    )
    assert r3.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_INVALID_DATE_RANGE" in str(r3.data) or "cannot be later" in str(r3.data)


@pytest.mark.django_db
def test_p267_cross_tenant_isolation(statement_env):
    """
    Scenario 4: Tenant B cashier cannot generate statement for Tenant A customer.
    """
    client = APIClient()
    client.force_authenticate(user=statement_env["cashier_b"])
    cust_a = statement_env["cust_a"]

    resp = client.get(f"/api/v2/pos/statements/generate/?customer_id={cust_a.customer_id}")
    assert resp.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
def test_p267_printable_formats_and_customer_action(statement_env):
    """
    Scenario 5: Test format negotiation (HTML and plain text slip) and PosCustomerViewSet action.
    """
    client = APIClient()
    client.force_authenticate(user=statement_env["cashier_a"])
    cust = statement_env["cust_a"]

    # 1. Request HTML format directly from PosCustomerViewSet action
    resp_html = client.get(f"/api/v2/pos/customers/{cust.customer_id}/statement/?format=html")
    if resp_html.status_code != 200:
        print("FAIL RESP_HTML:", resp_html.status_code, getattr(resp_html, "data", resp_html.content))
    assert resp_html.status_code == status.HTTP_200_OK
    assert "text/html" in resp_html["Content-Type"]
    html_content = resp_html.content.decode("utf-8")
    assert "STATEMENT OF ACCOUNT" in html_content
    assert cust.full_name in html_content
    assert "Due Aging Summary" in html_content

    # 2. Request Text slip format
    resp_txt = client.get(f"/api/v2/pos/customers/{cust.customer_id}/statement/?format=text")
    assert resp_txt.status_code == status.HTTP_200_OK
    assert "text/plain" in resp_txt["Content-Type"]
    txt_content = resp_txt.content.decode("utf-8")
    assert "DUE AGING" in txt_content


@pytest.mark.django_db
def test_p267_statement_print_logging_and_reprint(statement_env):
    """
    Scenario 6: Test statement print logging:
    - Initial print sets print_count = 1
    - Calling print endpoint increments print_count = 2 (is_reprint = True).
    """
    client = APIClient()
    client.force_authenticate(user=statement_env["cashier_a"])
    cust = statement_env["cust_a"]

    # Generate statement
    gen_resp = client.post(
        "/api/v2/pos/statements/generate/",
        {"customer_id": cust.customer_id},
        format="json",
    )
    assert gen_resp.status_code == status.HTTP_200_OK
    stmt_id = gen_resp.data["statement_id"]

    audit = CustomerStatementAudit.objects.get(statement_id=stmt_id)
    assert audit.print_count == 1

    # Record reprint
    print_resp = client.post(f"/api/v2/pos/statements/{stmt_id}/print/")
    assert print_resp.status_code == status.HTTP_200_OK
    assert print_resp.data["print_count"] == 2
    assert print_resp.data["is_reprint"] is True

    audit.refresh_from_db()
    assert audit.print_count == 2
