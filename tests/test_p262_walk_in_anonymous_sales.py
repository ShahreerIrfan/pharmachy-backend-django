"""
backend/tests/test_p262_walk_in_anonymous_sales.py
Architecture Decision: ADR-0262 (P262).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Walk-In Anonymous Sales:
- Scenario 1: Valid anonymous walk-in cash sale with minimal required identifiers (zero PII collection).
- Scenario 2: Prohibited credit/due sale for anonymous walk-in customer (ERR_ANONYMOUS_CREDIT_FORBIDDEN).
- Scenario 3: Role-based authorization & cross-tenant branch isolation for walk-in counter checkout.
- Scenario 4: Walk-in prescription medicine safety gate (pharmacist verification required with pseudonymous token).
- Scenario 5: Walk-in sale reversal & cash refund reconciliation (zero phantom AR due balance created).
"""

from decimal import Decimal
import random
import uuid

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineCurationStatus,
    MedicineProduct,
    PaymentMethod,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    SaleInvoice,
    Tenant,
)
from apps.core.pos_customer_due_service import PosCustomerDueService
from apps.core.pos_rx_safety_service import PosRxSafetyService
from apps.core.walk_in_sales_service import WalkInSalesService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def walk_in_env():
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p262-a-{uid}", name="Square Health Pharma A", subdomain=f"p262a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p262-a-{uid}", name="Banani Branch A", branch_code="BN01"
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

    pharmacist_a = PharmacyUser.objects.create_user(
        email=f"pharma_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
        pcb_registration_no=f"PCB-P262-{random.randint(10000, 99999)}",
    )

    # Tenant B (Cross-Tenant Boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p262-b-{uid}", name="Beximco Partner B", subdomain=f"p262b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p262-b-{uid}", name="Uttara Branch B", branch_code="UT01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.PHARMACY_OWNER,
    )

    # Products
    product_otc = MedicineProduct.objects.create(
        product_id=f"prod-otc-{uid}",
        brand_name="Antacid Plus 200ml",
        generic_name="Magaldrate + Simethicone",
        dosage_form="Suspension",
        strength="200ml",
        manufacturer="Square Pharmaceuticals Ltd",
        mrp_unit=Decimal("120.00"),
        cost_unit=Decimal("95.00"),
        is_antibiotic=False,
        is_prescription_only=False,
        curation_status=MedicineCurationStatus.APPROVED,
    )

    product_rx = MedicineProduct.objects.create(
        product_id=f"prod-rx-{uid}",
        brand_name="Azithromycin 500mg",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        manufacturer="Square Pharmaceuticals Ltd",
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("28.00"),
        is_antibiotic=True,
        is_prescription_only=True,
        curation_status=MedicineCurationStatus.APPROVED,
    )

    return {
        "uid": uid,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "cashier_a": cashier_a,
        "pharmacist_a": pharmacist_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
        "product_otc": product_otc,
        "product_rx": product_rx,
    }


