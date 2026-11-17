"""
Architecture Decision: ADR-0233 (P233).
Targeted Automated Test Suite for POS Cashier Shift Open-Close, Cash Drawer Payouts,
Physical Cash Counting, and Manager Variance Reconciliation.
"""

from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PlanTier,
    PosCashierShift,
    PosShiftCashMovement,
    PosShiftMovementType,
    PosShiftStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.pos_shift_service import PosShiftService


@pytest.fixture
def shift_env(db):
    tenant_a = Tenant.objects.create(
        tenant_id="ten-p233-a",
        name="Shift Pharmacy A",
        subdomain="p233a",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_a = Branch.objects.create(
        branch_id="br-p233-dhk",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        branch_code="DHK01",
    )
    cashier_a = PharmacyUser.objects.create_user(
        email="cashier_p233a@test.com",
        phone_number="+8801733334455",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Cashier Shift A",
    )
    manager_a = PharmacyUser.objects.create_user(
        email="manager_p233a@test.com",
        phone_number="+8801733334466",
        password="PassWord123!",
        role=UserRole.BRANCH_MANAGER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Manager Shift A",
    )

    tenant_b = Tenant.objects.create(
        tenant_id="ten-p233-b",
        name="Shift Pharmacy B",
        subdomain="p233b",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_b = Branch.objects.create(
        branch_id="br-p233-ctg",
        tenant=tenant_b,
        name="Chittagong Branch",
        branch_code="CTG01",
    )
    cashier_b = PharmacyUser.objects.create_user(
        email="cashier_p233b@test.com",
        phone_number="+8801733334477",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        full_name="Cashier Shift B",
    )

    return {
        "tenant_a": tenant_a,
        "branch_a": branch_a,
        "cashier_a": cashier_a,
        "manager_a": manager_a,
        "tenant_b": tenant_b,
        "branch_b": branch_b,
        "cashier_b": cashier_b,
    }


@pytest.mark.django_db
def test_p233_scenario1_open_shift_and_active_check(shift_env):
    """
    Scenario 1: Open shift with opening float and check active shift.
    """
    env = shift_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    # 1. Open shift
    res_open = client.post(
        "/api/v2/pos/shifts/open/",
        {
            "branch_id": env["branch_a"].branch_id,
            "device_code": "REG01",
            "opening_float_bdt": "2000.00",
        },
        format="json",
    )
    assert res_open.status_code == status.HTTP_201_CREATED
    data = res_open.data
    assert data["status"] == PosShiftStatus.OPEN
    assert data["opening_float_bdt"] == "2000.00"
    assert data["expected_drawer_cash_bdt"] == "2000.00"
    assert data["device_code"] == "REG01"

    # 2. Check active shift endpoint
    res_active = client.get("/api/v2/pos/shifts/active/")
    assert res_active.status_code == status.HTTP_200_OK
    assert res_active.data["shift_id"] == data["shift_id"]
    assert res_active.data["status"] == PosShiftStatus.OPEN


@pytest.mark.django_db
def test_p233_scenario2_duplicate_open_shift_rejected(shift_env):
    """
    Scenario 2: Invariant: Cashier cannot open a second shift if one is already OPEN.
    """
    env = shift_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    # 1. Open first shift
    res1 = client.post(
        "/api/v2/pos/shifts/open/",
        {"device_code": "REG01", "opening_float_bdt": "1500.00"},
        format="json",
    )
    assert res1.status_code == status.HTTP_201_CREATED

    # 2. Open second shift while first is open -> Rejected
    res2 = client.post(
        "/api/v2/pos/shifts/open/",
        {"device_code": "REG01", "opening_float_bdt": "1000.00"},
        format="json",
    )
    assert res2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "shift" in res2.data or "error_code" in res2.data


@pytest.mark.django_db
def test_p233_scenario3_cash_movements_and_drawer_limits(shift_env):
    """
    Scenario 3: Mid-shift payouts, drawer cash sufficiency, and float additions.
    """
    env = shift_env
    shift = PosShiftService.open_shift(
        user=env["cashier_a"],
        device_code="REG01",
        opening_float_bdt="2000.00",
    )

    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    # 1. Payout exceeding drawer (3000 BDT > 2000 BDT) -> Rejected
    res_excess = client.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/cash-movement/",
        {
            "movement_type": PosShiftMovementType.PAYOUT_EXPENSE,
            "amount_bdt": "3000.00",
            "reason": "Large supplier cash pay",
        },
        format="json",
    )
    assert res_excess.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Valid payout of 500 BDT -> Accepted, expected drawer becomes 1500.00
    res_valid = client.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/cash-movement/",
        {
            "movement_type": PosShiftMovementType.PAYOUT_EXPENSE,
            "amount_bdt": "500.00",
            "reason": "Emergency courier payment",
            "recipient_or_source": "Pathao Express",
        },
        format="json",
    )
    assert res_valid.status_code == status.HTTP_201_CREATED
    shift.refresh_from_db()
    assert shift.total_payouts_bdt == Decimal("500.00")
    assert shift.expected_drawer_cash_bdt == Decimal("1500.00")

    # 3. Add float of 1000 BDT -> expected drawer becomes 2500.00
    res_add = client.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/cash-movement/",
        {
            "movement_type": PosShiftMovementType.ADD_FLOAT,
            "amount_bdt": "1000.00",
            "reason": "Additional 100 BDT change notes from manager",
        },
        format="json",
    )
    assert res_add.status_code == status.HTTP_201_CREATED
    shift.refresh_from_db()
    assert shift.total_cash_in_bdt == Decimal("1000.00")
    assert shift.expected_drawer_cash_bdt == Decimal("2500.00")


