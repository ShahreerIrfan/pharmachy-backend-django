"""
Architecture Decision: ADR-0234 (P234).
Targeted Automated Test Suite for POS Sales Refund, Item Returns,
Lot Traceability, Pharmacist Safety Disposition, and Cashier Shift Drawer Impact.
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
    PosCashierShift,
    PosPackagingUnit,
    PosRefundMethod,
    PosReturnSafetyDisposition,
    PosReturnStatus,
    PosSaleReturn,
    PosSaleReturnLine,
    PosShiftStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_return_service import PosReturnService


@pytest.fixture
def return_env(db):
    tenant_a = Tenant.objects.create(
        tenant_id="ten-p234-a",
        name="Return Test Pharmacy A",
        subdomain="p234a",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_a = Branch.objects.create(
        branch_id="br-p234-dhk",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        branch_code="DHK01",
    )
    cashier_a = PharmacyUser.objects.create_user(
        email="cashier_p234a@test.com",
        phone_number="+8801744445566",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        full_name="Cashier P234 A",
    )

    tenant_b = Tenant.objects.create(
        tenant_id="ten-p234-b",
        name="Return Test Pharmacy B",
        subdomain="p234b",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    branch_b = Branch.objects.create(
        branch_id="br-p234-ctg",
        tenant=tenant_b,
        name="Chittagong Branch",
        branch_code="CTG01",
    )
    cashier_b = PharmacyUser.objects.create_user(
        email="cashier_p234b@test.com",
        phone_number="+8801744445577",
        password="PassWord123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        full_name="Cashier P234 B",
    )

    product = MedicineProduct.objects.create(
        product_id="prod-p234-napa",
        brand_name="Napa Extra 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_antibiotic=False,
        is_narcotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("2.50"),
        cost_unit=Decimal("1.80"),
    )

    batch_a = BatchStock.objects.create(
        batch_id="batch-p234-01",
        tenant=tenant_a,
        branch=branch_a,
        product=product,
        batch_number="BX-P234-01",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        cost_per_unit=Decimal("1.80"),
        quantity_base_units=100,
    )

    # Initial sale of 10 tablets
    sale_a, _ = PosDomainService.create_sale_order(
        cashier_a,
        {
            "branch_id": branch_a.branch_id,
            "offline_device_namespace": "",
            "customer_name": "Karim Hasan",
            "customer_phone": "01711223344",
            "lines": [
                {
                    "product": product.product_id,
                    "quantity_units": 10,
                    "unit_price_bdt": "2.50",
                    "batch_number": batch_a.batch_number,
                }
            ],
            "payments": [
                {
                    "payment_method": PaymentMethod.CASH,
                    "tender_type": "CASH",
                    "amount_bdt": "25.00",
                }
            ],
            "discount_amount_bdt": "0.00",
            "notes": "P234 Initial Sale",
        },
    )

    return {
        "tenant_a": tenant_a,
        "branch_a": branch_a,
        "cashier_a": cashier_a,
        "tenant_b": tenant_b,
        "branch_b": branch_b,
        "cashier_b": cashier_b,
        "product": product,
        "batch_a": batch_a,
        "sale_a": sale_a,
    }


@pytest.mark.django_db
def test_p234_valid_return_with_restockable_disposition(return_env):
    """
    Scenario 1: Valid return with RESTOCKABLE disposition restores batch quantity_base_units,
    increments returned_base_units, and creates an audited PosSaleReturn.
    """
    env = return_env
    sale = env["sale_a"]
    batch = env["batch_a"]
    sale_line = sale.lines.first()
    initial_batch_qty = batch.quantity_base_units

    # Return 4 units as RESTOCKABLE
    sale_return = PosReturnService.process_return(
        user=env["cashier_a"],
        sale_id=sale.sale_id,
        lines_data=[
            {
                "sale_line_id": sale_line.line_id,
                "quantity_units": 4,
                "batch_number": batch.batch_number,
                "safety_disposition": PosReturnSafetyDisposition.RESTOCKABLE,
                "disposition_notes": "Unopened strip verified",
            }
        ],
        refund_method=PosRefundMethod.CASH,
        reason="Wrong dosage purchased by customer",
        customer_name="Karim Hasan",
    )

    assert sale_return.status == PosReturnStatus.COMPLETED
    assert sale_return.total_refund_amount_bdt == Decimal("10.00")  # 4 * 2.50
    assert sale_return.refund_method == PosRefundMethod.CASH
    assert sale_return.lines.count() == 1

    ret_line = sale_return.lines.first()
    assert ret_line.quantity_units == 4
    assert ret_line.base_units_returned == 4
    assert ret_line.safety_disposition == PosReturnSafetyDisposition.RESTOCKABLE

    # Verify original sale line updated
    sale_line.refresh_from_db()
    assert sale_line.returned_base_units == 4

    # Verify inventory restored
    batch.refresh_from_db()
    assert batch.quantity_base_units == initial_batch_qty + 4


@pytest.mark.django_db
def test_p234_over_return_prevention(return_env):
    """
    Scenario 2: Attempting to return more units than sold (or remaining returnable)
    is blocked with a ValidationError.
    """
    env = return_env
    sale = env["sale_a"]
    sale_line = sale.lines.first()

    # Sale had 10 units. Attempt to return 11 units.
    with pytest.raises(ValidationError) as exc_info:
        PosReturnService.process_return(
            user=env["cashier_a"],
            sale_id=sale.sale_id,
            lines_data=[
                {
                    "sale_line_id": sale_line.line_id,
                    "quantity_units": 11,
                    "safety_disposition": PosReturnSafetyDisposition.RESTOCKABLE,
                }
            ],
            refund_method=PosRefundMethod.CASH,
            reason="Over return attempt",
        )
    assert "quantity_units" in exc_info.value.message_dict


@pytest.mark.django_db
def test_p234_damaged_disposition_does_not_restock_inventory(return_env):
    """
    Scenario 3: Returning items with DAMAGED_DESTROYED or QUARANTINED disposition
    does NOT restore saleable inventory, ensuring damaged medicine is not resold.
    """
    env = return_env
    sale = env["sale_a"]
    batch = env["batch_a"]
    sale_line = sale.lines.first()
    initial_batch_qty = batch.quantity_base_units

    # Return 3 units as DAMAGED_DESTROYED
    sale_return = PosReturnService.process_return(
        user=env["cashier_a"],
        sale_id=sale.sale_id,
        lines_data=[
            {
                "sale_line_id": sale_line.line_id,
                "quantity_units": 3,
                "batch_number": batch.batch_number,
                "safety_disposition": PosReturnSafetyDisposition.DAMAGED_DESTROYED,
                "disposition_notes": "Blister seal broken",
            }
        ],
        refund_method=PosRefundMethod.MFS_BKASH,
        reason="Damaged package",
    )

    assert sale_return.status == PosReturnStatus.COMPLETED
    assert sale_return.total_refund_amount_bdt == Decimal("7.50")

    # Inventory must NOT change
    batch.refresh_from_db()
    assert batch.quantity_base_units == initial_batch_qty

    # Sale line must reflect return
    sale_line.refresh_from_db()
    assert sale_line.returned_base_units == 3


@pytest.mark.django_db
def test_p234_cash_drawer_reconciliation(return_env):
    """
    Scenario 4: When a refund is issued in CASH, the cashier's active shift
    records the cash refund and recalculates expected drawer balance.
    """
    env = return_env
    tenant_a = env["tenant_a"]
    branch_a = env["branch_a"]
    cashier_a = env["cashier_a"]
    sale = env["sale_a"]
    sale_line = sale.lines.first()

    # Open active shift for cashier
    shift = PosCashierShift.objects.create(
        tenant=tenant_a,
        branch=branch_a,
        cashier_user=cashier_a,
        device_code="POS-01",
        status=PosShiftStatus.OPEN,
        opening_float_bdt=Decimal("500.00"),
        total_cash_sales_bdt=Decimal("200.00"),
        total_cash_refunds_bdt=Decimal("0.00"),
        total_payouts_bdt=Decimal("0.00"),
        total_cash_in_bdt=Decimal("0.00"),
        expected_drawer_cash_bdt=Decimal("700.00"),  # 500 + 200
        opened_at=timezone.now(),
    )

    # Process cash refund of 2 units (2 * 2.50 = 5.00 BDT)
    PosReturnService.process_return(
        user=cashier_a,
        sale_id=sale.sale_id,
        lines_data=[
            {
                "sale_line_id": sale_line.line_id,
                "quantity_units": 2,
                "safety_disposition": PosReturnSafetyDisposition.RESTOCKABLE,
            }
        ],
        refund_method=PosRefundMethod.CASH,
        reason="Patient return",
    )

    shift.refresh_from_db()
    assert shift.total_cash_refunds_bdt == Decimal("5.00")
    # Expected drawer = 500 + 200 - 5 = 695.00
    assert shift.expected_drawer_cash_bdt == Decimal("695.00")


@pytest.mark.django_db
def test_p234_cross_tenant_isolation(return_env):
    """
    Scenario 5: Cashier from Tenant B cannot view or process returns
    against Tenant A's sales orders.
    """
    env = return_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_b"])

    sale_a = env["sale_a"]
    sale_line_a = sale_a.lines.first()

    # Attempt return against Tenant A's sale by Tenant B's cashier
    payload = {
        "sale_id": sale_a.sale_id,
        "refund_method": "CASH",
        "reason": "Unauthorized cross tenant attempt",
        "lines": [
            {
                "sale_line_id": sale_line_a.line_id,
                "quantity_units": 1,
                "safety_disposition": "RESTOCKABLE",
            }
        ],
    }

    response = client.post("/api/v2/pos/returns/", data=payload, format="json")
    # Must fail because sale order is not found in Tenant B's context
    assert response.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
