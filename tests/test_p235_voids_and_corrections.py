"""
Architecture Decision: ADR-0235 (P235).
Targeted Automated Test Suite for POS Sales Voids, Cart Corrections,
Prohibition of Deleting Posted Sales, and Audit Reversals.
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
    PaymentMethod,
    PlanTier,
    PosSaleLifecycleAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.pos_domain_service import PosDomainService


@pytest.fixture
def void_correction_env(db):
    tenant_a = Tenant.objects.create(
        tenant_id="ten-p235-a",
        name="Void Test Pharmacy A",
        subdomain="p235a",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_a = Branch.objects.create(
        branch_id="br-p235-dhk",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        branch_code="DHK01",
    )
    cashier_a = PharmacyUser.objects.create_user(
        email="cashier_p235a@test.com",
        phone_number="+8801755556677",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Cashier P235 A",
    )
    manager_a = PharmacyUser.objects.create_user(
        email="manager_p235a@test.com",
        phone_number="+8801755556688",
        password="PassWord123!",
        role=UserRole.BRANCH_MANAGER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Manager P235 A",
    )

    tenant_b = Tenant.objects.create(
        tenant_id="ten-p235-b",
        name="Void Test Pharmacy B",
        subdomain="p235b",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_b = Branch.objects.create(
        branch_id="br-p235-ctg",
        tenant=tenant_b,
        name="Chittagong Branch",
        branch_code="CTG01",
    )
    cashier_b = PharmacyUser.objects.create_user(
        email="cashier_p235b@test.com",
        phone_number="+8801755556699",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        full_name="Cashier P235 B",
    )

    product = MedicineProduct.objects.create(
        product_id="prod-p235-ace",
        brand_name="Ace Plus 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_antibiotic=False,
        is_narcotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("3.00"),
        cost_unit=Decimal("2.10"),
    )

    batch_a = BatchStock.objects.create(
        batch_id="batch-p235-01",
        tenant=tenant_a,
        branch=branch_a,
        product=product,
        batch_number="BX-P235-01",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        cost_per_unit=Decimal("2.10"),
        quantity_base_units=200,
    )

    # Draft sale for Tenant A
    draft_sale = PosSaleOrder.objects.create(
        sale_id="sale-p235-draft",
        order_number="POS-202611-DRAFT01",
        tenant=tenant_a,
        branch=branch_a,
        cashier_user=cashier_a,
        customer_name="Walk-in Customer",
        lifecycle_state=PosSaleLifecycleState.DRAFT,
        subtotal_bdt=Decimal("30.00"),
        grand_total_bdt=Decimal("30.00"),
    )
    line_draft = PosSaleOrderLine.objects.create(
        line_id="line-p235-01",
        tenant=tenant_a,
        branch=branch_a,
        sale=draft_sale,
        line_number=1,
        product=product,
        packaging_unit="TABLET",
        conversion_factor=1,
        quantity_units=10,
        base_units_total=10,
        unit_price_bdt=Decimal("3.00"),
        line_subtotal_bdt=Decimal("30.00"),
        line_total_bdt=Decimal("30.00"),
    )

    # Finalized sale for Tenant A
    finalized_sale, _ = PosDomainService.create_sale_order(
        cashier_a,
        {
            "branch_id": branch_a.branch_id,
            "offline_device_namespace": "",
            "customer_name": "Tariqul Islam",
            "lines": [
                {
                    "product": product.product_id,
                    "quantity_units": 5,
                    "unit_price_bdt": "3.00",
                    "batch_number": batch_a.batch_number,
                }
            ],
            "payments": [
                {
                    "payment_method": PaymentMethod.CASH,
                    "tender_type": "CASH",
                    "amount_bdt": "15.00",
                }
            ],
            "discount_amount_bdt": "0.00",
            "notes": "P235 Finalized Sale Test",
        },
    )
    finalized_sale.lifecycle_state = PosSaleLifecycleState.FINALIZED
    finalized_sale.save(update_fields=["lifecycle_state"])

    return {
        "tenant_a": tenant_a,
        "branch_a": branch_a,
        "cashier_a": cashier_a,
        "manager_a": manager_a,
        "tenant_b": tenant_b,
        "branch_b": branch_b,
        "cashier_b": cashier_b,
        "product": product,
        "batch_a": batch_a,
        "draft_sale": draft_sale,
        "line_draft": line_draft,
        "finalized_sale": finalized_sale,
    }


@pytest.mark.django_db
def test_p235_valid_pre_final_void_command(void_correction_env):
    """
    Scenario 1: Cashier executes an explicit void command on a pre-final sale with reason.
    Verified: State changes to VOIDED, is_archived=True, and lifecycle audit is recorded.
    """
    env = void_correction_env
    sale = env["draft_sale"]
    reason = "Customer left queue due to emergency"

    voided = PosDomainService.void_pre_final_sale(user=env["cashier_a"], sale=sale, reason=reason)

    assert voided.lifecycle_state == PosSaleLifecycleState.VOIDED
    assert voided.is_archived is True
    assert voided.void_reason == reason
    assert voided.voided_at is not None

    # Check append-only lifecycle audit trail
    audit = PosSaleLifecycleAudit.objects.filter(sale=voided, action="SALE_VOIDED").first()
    assert audit is not None
    assert audit.reason == reason
    assert audit.to_state == PosSaleLifecycleState.VOIDED


@pytest.mark.django_db
def test_p235_void_rejects_missing_reason(void_correction_env):
    """
    Scenario 2: Voids without a documented business reason are strictly rejected.
    """
    env = void_correction_env
    sale = env["draft_sale"]

    with pytest.raises(Exception) as exc_info:
        PosDomainService.void_pre_final_sale(user=env["cashier_a"], sale=sale, reason="   ")

    assert "reason" in str(exc_info.value).lower()


@pytest.mark.django_db
def test_p235_prohibit_deleting_posted_sale_records(void_correction_env):
    """
    Scenario 3: Physical deletion of posted or finalized sales is strictly prohibited.
    Both ORM delete() and DELETE API endpoint must reject the operation.
    """
    env = void_correction_env
    finalized = env["finalized_sale"]
    assert finalized.lifecycle_state == PosSaleLifecycleState.FINALIZED

    # 1. ORM delete() raises ValidationError
    with pytest.raises(ValidationError) as exc_info:
        finalized.delete()
    assert "cannot be deleted" in str(exc_info.value)

    # 2. REST DELETE request returns 405 MethodNotAllowed
    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])
    resp = client.delete(f"/api/v2/pos/sales/{finalized.sale_id}/")
    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    # 3. Explicit void endpoint on finalized sale returns 405 MethodNotAllowed
    void_resp = client.post(
        f"/api/v2/pos/sales/{finalized.sale_id}/void/",
        data={"reason": "Attempting to void finalized sale"},
        format="json",
    )
    assert void_resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


@pytest.mark.django_db
def test_p235_pre_final_cart_correction(void_correction_env):
    """
    Scenario 4: Audited item/cart correction command adjusts quantity safely
    and records CART_CORRECTION audit entry.
    """
    env = void_correction_env
    sale = env["draft_sale"]
    line = env["line_draft"]

    corrected = PosDomainService.correct_sale_order(
        user=env["cashier_a"],
        sale=sale,
        reason="Customer requested 5 tablets instead of 10",
        correction_type="CART_CORRECTION",
        line_id=line.line_id,
        quantity_units=5,
    )

    line.refresh_from_db()
    assert line.quantity_units == 5
    assert line.line_total_bdt == Decimal("15.00")
    assert corrected.grand_total_bdt == Decimal("15.00")

    audit = PosSaleLifecycleAudit.objects.filter(sale=corrected, action="CART_CORRECTION").first()
    assert audit is not None
    assert "5 tablets" in audit.reason


@pytest.mark.django_db
def test_p235_post_final_correction_creates_linked_order(void_correction_env):
    """
    Scenario 5: Post-final correction command reverses original finalized sale
    and creates a new linked replacement draft order.
    """
    env = void_correction_env
    finalized = env["finalized_sale"]
    manager = env["manager_a"]

    replacement = PosDomainService.correct_sale_order(
        user=manager,
        sale=finalized,
        reason="Incorrect customer entered by cashier",
        correction_type="POSTED_SALE_CORRECTION",
    )

    # Original sale is now reversed
    finalized.refresh_from_db()
    assert finalized.lifecycle_state == PosSaleLifecycleState.REVERSED

    # New replacement order created in DRAFT
    assert replacement.lifecycle_state == PosSaleLifecycleState.DRAFT
    assert replacement.is_correction is True
    assert replacement.corrected_from_sale_id == finalized.sale_id
    assert replacement.grand_total_bdt == finalized.grand_total_bdt


@pytest.mark.django_db
def test_p235_cross_tenant_isolation(void_correction_env):
    """
    Scenario 6: Cashier from Tenant B cannot void or correct Tenant A's sales orders.
    """
    env = void_correction_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_b"])

    sale_a = env["draft_sale"]

    # Attempt void
    void_resp = client.post(
        f"/api/v2/pos/sales/{sale_a.sale_id}/void/",
        data={"reason": "Cross tenant malicious void"},
        format="json",
    )
    assert void_resp.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