@pytest.mark.django_db
def test_p233_scenario4_close_with_variance_and_manager_approval(shift_env):
    """
    Scenario 4: Close shift with cash shortage:
    - Missing reason is rejected.
    - With reason enters PENDING_MANAGER_APPROVAL.
    - Cashier cannot approve their own variance (Separation of Duties).
    - Manager approves variance -> RECONCILED.
    """
    env = shift_env
    shift = PosShiftService.open_shift(
        user=env["cashier_a"],
        device_code="REG01",
        opening_float_bdt="2000.00",
    )

    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])

    # 1. Close with shortage (-50 BDT) WITHOUT reason -> Rejected
    res_no_reason = client_cashier.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/close/",
        {"closing_cash_counted_bdt": "1950.00"},
        format="json",
    )
    assert res_no_reason.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Close with shortage WITH reason -> Enters PENDING_MANAGER_APPROVAL
    res_close = client_cashier.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/close/",
        {
            "closing_cash_counted_bdt": "1950.00",
            "variance_reason": "Missing 50 BDT note, cashier reconciliation discrepancy",
        },
        format="json",
    )
    assert res_close.status_code == status.HTTP_200_OK
    data = res_close.data
    assert data["status"] == PosShiftStatus.PENDING_MANAGER_APPROVAL
    assert data["variance_bdt"] == "-50.00"
    assert data["requires_manager_approval"] is True

    # 3. Cashier attempts to approve their own variance -> Forbidden (Separation of duties)
    res_self_app = client_cashier.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/approve-variance/",
        {"manager_approval_notes": "Self approval attempt"},
        format="json",
    )
    assert res_self_app.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 4. Manager approves variance -> RECONCILED
    client_mgr = APIClient()
    client_mgr.force_authenticate(user=env["manager_a"])
    res_mgr_app = client_mgr.post(
        f"/api/v2/pos/shifts/{shift.shift_id}/approve-variance/",
        {"manager_approval_notes": "Shortage audited and approved as minor cashier variance"},
        format="json",
    )
    assert res_mgr_app.status_code == status.HTTP_200_OK
    assert res_mgr_app.data["status"] == PosShiftStatus.RECONCILED
    assert res_mgr_app.data["approved_by_email"] == env["manager_a"].email


@pytest.mark.django_db
def test_p233_scenario5_cross_tenant_isolation(shift_env):
    """
    Scenario 5: Cashier B (Tenant B) cannot view, close, or add movements to Tenant A's shift.
    """
    env = shift_env
    shift_a = PosShiftService.open_shift(
        user=env["cashier_a"],
        device_code="REG01",
        opening_float_bdt="2000.00",
    )

    client_b = APIClient()
    client_b.force_authenticate(user=env["cashier_b"])

    # Attempt to retrieve Tenant A shift
    res_get = client_b.get(f"/api/v2/pos/shifts/{shift_a.shift_id}/")
    assert res_get.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)

    # Attempt to close Tenant A shift
    res_close = client_b.post(
        f"/api/v2/pos/shifts/{shift_a.shift_id}/close/",
        {"closing_cash_counted_bdt": "2000.00"},
        format="json",
    )
    assert res_close.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN)
