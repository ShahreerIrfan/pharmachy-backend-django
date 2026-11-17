"""
backend/tests/test_p248_offline_prescription_authorization.py

Architecture Decision: ADR-0248 (P248).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Conservative Offline Prescription Authorization:
1. Valid offline prescription authorization by on-duty pharmacist enables offline POS finalization.
2. Incomplete input rejection (short PCB, missing doctor BMDC, short review notes).
3. Absolute prohibition of narcotics / Schedule-G controlled substances from offline dispensing.
4. Unauthorized cashier role blocked with 403 Forbidden.
5. Cross-tenant isolation blocked with 403/404.
"""

from decimal import Decimal
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    PaymentMethod,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def offline_rx_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p248-a-{uid}", name="Offline Rx Tenant A", subdomain=f"p248a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p248-a-{uid}", name="Mirpur Branch", branch_code="MP01"
    )

    # Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_p248_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharma_a = StaffMember.objects.create(
        staff_id=f"stf-p248-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Dr. Enamul Kabir",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-99412",
        is_active=True,
    )

    # Cashier for Tenant A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_p248_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p248-b-{uid}", name="Offline Rx Tenant B", subdomain=f"p248b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p248-b-{uid}", name="Uttara Branch", branch_code="UT01"
    )
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_p248_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Product: Antibiotic (Access tier, requires prescription)
    prod_antibiotic = MedicineProduct.objects.create(
        product_id=f"prod-abx-{uid}",
        brand_name="Azithral 500mg Tablet",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=True,
        is_prescription_only=True,
        is_narcotic=False,
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("25.00"),
    )

    # Product: Narcotic / Controlled Substance (Schedule-G)
    prod_narcotic = MedicineProduct.objects.create(
        product_id=f"prod-narc-{uid}",
        brand_name="Morphine Sulfate 15mg",
        generic_name="Morphine",
        dosage_form="Tablet",
        strength="15mg",
        is_antibiotic=False,
        is_prescription_only=True,
        is_narcotic=True,
        mrp_unit=Decimal("50.00"),
        cost_unit=Decimal("35.00"),
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "pharmacist_a": pharmacist_user_a,
        "staff_pharma_a": staff_pharma_a,
        "cashier_a": cashier_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "pharmacist_b": pharmacist_user_b,
        "prod_antibiotic": prod_antibiotic,
        "prod_narcotic": prod_narcotic,
        "uid": uid,
    }


@pytest.mark.django_db
def test_p248_valid_offline_prescription_authorization(offline_rx_env):
    """
    Scenario 1: Offline prescription authorization by on-duty pharmacist transitions
    cart to APPROVED, enabling safe offline checkout.
    """
    env = offline_rx_env
    uid = env["uid"]

    # Create Offline Sale with Antibiotic
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-off-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-OFF-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_OFFLINE,
        requires_rx_verification=True,
        rx_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
        subtotal_bdt=Decimal("175.00"),
        grand_total_bdt=Decimal("175.00"),
        paid_amount_bdt=Decimal("175.00"),
    )
    PosSaleOrderLine.objects.create(
        line_id=f"line-off-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_antibiotic"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=5,
        conversion_factor=1,
        base_units_total=5,
        unit_price_bdt=Decimal("35.00"),
        unit_cost_bdt=Decimal("25.00"),
        line_subtotal_bdt=Decimal("175.00"),
        line_total_bdt=Decimal("175.00"),
        requires_rx_verification=True,
        rx_verified=False,
        regulatory_classification=PosRegulatoryClass.ANTIMICROBIAL_ACCESS,
    )

    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])

    # 1. Preflight initially blocked (422 ERR_OFFLINE_RX_BLOCKED)
    res_pre_blk = client_cashier.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-preflight/",
        {},
        format="json",
    )
    assert res_pre_blk.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert res_pre_blk.data["offline_eligible"] is False
    assert res_pre_blk.data["error_code"] == "ERR_OFFLINE_RX_BLOCKED"

    # 2. On-duty Pharmacist authorizes offline prescription
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])
    res_auth = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PCB-99412",
            "doctor_bmdc_no": "BMDC-A8821",
            "doctor_name": "Dr. Nazmul Islam",
            "patient_token": f"PT-OFF-{uid}",
            "review_notes": "Emergency physical paper prescription verified by on-duty registered pharmacist.",
        },
        format="json",
    )
    assert res_auth.status_code == status.HTTP_200_OK
    assert res_auth.data["rx_gate_status"] == PosRxGateStatus.APPROVED
    assert res_auth.data["can_finalize_offline"] is True

    # 3. Preflight now succeeds (200 OK, offline_eligible=True)
    res_pre_ok = client_cashier.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-preflight/",
        {},
        format="json",
    )
    assert res_pre_ok.status_code == status.HTTP_200_OK
    assert res_pre_ok.data["offline_eligible"] is True

    # 4. Verify Rx Safety Gate allows offline finalization
    sale.refresh_from_db()
    from apps.core.pos_rx_safety_service import PosRxSafetyService
    gate_summary = PosRxSafetyService.assert_rx_gate_for_finalization(
        env["cashier_a"],
        sale,
        is_offline_sync=True,
    )
    assert gate_summary["can_finalize_offline"] is True
    assert gate_summary["rx_gate_status"] == PosRxGateStatus.APPROVED


