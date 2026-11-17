"""
backend/tests/test_p249_pharmacist_only_overrides.py

Architecture Decision: ADR-0249 (P249).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Credentialed Pharmacist Clinical Overrides:
1. Valid emergency chronic care override by licensed pharmacist with PCB, policy ref, consent, and SHA-256 hash.
2. Incomplete input rejection (short PCB, short policy reference, short consent notes, short clinical notes, invalid units).
3. Absolute prohibition of narcotics / Schedule-G controlled substances from pharmacist override.
4. Absolute prohibition of recalled / banned medicines from pharmacist override.
5. Unauthorized cashier role blocked with 403 Forbidden, and cross-tenant isolation enforcement.
"""

from decimal import Decimal
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineCurationStatus,
    MedicineProduct,
    PatientConsentType,
    PharmacistClinicalOverride,
    PharmacistOverrideCategory,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleSourceChannel,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def override_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p249-a-{uid}", name="Override Tenant A", subdomain=f"p249a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p249-a-{uid}", name="Gulshan Branch", branch_code="GL01"
    )

    # Licensed Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_p249_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharma_a = StaffMember.objects.create(
        staff_id=f"stf-p249-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Md. Tanvir Hasan, R.Ph",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-A-9921",
        is_active=True,
    )

    # Cashier for Tenant A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_p249_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p249-b-{uid}", name="Override Tenant B", subdomain=f"p249b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p249-b-{uid}", name="Dhanmondi Branch", branch_code="DH01"
    )
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_p249_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Product: Regular Prescription-Only Medicine (e.g. Antihypertensive)
    prod_rx = MedicineProduct.objects.create(
        product_id=f"prod-rx-{uid}",
        brand_name="Camlodin 5mg Tablet",
        generic_name="Amlodipine",
        dosage_form="Tablet",
        strength="5mg",
        is_antibiotic=False,
        is_prescription_only=True,
        is_narcotic=False,
        mrp_unit=Decimal("7.00"),
        cost_unit=Decimal("5.50"),
    )

    # Product: Narcotic / Controlled Substance (Schedule-G)
    prod_narcotic = MedicineProduct.objects.create(
        product_id=f"prod-narc-{uid}",
        brand_name="Pethidine HCl 100mg Ampoule",
        generic_name="Pethidine",
        dosage_form="Injection",
        strength="100mg/2ml",
        is_antibiotic=False,
        is_prescription_only=True,
        is_narcotic=True,
        mrp_unit=Decimal("120.00"),
        cost_unit=Decimal("90.00"),
    )

    # Product: Recalled / Banned Medicine
    prod_recalled = MedicineProduct.objects.create(
        product_id=f"prod-rec-{uid}",
        brand_name="Ranitidine 150mg Recalled",
        generic_name="Ranitidine",
        dosage_form="Tablet",
        strength="150mg",
        is_antibiotic=False,
        is_prescription_only=False,
        is_narcotic=False,
        curation_status=MedicineCurationStatus.RECALLED,
        mrp_unit=Decimal("3.00"),
        cost_unit=Decimal("2.00"),
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
        "prod_rx": prod_rx,
        "prod_narcotic": prod_narcotic,
        "prod_recalled": prod_recalled,
        "uid": uid,
    }


