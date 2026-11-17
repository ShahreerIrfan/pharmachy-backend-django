"""
Architecture Decision: ADR-0236 (P236).
Targeted Automated Test Suite for POS Manager Discount Approvals,
Price Override Restraints, Cost Floor Guards, and Offline Policy Boundaries.
"""

from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    PlanTier,
    PosCartPricingAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.pos_cart_pricing_service import PosCartPricingService


@pytest.fixture
def discount_env(db):
    tenant_a = Tenant.objects.create(
        tenant_id="ten-p236-a",
        name="Discount Pharmacy A",
        subdomain="p236a",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_a = Branch.objects.create(
        branch_id="br-p236-dhk",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        branch_code="DHK01",
    )
    cashier_a = PharmacyUser.objects.create_user(
        email="cashier_p236a@test.com",
        phone_number="+8801766667788",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Cashier P236 A",
    )
    manager_a = PharmacyUser.objects.create_user(
        email="manager_p236a@test.com",
        phone_number="+8801766667799",
        password="PassWord123!",
        role=UserRole.BRANCH_MANAGER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Manager P236 A",
    )

    tenant_b = Tenant.objects.create(
        tenant_id="ten-p236-b",
        name="Discount Pharmacy B",
        subdomain="p236b",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_b = Branch.objects.create(
        branch_id="br-p236-ctg",
        tenant=tenant_b,
        name="Chittagong Branch",
        branch_code="CTG01",
    )
    manager_b = PharmacyUser.objects.create_user(
        email="manager_p236b@test.com",
        phone_number="+8801766667700",
        password="PassWord123!",
        role=UserRole.BRANCH_MANAGER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        full_name="Manager P236 B",
    )

    product = MedicineProduct.objects.create(
        product_id="prod-p236-cefix",
        brand_name="Cef-3 200mg",
        generic_name="Cefixime",
        dosage_form="Capsule",
        strength="200mg",
        is_antibiotic=True,
        is_narcotic=False,
        is_prescription_only=True,
        mrp_unit=Decimal("40.00"),
        cost_unit=Decimal("30.00"),
    )

    batch_a = BatchStock.objects.create(
        batch_id="batch-p236-01",
        tenant=tenant_a,
        branch=branch_a,
        product=product,
        batch_number="BX-P236-01",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        cost_per_unit=Decimal("30.00"),
        quantity_base_units=100,
    )

    # Draft sale of 10 capsules @ 40.00 = 400.00 BDT
    sale_a = PosSaleOrder.objects.create(
        sale_id="sale-p236-001",
        order_number="POS-202611-DISC01",
        tenant=tenant_a,
        branch=branch_a,
        cashier_user=cashier_a,
        customer_name="Kamrul Hasan",
        lifecycle_state=PosSaleLifecycleState.DRAFT,
        subtotal_bdt=Decimal("400.00"),
        grand_total_bdt=Decimal("400.00"),
    )
    line_a = PosSaleOrderLine.objects.create(
        line_id="line-p236-001",
        tenant=tenant_a,
        branch=branch_a,
        sale=sale_a,
        line_number=1,
        product=product,
        packaging_unit="CAPSULE",
        conversion_factor=1,
        quantity_units=10,
        base_units_total=10,
        unit_price_bdt=Decimal("40.00"),
        unit_cost_bdt=Decimal("30.00"),
        line_subtotal_bdt=Decimal("400.00"),
        line_total_bdt=Decimal("400.00"),
    )

    return {
        "tenant_a": tenant_a,
        "branch_a": branch_a,
        "cashier_a": cashier_a,
        "manager_a": manager_a,
        "tenant_b": tenant_b,
        "branch_b": branch_b,
        "manager_b": manager_b,
        "product": product,
        "batch_a": batch_a,
        "sale_a": sale_a,
        "line_a": line_a,
    }


@pytest.mark.django_db
def test_p236_valid_manager_discount_approval(discount_env):
    """
    Scenario 1: Manager approves a restricted 15% discount (> 5% cashier limit)
    with a documented business reason. Verified: Discount applied, totals recomputed, audit logged.
    """
    env = discount_env
    sale = env["sale_a"]
    manager = env["manager_a"]

    client = APIClient()
    client.force_authenticate(user=manager)

    payload = {
        "discount_rate_pct": "15.00",
        "discount_reason": "Chronic illness patient regular discount",
    }
    response = client.post(f"/api/v2/pos/sales/{sale.sale_id}/approve-discount/", data=payload, format="json")
    assert response.status_code == status.HTTP_200_OK

    sale.refresh_from_db()
    assert sale.discount_approved_by == manager
    assert sale.discount_rate_pct == Decimal("15.00")
    # 400 - 15% (60.00) = 340.00 BDT
    assert sale.grand_total_bdt == Decimal("340.00")

    audit = PosCartPricingAudit.objects.filter(sale=sale, action="DISCOUNT_APPROVED").first()
    assert audit is not None
    assert "Chronic illness" in sale.discount_reason


@pytest.mark.django_db
def test_p236_unauthorized_cashier_exceeding_discount_limit(discount_env):
    """
    Scenario 2: Cashier attempting to approve or apply 15% discount without manager approval
    is blocked with PermissionDenied.
    """
    env = discount_env
    sale = env["sale_a"]
    cashier = env["cashier_a"]

    client = APIClient()
    client.force_authenticate(user=cashier)

    # Cashier calling approve-discount without manager PIN is rejected
    payload = {
        "discount_rate_pct": "15.00",
        "discount_reason": "Cashier unauthorized discount attempt",
    }
    response = client.post(f"/api/v2/pos/sales/{sale.sale_id}/approve-discount/", data=payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_p236_rejection_of_missing_reason(discount_env):
    """
    Scenario 3: Discount approval without documented reason (< 3 chars) is rejected.
    """
    env = discount_env
    sale = env["sale_a"]
    manager = env["manager_a"]

    client = APIClient()
    client.force_authenticate(user=manager)

    payload = {
        "discount_rate_pct": "10.00",
        "discount_reason": "ok",  # too short
    }
    response = client.post(f"/api/v2/pos/sales/{sale.sale_id}/approve-discount/", data=payload, format="json")
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p236_cost_floor_guardrail(discount_env):
    """
    Scenario 4: Overriding unit price below purchase cost price is blocked
    to prevent rogue dispensing below wholesale cost.
    """
    env = discount_env
    sale = env["sale_a"]
    line = env["line_a"]
    manager = env["manager_a"]

    client = APIClient()
    client.force_authenticate(user=manager)

    # Cost is 30.00 BDT. Attempt to override to 25.00 BDT without allow_below_cost
    payload = {
        "discount_reason": "Clearance discount attempt below cost",
        "line_overrides": [
            {
                "line_id": line.line_id,
                "unit_price_bdt": "25.00",
            }
        ],
    }
    response = client.post(f"/api/v2/pos/sales/{sale.sale_id}/approve-discount/", data=payload, format="json")
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p236_offline_discount_ceiling_boundary(discount_env):
    """
    Scenario 5: In offline mode, discounts exceeding 20% ceiling are prohibited
    even with local manager override, requiring cloud connectivity.
    """
    env = discount_env
    sale = env["sale_a"]
    manager = env["manager_a"]

    client = APIClient()
    client.force_authenticate(user=manager)

    payload = {
        "discount_rate_pct": "25.00",  # exceeds 20% offline ceiling
        "discount_reason": "Offline emergency override",
        "is_offline": True,
    }
    response = client.post(f"/api/v2/pos/sales/{sale.sale_id}/approve-discount/", data=payload, format="json")
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p236_cross_tenant_isolation(discount_env):
    """
    Scenario 6: Manager from Tenant B cannot approve discounts on Tenant A's sales.
    """
    env = discount_env
    sale_a = env["sale_a"]
    manager_b = env["manager_b"]

    client = APIClient()
    client.force_authenticate(user=manager_b)

    payload = {
        "discount_rate_pct": "10.00",
        "discount_reason": "Cross-tenant unauthorized approval attempt",
    }
    response = client.post(f"/api/v2/pos/sales/{sale_a.sale_id}/approve-discount/", data=payload, format="json")
    assert response.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