@pytest.mark.django_db
def test_p248_incomplete_input_rejection(offline_rx_env):
    """
    Scenario 2: Missing or short credentials (PCB, BMDC, notes) are rejected with 400/422.
    """
    env = offline_rx_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-inc-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-INC-{uid.upper()}",
    )

    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    # Missing/short PCB (< 4 chars)
    res_bad_pcb = client.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PC",
            "doctor_bmdc_no": "BMDC-A8821",
            "doctor_name": "Dr. Nazmul Islam",
            "review_notes": "Valid review notes",
        },
        format="json",
    )
    assert res_bad_pcb.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Missing doctor BMDC
    res_no_bmdc = client.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PCB-99412",
            "doctor_bmdc_no": "",
            "doctor_name": "Dr. Nazmul Islam",
            "review_notes": "Valid review notes",
        },
        format="json",
    )
    assert res_no_bmdc.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Short notes (< 5 chars)
    res_short_notes = client.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PCB-99412",
            "doctor_bmdc_no": "BMDC-A8821",
            "doctor_name": "Dr. Nazmul Islam",
            "review_notes": "OK",
        },
        format="json",
    )
    assert res_short_notes.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p248_narcotics_strictly_forbidden_offline(offline_rx_env):
    """
    Scenario 3: Narcotics and Schedule-G controlled substances are STRICTLY PROHIBITED
    from offline dispensing under any condition.
    """
    env = offline_rx_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-narc-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-NARC-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_OFFLINE,
    )
    PosSaleOrderLine.objects.create(
        line_id=f"line-narc-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_narcotic"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=2,
        conversion_factor=1,
        base_units_total=2,
        unit_price_bdt=Decimal("50.00"),
        unit_cost_bdt=Decimal("35.00"),
        line_subtotal_bdt=Decimal("100.00"),
        line_total_bdt=Decimal("100.00"),
        requires_rx_verification=True,
        regulatory_classification=PosRegulatoryClass.NARCOTIC_SCHEDULED,
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    # 1. Preflight is blocked with ERR_OFFLINE_NARCOTIC_FORBIDDEN
    res_pre = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-preflight/",
        {},
        format="json",
    )
    assert res_pre.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert res_pre.data["error_code"] == "ERR_OFFLINE_NARCOTIC_FORBIDDEN"

    # 2. Attempting offline authorization on narcotic cart is rejected
    res_auth = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PCB-99412",
            "doctor_bmdc_no": "BMDC-A8821",
            "doctor_name": "Dr. Nazmul Islam",
            "review_notes": "Attempting offline narcotic authorization",
        },
        format="json",
    )
    assert res_auth.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_OFFLINE_NARCOTIC_FORBIDDEN" in str(res_auth.data)


@pytest.mark.django_db
def test_p248_unauthorized_cashier_role_blocked(offline_rx_env):
    """
    Scenario 4: Cashier attempting offline prescription authorization is blocked with 403 Forbidden.
    """
    env = offline_rx_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-csh-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-CSH-{uid.upper()}",
    )

    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])

    res = client_cashier.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PCB-FAKE",
            "doctor_bmdc_no": "BMDC-A8821",
            "doctor_name": "Dr. Nazmul Islam",
            "review_notes": "Cashier unauthorized attempt",
        },
        format="json",
    )
    assert res.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_p248_cross_tenant_isolation(offline_rx_env):
    """
    Scenario 5: Pharmacist from Tenant B cannot authorize a sale order belonging to Tenant A.
    """
    env = offline_rx_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-tenta-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-TA-{uid.upper()}",
    )

    client_b = APIClient()
    client_b.force_authenticate(user=env["pharmacist_b"])

    res = client_b.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/offline-authorize/",
        {
            "pharmacist_pcb_no": "PCB-99412",
            "doctor_bmdc_no": "BMDC-A8821",
            "doctor_name": "Dr. Nazmul Islam",
            "review_notes": "Cross tenant attempt",
        },
        format="json",
    )
    assert res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