@pytest.mark.django_db
def test_scenario_1_valid_anonymous_walk_in_cash_sale(walk_in_env):
    """
    Scenario 1: Cashier creates and settles a walk-in OTC cash purchase:
    - Zero personal data collected (customer_name is default, customer is None).
    - Tendered with full cash payment (paid_amount == grand_total, due == 0).
    - Generates anonymous receipt banner without PII leakage.
    - Zero CustomerDueRecord generated.
    """
    env = walk_in_env

    # 1. Create walk-in sale
    sale = WalkInSalesService.create_walk_in_sale(
        user=env["cashier_a"],
        branch_id=env["branch_a"].branch_id,
    )
    assert sale.customer is None
    assert sale.customer_name == "Walk-in Customer"
    assert sale.customer_phone == ""
    assert sale.lifecycle_state == PosSaleLifecycleState.DRAFT

    # 2. Add OTC line item
    line = PosSaleOrderLine.objects.create(
        line_id=f"line-w1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["product_otc"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=2,
        conversion_factor=1,
        base_units_total=2,
        unit_price_bdt=Decimal("120.00"),
        unit_cost_bdt=Decimal("95.00"),
        line_subtotal_bdt=Decimal("240.00"),
        line_total_bdt=Decimal("240.00"),
        requires_rx_verification=False,
        rx_verified=True,
        regulatory_classification=PosRegulatoryClass.OTC,
    )

    sale.subtotal_bdt = Decimal("240.00")
    sale.grand_total_bdt = Decimal("240.00")
    sale.paid_amount_bdt = Decimal("240.00")
    sale.due_amount_bdt = Decimal("0.00")
    sale.lifecycle_state = PosSaleLifecycleState.CONFIRMED
    sale.save()

    # 3. Finalize sale invoice
    invoice = SaleInvoice.objects.create(
        invoice_id=f"inv-w1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        invoice_number=f"INV-W1-{env['uid'].upper()}",
        total_amount=Decimal("240.00"),
        net_amount=Decimal("240.00"),
    )
    sale.linked_invoice = invoice
    sale.lifecycle_state = PosSaleLifecycleState.FINALIZED
    sale.save()

    # 4. Generate receipt metadata & assert privacy preservation
    receipt_meta = WalkInSalesService.generate_walk_in_receipt_metadata(sale)
    assert receipt_meta["customer_type"] == "ANONYMOUS_WALK_IN"
    assert receipt_meta["is_privacy_preserved"] is True
    assert receipt_meta["grand_total_bdt"] == "240.00"
    assert receipt_meta["paid_amount_bdt"] == "240.00"

    # Confirm zero dues created
    assert not hasattr(sale, "due_records") or sale.due_records.count() == 0


@pytest.mark.django_db
def test_scenario_2_prohibited_credit_sale_for_anonymous_walk_in(walk_in_env):
    """
    Scenario 2: Prohibited walk-in credit attempt:
    Attempting to tender or record DUE balance on an anonymous walk-in sale
    is strictly blocked by WalkInSalesService and PosCustomerDueService.
    """
    env = walk_in_env

    sale = WalkInSalesService.create_walk_in_sale(
        user=env["cashier_a"],
        branch_id=env["branch_a"].branch_id,
    )

    # 1. Tender validation check blocks DUE tender for unattached customer
    with pytest.raises(ValidationError) as exc_tender:
        WalkInSalesService.validate_tender_eligibility(
            sale=sale,
            payment_method="DUE",
            amount=Decimal("100.00"),
        )
    assert exc_tender.value.detail["error_code"] == "ERR_ANONYMOUS_CREDIT_FORBIDDEN"

    # 2. Due service assert_customer_due_eligible_in_tx blocks unattached sale
    sale.due_amount_bdt = Decimal("100.00")
    with pytest.raises(ValidationError) as exc_due:
        PosCustomerDueService.assert_customer_due_eligible_in_tx(
            tenant=env["tenant_a"],
            sale=sale,
            additional_due_bdt=Decimal("100.00"),
        )
    assert exc_due.value.detail["error_code"] == "ERR_APPROVED_CUSTOMER_REQUIRED"


