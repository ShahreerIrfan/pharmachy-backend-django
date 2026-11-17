"""
backend/tests/test_p250_no_automated_substitution.py

Architecture Decision: ADR-0250 (P250).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for No Automated Substitution, Clinical Advice or Prescriber Impersonation:
1. Valid manual brand substitution by licensed pharmacist with PCB, rationale, audit trail and SHA-256 hash.
2. Attempted autonomous/machine substitution strictly prohibited (ERR_AUTOMATED_SUBSTITUTION_FORBIDDEN).
3. Algorithmic diagnosis, dosage calculation, or prescriber impersonation blocked (ERR_CLINICAL_ADVICE_FORBIDDEN / ERR_PRESCRIBER_IMPERSONATION_FORBIDDEN).
4. Absolute prohibition of substituting to or from narcotics or recalled/banned medicines.
5. Cashier role blocked with 403 Forbidden, cross-tenant isolation enforcement, and public safety disclaimers.
"""

from decimal import Decimal
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    ClinicalSubstitutionAudit,
    MedicineCurationStatus,
    MedicineProduct,
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
def substitution_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p250-a-{uid}", name="Substitution Tenant A", subdomain=f"p250a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p250-a-{uid}", name="Banani Branch", branch_code="BN01"
    )

    # Licensed Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_p250_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharma_a = StaffMember.objects.create(
        staff_id=f"stf-p250-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Mahmudur Rahman, R.Ph",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-B-7714",
        is_active=True,
    )

    # Cashier for Tenant A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_p250_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p250-b-{uid}", name="Substitution Tenant B", subdomain=f"p250b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p250-b-{uid}", name="Mohakhali Branch", branch_code="MK01"
    )
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_p250_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Product A: Prescribed Brand (e.g. Napa 500mg Tablet)
    prod_a = MedicineProduct.objects.create(
        product_id=f"prod-napa-{uid}",
        brand_name="Napa 500mg Tablet",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=False,
        is_prescription_only=False,
        is_narcotic=False,
        mrp_unit=Decimal("1.20"),
        cost_unit=Decimal("0.90"),
    )

    # Product B: Manual Replacement Brand (e.g. Ace 500mg Tablet)
    prod_b = MedicineProduct.objects.create(
        product_id=f"prod-ace-{uid}",
        brand_name="Ace 500mg Tablet",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=False,
        is_prescription_only=False,
        is_narcotic=False,
        mrp_unit=Decimal("1.20"),
        cost_unit=Decimal("0.90"),
    )

    # Product C: Narcotic Product (Schedule-G)
    prod_narcotic = MedicineProduct.objects.create(
        product_id=f"prod-narc-{uid}",
        brand_name="Morphine Sulfate 10mg",
        generic_name="Morphine",
        dosage_form="Tablet",
        strength="10mg",
        is_antibiotic=False,
        is_prescription_only=True,
        is_narcotic=True,
        mrp_unit=Decimal("45.00"),
        cost_unit=Decimal("30.00"),
    )

    # Product D: Recalled Medicine
    prod_recalled = MedicineProduct.objects.create(
        product_id=f"prod-rec-{uid}",
        brand_name="Banned Formulation X",
        generic_name="DangerousGeneric",
        dosage_form="Tablet",
        strength="100mg",
        is_antibiotic=False,
        is_prescription_only=False,
        is_narcotic=False,
        curation_status=MedicineCurationStatus.RECALLED,
        mrp_unit=Decimal("5.00"),
        cost_unit=Decimal("3.00"),
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
        "prod_a": prod_a,
        "prod_b": prod_b,
        "prod_narcotic": prod_narcotic,
        "prod_recalled": prod_recalled,
        "uid": uid,
    }