@pytest.mark.django_db
def test_p249_valid_pharmacist_clinical_override(override_env):
    """
    Scenario 1: Valid emergency chronic care clinical override by licensed pharmacist.
    Transitions sale rx_gate_status to APPROVED, records cryptographic SHA-256 hash,
    and returns 200 OK.
    """
    env = override_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-pco-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-PCO-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_ONLINE,
        requires_rx_verification=True,
        rx_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
        subtotal_bdt=Decimal("21.00"),
        grand_total_bdt=Decimal("21.00"),
        paid_amount_bdt=Decimal("21.00"),
    )
    line = PosSaleOrderLine.objects.create(
        line_id=f"line-pco-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_rx"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=3,
        conversion_factor=1,
        base_units_total=3,
        unit_price_bdt=Decimal("7.00"),
        unit_cost_bdt=Decimal("5.50"),
        line_subtotal_bdt=Decimal("21.00"),
        line_total_bdt=Decimal("21.00"),
        requires_rx_verification=True,
        rx_verified=False,
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    payload = {
        "product_id": env["prod_rx"].product_id,
        "pharmacist_pcb_no": "PCB-A-9921",
        "override_category": PharmacistOverrideCategory.EMERGENCY_CHRONIC_CONTINUATION,
        "statutory_policy_reference": "DGDA Good Pharmacy Practice Guidelines 2024 Sec 8.3",
        "consent_type": PatientConsentType.INFORMED_VERBAL,
        "consent_evidence_notes": "Patient is regular hypertensive awaiting clinic appointment in 48 hours.",
        "clinical_justification": "Emergency 3-day continuation approved to prevent cardiovascular rebound risk.",
        "approved_base_units": 3,
    }

    res = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/",
        payload,
        format="json",
    )
    assert res.status_code == status.HTTP_200_OK
    override_data = res.data["override"]
    assert override_data["override_id"].startswith("pco-")
    assert override_data["pharmacist_pcb_no"] == "PCB-A-9921"
    assert override_data["override_category"] == PharmacistOverrideCategory.EMERGENCY_CHRONIC_CONTINUATION
    assert len(override_data["sha256_hash"]) == 64

    # Verify sale state updated
    sale.refresh_from_db()
    assert sale.rx_gate_status == PosRxGateStatus.APPROVED
    assert sale.pharmacist_pcb_no_snapshot == "PCB-A-9921"
    assert "PHARMACIST_OVERRIDE" in sale.rx_decision_reason

    # Verify sale line verified
    line.refresh_from_db()
    assert line.rx_verified is True

    # Verify immutable database record
    overrides = PharmacistClinicalOverride.objects.filter(sale=sale)
    assert overrides.count() == 1
    record = overrides.first()
    assert record.approved_base_units == 3
    assert record.sha256_hash == override_data["sha256_hash"]

    # Verify GET list overrides endpoint
    res_list = client_pharma.get(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-overrides/"
    )
    assert res_list.status_code == status.HTTP_200_OK
    assert res_list.data["count"] == 1
    assert res_list.data["results"][0]["override_id"] == record.override_id