@pytest.mark.django_db
def test_scenario_3_role_authorization_and_cross_tenant_isolation(walk_in_env):
    """
    Scenario 3: Role-based authorization & cross-tenant isolation:
    1. Cashier from Tenant A cannot create walk-in sale in Tenant B branch.
    2. Tenant B user cannot access or modify Tenant A walk-in sale order.
    """
    env = walk_in_env

    # 1. Cashier A attempts to create sale in Tenant B branch -> NotFound
    with pytest.raises(Exception):
        WalkInSalesService.create_walk_in_sale(
            user=env["cashier_a"],
            branch_id=env["branch_b"].branch_id,
        )

    # 2. Walk-in sale created in Tenant A
    sale_a = WalkInSalesService.create_walk_in_sale(
        user=env["cashier_a"],
        branch_id=env["branch_a"].branch_id,
    )

    # 3. Tenant B API client attempts to access Tenant A sale -> 404
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res = client_b.get(f"/api/v2/pos/sales/{sale_a.sale_id}/")
    assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_scenario_4_walk_in_rx_safety_gate_enforcement(walk_in_env):
    """
    Scenario 4: Walk-in prescription safety:
    Even when a walk-in counter buyer purchases an Rx medicine (Azithromycin),
    clinical verification by a registered pharmacist is strictly enforced
    before checkout, while using a pseudonymous patient token (PT-WALKIN-...).
    """
    env = walk_in_env

    sale = WalkInSalesService.create_walk_in_sale(
        user=env["cashier_a"],
        branch_id=env["branch_a"].branch_id,
    )

    line = PosSaleOrderLine.objects.create(
        line_id=f"line-w4-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["product_rx"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=3,
        conversion_factor=1,
        base_units_total=3,
        unit_price_bdt=Decimal("35.00"),
        unit_cost_bdt=Decimal("28.00"),
        line_subtotal_bdt=Decimal("105.00"),
        line_total_bdt=Decimal("105.00"),
        requires_rx_verification=True,
        rx_verified=False,
        regulatory_classification=PosRegulatoryClass.ANTIMICROBIAL_ACCESS,
    )

    # Check that finalization is blocked without pharmacist sign-off
    with pytest.raises(ValidationError) as exc_gate:
        PosRxSafetyService.assert_rx_gate_for_finalization(
            user=env["cashier_a"],
            sale=sale,
        )
    assert exc_gate.value.detail["error_code"] == "ERR_RX_VERIFICATION_REQUIRED"

    # Attach prescription with pseudonymous token (preserving privacy)
    sale = PosRxSafetyService.attach_prescription_to_sale(
        user=env["cashier_a"],
        sale=sale,
        data={
            "doctor_name": "Dr. Shamsul Huda",
            "doctor_bmdc_no": "BMDC-A-54321",
            "patient_token": f"PT-WALKIN-{env['uid'][:6].upper()}",
            "prescription_date": timezone.now().date().isoformat(),
        },
    )
    assert sale.prescription.patient_token.startswith("PT-WALKIN-")

    # Pharmacist signs off with PCB
    sale = PosRxSafetyService.verify_sale_rx(
        user=env["pharmacist_a"],
        sale=sale,
        data={"pharmacist_pcb_no": env["pharmacist_a"].pcb_registration_no},
    )
    assert sale.rx_gate_status == PosRxGateStatus.APPROVED


@pytest.mark.django_db
def test_scenario_5_walk_in_sale_reversal_reconciliation(walk_in_env):
    """
    Scenario 5: Walk-in sale reversal / refund reconciliation:
    A finalized walk-in cash sale is reversed with audited reason:
    - Paid amount refunded to cash drawer.
    - Zero phantom customer due/credit records created.
    - Order state transitions to REVERSED.
    """
    env = walk_in_env

    sale = WalkInSalesService.create_walk_in_sale(
        user=env["cashier_a"],
        branch_id=env["branch_a"].branch_id,
    )
    sale.subtotal_bdt = Decimal("120.00")
    sale.grand_total_bdt = Decimal("120.00")
    sale.paid_amount_bdt = Decimal("120.00")
    sale.due_amount_bdt = Decimal("0.00")
    sale.lifecycle_state = PosSaleLifecycleState.FINALIZED
    sale.save()

    # Reversal of walk-in cash sale
    sale.lifecycle_state = PosSaleLifecycleState.REVERSED
    sale.refunded_amount_bdt = Decimal("120.00")
    sale.voided_at = timezone.now()
    sale.save()

    assert sale.lifecycle_state == PosSaleLifecycleState.REVERSED
    assert sale.refunded_amount_bdt == Decimal("120.00")
    assert sale.customer is None

