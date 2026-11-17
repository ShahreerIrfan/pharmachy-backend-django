"""
Acceptance Tests for P237 - Daily Sales Close, Drawer Cash, and Ledger Reconciliation.
Tests cover:
1. Valid scenario: Daily summary aggregation, cash submission, manager approval & SHA-256 sealing.
2. Incomplete or invalid input: Negative counted drawer cash rejected.
3. Unauthorized access: Cashier cannot approve daily sales close (403 PermissionDenied).
4. Cross-tenant isolation: Tenant B manager cannot access or approve Tenant A's daily close.
5. Unclosed shifts guard: Approval blocked if open cashier shifts exist for the day.
6. Immutability guard: Approved and sealed daily close cannot be modified or deleted.
"""

from datetime import date
from decimal import Decimal
import uuid
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    PaymentMethod,
    PosCashierShift,
    PosDailyCloseStatus,
    PosDailySalesClose,
    PosPaymentAttempt,
    PosPaymentAttemptStatus,
    PosPaymentJournalEntry,
    PosPaymentJournalEntryType,
    PosRefundMethod,
    PosReturnStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleReturn,
    PosShiftMovementType,
    PosShiftStatus,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def daily_close_env(db):
    """
    Sets up two isolated tenants, branches, users (cashier & manager),
    finalized sales orders, captured payments, refunds, and shifts.
    """
    from django.utils import timezone
    uid = uuid.uuid4().hex[:6]
    now_ts = timezone.now()
    today = now_ts.date()

    # Tenant A
    ten_a = Tenant.objects.create(tenant_id=f"ten-p237-a-{uid}", name="Tenant P237 A", subdomain=f"p237a-{uid}")
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p237-a-{uid}",
        name="Dhanmondi Branch",
        branch_code="DH01",
    )
    import random
    p_cashier = f"+88017{random.randint(10000000, 99999999)}"
    p_manager_a = f"+88018{random.randint(10000000, 99999999)}"
    p_manager_b = f"+88019{random.randint(10000000, 99999999)}"

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_a_{uid}@test.com",
        phone_number=p_cashier,
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )
    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_a_{uid}@test.com",
        phone_number=p_manager_a,
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(tenant_id=f"ten-p237-b-{uid}", name="Tenant P237 B", subdomain=f"p237b-{uid}")
    br_b = Branch.objects.create(
        tenant=ten_b,
        branch_id=f"br-p237-b-{uid}",
        name="Gulshan Branch",
        branch_code="GL01",
    )
    manager_b = PharmacyUser.objects.create_user(
        email=f"manager_b_{uid}@test.com",
        phone_number=p_manager_b,
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    from django.utils import timezone
    now_ts = timezone.now()

    # Closed Shift for Tenant A
    shift = PosCashierShift.objects.create(
        shift_id=f"shf-p237-{uid}",
        shift_number=f"SHF-DH01-20261116-{uid[:4].upper()}",
        tenant=ten_a,
        branch=br_a,
        cashier_user=cashier_a,
        status=PosShiftStatus.CLOSED,
        opening_float_bdt=Decimal("2000.00"),
        closing_cash_counted_bdt=Decimal("5000.00"),
        opened_at=now_ts,
        closed_at=now_ts,
    )

    # Finalized Sale 1: Cash sale 3000 BDT
    sale1 = PosSaleOrder.objects.create(
        sale_id=f"sale-p237-01-{uid}",
        tenant=ten_a,
        branch=br_a,
        cashier_user=cashier_a,
        subtotal_bdt=Decimal("3000.00"),
        grand_total_bdt=Decimal("3000.00"),
        paid_amount_bdt=Decimal("3000.00"),
        lifecycle_state=PosSaleLifecycleState.FINALIZED,
        created_at=now_ts,
    )
    PosPaymentAttempt.objects.create(
        attempt_id=f"att-p237-01-{uid}",
        tenant=ten_a,
        branch=br_a,
        sale=sale1,
        attempt_number=1,
        tender_type=PaymentMethod.CASH,
        amount_bdt=Decimal("3000.00"),
        status=PosPaymentAttemptStatus.CAPTURED,
    )
    PosPaymentJournalEntry.objects.create(
        journal_id=f"jrn-p237-01-{uid}",
        tenant=ten_a,
        branch=br_a,
        sale=sale1,
        entry_type=PosPaymentJournalEntryType.SALE_SETTLEMENT,
        tender_type=PaymentMethod.CASH,
        debit_account="CASH_DRAWER",
        credit_account="SALES_REVENUE",
        amount_bdt=Decimal("3000.00"),
        posted_at=now_ts,
    )

    # Finalized Sale 2: Digital sale (bKash) 2000 BDT
    sale2 = PosSaleOrder.objects.create(
        sale_id=f"sale-p237-02-{uid}",
        tenant=ten_a,
        branch=br_a,
        cashier_user=cashier_a,
        subtotal_bdt=Decimal("2000.00"),
        grand_total_bdt=Decimal("2000.00"),
        paid_amount_bdt=Decimal("2000.00"),
        lifecycle_state=PosSaleLifecycleState.FINALIZED,
        created_at=now_ts,
    )
    PosPaymentAttempt.objects.create(
        attempt_id=f"att-p237-02-{uid}",
        tenant=ten_a,
        branch=br_a,
        sale=sale2,
        attempt_number=1,
        tender_type=PaymentMethod.BKASH,
        amount_bdt=Decimal("2000.00"),
        status=PosPaymentAttemptStatus.CAPTURED,
    )
    PosPaymentJournalEntry.objects.create(
        journal_id=f"jrn-p237-02-{uid}",
        tenant=ten_a,
        branch=br_a,
        sale=sale2,
        entry_type=PosPaymentJournalEntryType.SALE_SETTLEMENT,
        tender_type=PaymentMethod.BKASH,
        debit_account="BKASH_SETTLEMENT",
        credit_account="SALES_REVENUE",
        amount_bdt=Decimal("2000.00"),
        posted_at=now_ts,
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "cashier_a": cashier_a,
        "manager_a": manager_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "manager_b": manager_b,
        "shift": shift,
        "today": today,
    }


@pytest.mark.django_db
def test_p237_valid_daily_close_submission_and_approval(daily_close_env):
    """
    Scenario 1: Cashier compiles live daily summary, submits physical drawer cash count,
    and Branch Manager approves and seals the record with an immutable SHA-256 digest.
    """
    env = daily_close_env
    cashier = env["cashier_a"]
    manager = env["manager_a"]
    d_str = env["today"].strftime("%Y-%m-%d")

    client = APIClient()
    client.force_authenticate(user=cashier)

    # 1. Inspect live summary
    res_sum = client.get(f"/api/v2/pos/daily-close/summary/?date={d_str}")
    assert res_sum.status_code == status.HTTP_200_OK
    assert res_sum.data["sales_count"] == 2
    assert Decimal(res_sum.data["gross_sales_bdt"]) == Decimal("5000.00")
    assert Decimal(res_sum.data["cash_sales_bdt"]) == Decimal("3000.00")
    assert Decimal(res_sum.data["digital_sales_bdt"]) == Decimal("2000.00")
    # Expected drawer = opening 2000 + cash sales 3000 = 5000.00 BDT
    assert Decimal(res_sum.data["expected_drawer_cash_bdt"]) == Decimal("5000.00")
    assert res_sum.data["is_ledger_balanced"] is True

    # 2. Submit daily close
    submit_payload = {
        "closing_date": d_str,
        "actual_drawer_cash_bdt": "5000.00",
        "notes": "Cashier EOD physical count matches drawer expected cash.",
    }
    res_sub = client.post("/api/v2/pos/daily-close/submit/", data=submit_payload, format="json")
    assert res_sub.status_code == status.HTTP_201_CREATED
    close_id = res_sub.data["close_id"]
    assert res_sub.data["status"] == PosDailyCloseStatus.SUBMITTED
    assert Decimal(res_sub.data["cash_variance_bdt"]) == Decimal("0.00")

    # 3. Manager approves daily close
    client.force_authenticate(user=manager)
    approve_payload = {"manager_notes": "All counts verified against physical currency."}
    res_app = client.post(f"/api/v2/pos/daily-close/{close_id}/approve/", data=approve_payload, format="json")
    assert res_app.status_code == status.HTTP_200_OK
    assert res_app.data["status"] == PosDailyCloseStatus.APPROVED
    assert len(res_app.data["sha256_hash"]) == 64


@pytest.mark.django_db
def test_p237_rejects_negative_counted_cash(daily_close_env):
    """
    Scenario 2: Incomplete or invalid input - Negative actual cash count is rejected.
    """
    env = daily_close_env
    cashier = env["cashier_a"]
    d_str = env["today"].strftime("%Y-%m-%d")

    client = APIClient()
    client.force_authenticate(user=cashier)

    payload = {
        "closing_date": d_str,
        "actual_drawer_cash_bdt": "-500.00",
    }
    response = client.post("/api/v2/pos/daily-close/submit/", data=payload, format="json")
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p237_unauthorized_cashier_cannot_approve(daily_close_env):
    """
    Scenario 3: Cashier attempting to approve daily close is denied (403 PermissionDenied).
    """
    env = daily_close_env
    cashier = env["cashier_a"]
    d_str = env["today"].strftime("%Y-%m-%d")

    client = APIClient()
    client.force_authenticate(user=cashier)

    # Submit first
    res_sub = client.post(
        "/api/v2/pos/daily-close/submit/",
        data={"closing_date": d_str, "actual_drawer_cash_bdt": "5000.00"},
        format="json",
    )
    close_id = res_sub.data["close_id"]

    # Attempt approve as cashier
    res_app = client.post(f"/api/v2/pos/daily-close/{close_id}/approve/", data={}, format="json")
    assert res_app.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_p237_cross_tenant_isolation(daily_close_env):
    """
    Scenario 4: Tenant B manager cannot access or approve Tenant A's daily sales close.
    """
    env = daily_close_env
    cashier_a = env["cashier_a"]
    manager_b = env["manager_b"]
    d_str = env["today"].strftime("%Y-%m-%d")

    client = APIClient()
    client.force_authenticate(user=cashier_a)

    res_sub = client.post(
        "/api/v2/pos/daily-close/submit/",
        data={"closing_date": d_str, "actual_drawer_cash_bdt": "5000.00"},
        format="json",
    )
    close_id = res_sub.data["close_id"]

    # Tenant B manager tries to access
    client.force_authenticate(user=manager_b)
    res_get = client.get(f"/api/v2/pos/daily-close/{close_id}/")
    assert res_get.status_code == status.HTTP_404_NOT_FOUND

    res_app = client.post(f"/api/v2/pos/daily-close/{close_id}/approve/", data={}, format="json")
    assert res_app.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
def test_p237_unclosed_shifts_blocks_approval(daily_close_env):
    """
    Scenario 5: If an active cashier shift is still OPEN, approving daily close is blocked.
    """
    env = daily_close_env
    cashier = env["cashier_a"]
    manager = env["manager_a"]
    d_str = env["today"].strftime("%Y-%m-%d")

    # Leave an open shift
    shift = env["shift"]
    shift.status = PosShiftStatus.OPEN
    shift.save()

    client = APIClient()
    client.force_authenticate(user=cashier)

    res_sub = client.post(
        "/api/v2/pos/daily-close/submit/",
        data={"closing_date": d_str, "actual_drawer_cash_bdt": "5000.00"},
        format="json",
    )
    close_id = res_sub.data["close_id"]

    # Manager tries to approve
    client.force_authenticate(user=manager)
    res_app = client.post(f"/api/v2/pos/daily-close/{close_id}/approve/", data={}, format="json")
    assert res_app.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "unclosed_shifts" in str(res_app.data)


@pytest.mark.django_db
def test_p237_approved_daily_close_is_immutable(daily_close_env):
    """
    Scenario 6: Once APPROVED and sealed with SHA-256 digest, daily close cannot be deleted.
    """
    env = daily_close_env
    cashier = env["cashier_a"]
    manager = env["manager_a"]
    d_str = env["today"].strftime("%Y-%m-%d")

    client = APIClient()
    client.force_authenticate(user=cashier)

    res_sub = client.post(
        "/api/v2/pos/daily-close/submit/",
        data={"closing_date": d_str, "actual_drawer_cash_bdt": "5000.00"},
        format="json",
    )
    close_id = res_sub.data["close_id"]

    client.force_authenticate(user=manager)
    res_app = client.post(f"/api/v2/pos/daily-close/{close_id}/approve/", data={}, format="json")
    assert res_app.status_code == status.HTTP_200_OK

    # Try deleting the approved record
    res_del = client.delete(f"/api/v2/pos/daily-close/{close_id}/")
    assert res_del.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_405_METHOD_NOT_ALLOWED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