@pytest.mark.django_db
def test_p249_input_validation_rejections(override_env):
    """
    Scenario 2: Preflight validation rejects short PCB, short policy ref,
    short consent notes, short clinical notes, or zero approved units.
    """
    env = override_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-val-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-VAL-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_ONLINE,
        requires_rx_verification=True,
        rx_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    base_payload = {
        "product_id": env["prod_rx"].product_id,
        "pharmacist_pcb_no": "PCB-12345",
        "override_category": PharmacistOverrideCategory.EMERGENCY_CHRONIC_CONTINUATION,
        "statutory_policy_reference": "DGDA GPP Sec 8.3",
        "consent_type": PatientConsentType.INFORMED_VERBAL,
        "consent_evidence_notes": "Valid verbal consent documented.",
        "clinical_justification": "Valid clinical justification documented.",
        "approved_base_units": 2,
    }

    # 1. Short PCB (<4 chars)
    p1 = dict(base_payload, pharmacist_pcb_no="PC")
    r1 = client_pharma.post(f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/", p1, format="json")
    assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Short policy reference (<5 chars)
    p2 = dict(base_payload, statutory_policy_reference="DG")
    r2 = client_pharma.post(f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/", p2, format="json")
    assert r2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 3. Short consent notes (<5 chars)
    p3 = dict(base_payload, consent_evidence_notes="ok")
    r3 = client_pharma.post(f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/", p3, format="json")
    assert r3.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 4. Short clinical justification (<5 chars)
    p4 = dict(base_payload, clinical_justification="no")
    r4 = client_pharma.post(f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/", p4, format="json")
    assert r4.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 5. Invalid approved base units (<= 0)
    p5 = dict(base_payload, approved_base_units=0)
    r5 = client_pharma.post(f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/", p5, format="json")
    assert r5.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 6. Invalid override category
    p6 = dict(base_payload, override_category="INVALID_CATEGORY")
    r6 = client_pharma.post(f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/", p6, format="json")
    assert r6.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p249_narcotics_strictly_forbidden_from_override(override_env):
    """
    Scenario 3: Narcotics / Schedule-G controlled substances are strictly prohibited
    from pharmacist override under Bangladesh narcotics control statutes.
    """
    env = override_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-narc-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-NARC-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_ONLINE,
        requires_rx_verification=True,
        rx_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    payload = {
        "product_id": env["prod_narcotic"].product_id,
        "pharmacist_pcb_no": "PCB-A-9921",
        "override_category": PharmacistOverrideCategory.EMERGENCY_CHRONIC_CONTINUATION,
        "statutory_policy_reference": "DGDA GPP Sec 8.3",
        "consent_type": PatientConsentType.INFORMED_VERBAL,
        "consent_evidence_notes": "Patient claims regular prescription elsewhere.",
        "clinical_justification": "Patient in severe pain.",
        "approved_base_units": 1,
    }

    res = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/",
        payload,
        format="json",
    )
    assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert res.data["error_code"] == "ERR_NARCOTIC_OVERRIDE_FORBIDDEN"

    # Confirm zero overrides saved
    assert PharmacistClinicalOverride.objects.filter(sale=sale).count() == 0


@pytest.mark.django_db
def test_p249_recalled_or_banned_drugs_strictly_forbidden_from_override(override_env):
    """
    Scenario 4: Recalled or banned drugs cannot be overridden under any condition.
    """
    env = override_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-rec-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-REC-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_ONLINE,
        requires_rx_verification=True,
        rx_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    payload = {
        "product_id": env["prod_recalled"].product_id,
        "pharmacist_pcb_no": "PCB-A-9921",
        "override_category": PharmacistOverrideCategory.EMERGENCY_CHRONIC_CONTINUATION,
        "statutory_policy_reference": "DGDA GPP Sec 8.3",
        "consent_type": PatientConsentType.INFORMED_VERBAL,
        "consent_evidence_notes": "Patient insists on Ranitidine.",
        "clinical_justification": "Attempting override for banned lot.",
        "approved_base_units": 1,
    }

    res = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/",
        payload,
        format="json",
    )
    assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert res.data["error_code"] == "ERR_REGULATED_PRODUCT_BLOCKED"

    # Confirm zero overrides saved
    assert PharmacistClinicalOverride.objects.filter(sale=sale).count() == 0


@pytest.mark.django_db
def test_p249_cashier_rbac_and_cross_tenant_isolation(override_env):
    """
    Scenario 5:
    a) Ordinary Cashier role is rejected with 403 Forbidden.
    b) Pharmacist from another tenant (Tenant B) is blocked with 403/404 Forbidden.
    """
    env = override_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-rbac-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-RBAC-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_ONLINE,
        requires_rx_verification=True,
        rx_gate_status=PosRxGateStatus.PENDING_PHARMACIST,
    )

    valid_payload = {
        "product_id": env["prod_rx"].product_id,
        "pharmacist_pcb_no": "PCB-A-9921",
        "override_category": PharmacistOverrideCategory.EMERGENCY_CHRONIC_CONTINUATION,
        "statutory_policy_reference": "DGDA Good Pharmacy Practice Guidelines 2024 Sec 8.3",
        "consent_type": PatientConsentType.INFORMED_VERBAL,
        "consent_evidence_notes": "Patient is regular hypertensive awaiting refill.",
        "clinical_justification": "Emergency continuation approved.",
        "approved_base_units": 3,
    }

    # a) Cashier attempts override -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])
    res_cashier = client_cashier.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/",
        valid_payload,
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # b) Cross-tenant pharmacist attempts override on Tenant A's sale -> 403/404 Forbidden
    client_cross = APIClient()
    client_cross.force_authenticate(user=env["pharmacist_b"])
    res_cross = client_cross.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/pharmacist-override/",
        valid_payload,
        format="json",
    )
    assert res_cross.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