@pytest.mark.django_db
def test_p250_valid_manual_brand_substitution(substitution_env):
    """
    Scenario 1: Credentialed pharmacist executes an audited manual brand substitution
    for an out-of-stock item with patient consent and valid PCB.
    """
    env = substitution_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-sub-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-SUB-{uid.upper()}",
        source_channel=PosSaleSourceChannel.DESKTOP_POS_ONLINE,
        subtotal_bdt=Decimal("12.00"),
        grand_total_bdt=Decimal("12.00"),
        paid_amount_bdt=Decimal("12.00"),
    )
    line = PosSaleOrderLine.objects.create(
        line_id=f"line-sub-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_a"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=10,
        conversion_factor=1,
        base_units_total=10,
        unit_price_bdt=Decimal("1.20"),
        unit_cost_bdt=Decimal("0.90"),
        line_subtotal_bdt=Decimal("12.00"),
        line_total_bdt=Decimal("12.00"),
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    payload = {
        "original_product_id": env["prod_a"].product_id,
        "replacement_product_id": env["prod_b"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Napa stock depleted; Ace 500mg exact generic Paracetamol substituted with customer verbal consent",
        "is_autonomous_system_substitution": False,
    }

    res = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        payload,
        format="json",
    )
    assert res.status_code == status.HTTP_200_OK
    sub_data = res.data["substitution"]
    assert sub_data["audit_id"].startswith("csa-")
    assert sub_data["pharmacist_pcb_no"] == "PCB-B-7714"
    assert sub_data["original_product_id"] == env["prod_a"].product_id
    assert sub_data["replacement_product_id"] == env["prod_b"].product_id
    assert sub_data["is_manual_human_decision"] is True
    assert len(sub_data["sha256_hash"]) == 64

    # Verify line updated
    line.refresh_from_db()
    assert line.product_id == env["prod_b"].product_id

    # Verify immutable database record
    audits = ClinicalSubstitutionAudit.objects.filter(sale=sale)
    assert audits.count() == 1
    assert audits.first().sha256_hash == sub_data["sha256_hash"]

    # Verify GET list substitutions
    res_list = client_pharma.get(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitutions/"
    )
    assert res_list.status_code == status.HTTP_200_OK
    assert res_list.data["count"] == 1
    assert res_list.data["results"][0]["audit_id"] == audits.first().audit_id


@pytest.mark.django_db
def test_p250_autonomous_substitution_strictly_prohibited(substitution_env):
    """
    Scenario 2: Software is strictly prohibited from executing autonomous or automated
    brand substitutions without human pharmacist evaluation.
    """
    env = substitution_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-auto-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-AUTO-{uid.upper()}",
    )
    PosSaleOrderLine.objects.create(
        line_id=f"line-auto-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_a"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=5,
        conversion_factor=1,
        base_units_total=5,
        unit_price_bdt=Decimal("1.20"),
        unit_cost_bdt=Decimal("0.90"),
        line_subtotal_bdt=Decimal("6.00"),
        line_total_bdt=Decimal("6.00"),
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    # Attempt autonomous substitution flag
    payload = {
        "original_product_id": env["prod_a"].product_id,
        "replacement_product_id": env["prod_b"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Machine automated switch",
        "is_autonomous_system_substitution": True,
    }

    res = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        payload,
        format="json",
    )
    assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert res.data["error_code"] == "ERR_AUTOMATED_SUBSTITUTION_FORBIDDEN"

    # Confirm zero substitutions recorded
    assert ClinicalSubstitutionAudit.objects.filter(sale=sale).count() == 0


@pytest.mark.django_db
def test_p250_algorithmic_diagnosis_and_prescriber_impersonation_blocked(substitution_env):
    """
    Scenario 3: Attempted algorithmic medical advice (AI diagnosis, automated dosage)
    or synthetic prescriber impersonation must be rejected.
    """
    env = substitution_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-diag-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-DIAG-{uid.upper()}",
    )
    PosSaleOrderLine.objects.create(
        line_id=f"line-diag-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_a"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=5,
        conversion_factor=1,
        base_units_total=5,
        unit_price_bdt=Decimal("1.20"),
        unit_cost_bdt=Decimal("0.90"),
        line_subtotal_bdt=Decimal("6.00"),
        line_total_bdt=Decimal("6.00"),
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    # a) Algorithmic diagnosis / clinical dosage advice
    p_diag = {
        "original_product_id": env["prod_a"].product_id,
        "replacement_product_id": env["prod_b"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Manual switch",
        "ai_diagnosis": "System diagnosed Acute Pyrexia of Unknown Origin",
        "automated_dosage_prescribed": "500mg TDS for 5 days",
    }
    r_diag = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        p_diag,
        format="json",
    )
    assert r_diag.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert r_diag.data["error_code"] == "ERR_CLINICAL_ADVICE_FORBIDDEN"

    # b) Prescriber impersonation / synthetic credentials
    p_imp = {
        "original_product_id": env["prod_a"].product_id,
        "replacement_product_id": env["prod_b"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Manual switch",
        "impersonate_prescriber": True,
        "auto_generate_bmdc": True,
    }
    r_imp = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        p_imp,
        format="json",
    )
    assert r_imp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert r_imp.data["error_code"] == "ERR_PRESCRIBER_IMPERSONATION_FORBIDDEN"


@pytest.mark.django_db
def test_p250_narcotics_and_recalled_drugs_substitution_forbidden(substitution_env):
    """
    Scenario 4: Narcotics / Schedule-G controlled substances and recalled/banned drugs
    cannot be substituted under any circumstances.
    """
    env = substitution_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-dang-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-DANG-{uid.upper()}",
    )
    PosSaleOrderLine.objects.create(
        line_id=f"line-dang-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_narcotic"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=2,
        conversion_factor=1,
        base_units_total=2,
        unit_price_bdt=Decimal("45.00"),
        unit_cost_bdt=Decimal("30.00"),
        line_subtotal_bdt=Decimal("90.00"),
        line_total_bdt=Decimal("90.00"),
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    # 1. Narcotic substitution blocked
    p_narc = {
        "original_product_id": env["prod_narcotic"].product_id,
        "replacement_product_id": env["prod_b"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Morphine stockout attempt",
    }
    r_narc = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        p_narc,
        format="json",
    )
    assert r_narc.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert r_narc.data["error_code"] == "ERR_NARCOTIC_OVERRIDE_FORBIDDEN"

    # 2. Recalled drug substitution blocked
    p_rec = {
        "original_product_id": env["prod_a"].product_id,
        "replacement_product_id": env["prod_recalled"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Attempting swap to recalled medicine",
    }
    r_rec = client_pharma.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        p_rec,
        format="json",
    )
    assert r_rec.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert r_rec.data["error_code"] == "ERR_REGULATED_PRODUCT_BLOCKED"


@pytest.mark.django_db
def test_p250_cashier_rbac_and_cross_tenant_isolation(substitution_env):
    """
    Scenario 5:
    a) Ordinary Cashier role blocked with 403 Forbidden.
    b) Cross-tenant pharmacist blocked with 403/404 Forbidden.
    c) Statutory disclaimers endpoint accessible to all roles.
    """
    env = substitution_env
    uid = env["uid"]

    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-rbac-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-RBAC-{uid.upper()}",
    )
    PosSaleOrderLine.objects.create(
        line_id=f"line-rbac-{uid}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["prod_a"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=5,
        conversion_factor=1,
        base_units_total=5,
        unit_price_bdt=Decimal("1.20"),
        unit_cost_bdt=Decimal("0.90"),
        line_subtotal_bdt=Decimal("6.00"),
        line_total_bdt=Decimal("6.00"),
    )

    valid_payload = {
        "original_product_id": env["prod_a"].product_id,
        "replacement_product_id": env["prod_b"].product_id,
        "pharmacist_pcb_no": "PCB-B-7714",
        "clinical_rationale": "Valid generic substitution with patient consent",
    }

    # a) Cashier attempts brand substitution -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])
    res_cashier = client_cashier.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        valid_payload,
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # b) Cross-tenant pharmacist attempts substitution on Tenant A's sale -> 403/404 Forbidden
    client_cross = APIClient()
    client_cross.force_authenticate(user=env["pharmacist_b"])
    res_cross = client_cross.post(
        f"/api/v2/pos/sales/{sale.sale_id}/rx-gate/substitute-item/",
        valid_payload,
        format="json",
    )
    assert res_cross.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    # c) Statutory disclaimers endpoint accessible
    res_disc = client_cashier.get("/api/v2/pos/sales/clinical-safety-disclaimers/")
    assert res_disc.status_code == status.HTTP_200_OK
    assert "administrative workflow system" in res_disc.data["disclaimer_en"]
    assert "প্রশাসনিক ওয়ার্কফ্লো" in res_disc.data["disclaimer_bn"]
    assert res_disc.data["prohibitions"]["no_automated_substitution"] is True

