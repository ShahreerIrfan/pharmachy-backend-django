"""
backend/tests/test_p246_antibiotic_register_ledger.py

Architecture Decision: ADR-0246 (P246).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Antibiotic Register Ledger:
1. Valid antibiotic dispense register recording linked to sale, verified Rx, pharmacist PCB, branch.
2. Incomplete and invalid input validation (missing PCB, missing Rx, non-antibiotic product, zero units).
3. Audited partial return and full reversal with state transitions.
4. Privacy-safe DGDA compliance ledger export and AWaRe summary metrics.
5. Cross-tenant isolation and unauthorized cashier reversal block.
"""

from datetime import date, timedelta
from decimal import Decimal
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.antibiotic_register_service import AntibioticRegisterLedgerService
from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AntibioticRegisterStatus,
    Branch,
    MedicineProduct,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosSaleOrder,
    PosSaleOrderLine,
    PrescriptionRecord,
    PrescriptionStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.prescription_service import PrescriptionService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def abx_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p246-a-{uid}", name="Abx Register Tenant A", subdomain=f"p246a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p246-a-{uid}", name="Dhanmondi Branch", branch_code="DN01"
    )

    # Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_abx_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharmacist_a = StaffMember.objects.create(
        staff_id=f"stf-abx-a-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Dr. Shahinur Alam",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-ABX-8821",
        is_active=True,
    )

    # Cashier for Tenant A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_abx_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p246-b-{uid}", name="Abx Register Tenant B", subdomain=f"p246b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p246-b-{uid}", name="Banani Branch", branch_code="BN01"
    )
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_abx_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Products: Antibiotic (Access tier) and Non-Antibiotic (OTC)
    prod_cefix = MedicineProduct.objects.create(
        product_id=f"prod-cefix-{uid}",
        brand_name="Cef-3 200mg Capsule",
        generic_name="Cefixime",
        dosage_form="Capsule",
        strength="200mg",
        is_antibiotic=True,
        is_prescription_only=True,
        mrp_unit=Decimal("40.00"),
        cost_unit=Decimal("30.00"),
    )
    prod_paracetamol = MedicineProduct.objects.create(
        product_id=f"prod-para-{uid}",
        brand_name="Napa 500mg",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("1.20"),
        cost_unit=Decimal("0.80"),
    )

    # POS Sale Order & Sale Line
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-abx-{uid}",
        tenant=ten_a,
        branch=br_a,
        cashier_user=cashier_user_a,
        order_number=f"SO-ABX-{uid.upper()}",
    )
    sale_line = PosSaleOrderLine.objects.create(
        line_id=f"line-abx-{uid}",
        tenant=ten_a,
        branch=br_a,
        sale=sale,
        line_number=1,
        product=prod_cefix,
        packaging_unit=PosPackagingUnit.STRIP,
        quantity_units=1,
        conversion_factor=10,
        base_units_total=10,
        unit_price_bdt=Decimal("40.00"),
        unit_cost_bdt=Decimal("30.00"),
        line_subtotal_bdt=Decimal("400.00"),
        line_total_bdt=Decimal("400.00"),
    )

    # Verified Prescription
    rx = PrescriptionService.create_prescription(
        pharmacist_user_a,
        doctor_name="Dr. Moniruzzaman",
        doctor_bmdc_no="BMDC-77412",
        doctor_workplace="Dhaka Medical College Hospital",
        patient_token=f"PT-ABX-{uid}",
        prescription_date=date.today() - timedelta(days=1),
        expiry_date=date.today() + timedelta(days=20),
        items_data=[{"product_id": prod_cefix.product_id, "quantity_prescribed": 10}],
    )
    rx.status = PrescriptionStatus.VERIFIED
    rx.pharmacist_pcb_no = staff_pharmacist_a.pcb_registration_no
    rx.save(update_fields=["status", "pharmacist_pcb_no"])

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "pharmacist_a": pharmacist_user_a,
        "staff_pharmacist_a": staff_pharmacist_a,
        "cashier_a": cashier_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "pharmacist_b": pharmacist_user_b,
        "prod_cefix": prod_cefix,
        "prod_paracetamol": prod_paracetamol,
        "sale": sale,
        "sale_line": sale_line,
        "prescription": rx,
    }


@pytest.mark.django_db
def test_p246_valid_antibiotic_register_recording(abx_env):
    """
    Scenario 1: Accurate recording of statutory antibiotic dispense register entry
    with cryptographic SHA-256 integrity hash.
    """
    env = abx_env
    user = env["pharmacist_a"]

    entry = AntibioticRegisterLedgerService.record_dispense_entry(
        user,
        tenant_id=env["tenant_a"].tenant_id,
        branch_id=env["branch_a"].branch_id,
        sale=env["sale"],
        sale_line=env["sale_line"],
        product=env["prod_cefix"],
        dispensed_base_units=10,
        prescription=env["prescription"],
        pharmacist_pcb_no=env["staff_pharmacist_a"].pcb_registration_no,
        dosage_instructions="1 cap twice daily after meal",
        duration_days=5,
    )

    assert entry.register_entry_id.startswith("abxreg-")
    assert entry.status == AntibioticRegisterStatus.ACTIVE
    assert entry.dispensed_base_units == 10
    assert entry.returned_base_units == 0
    assert entry.product == env["prod_cefix"]
    assert entry.doctor_bmdc_no == "BMDC-77412"
    assert entry.approving_pharmacist_pcb_no == "PCB-ABX-8821"
    assert len(entry.sha256_hash) == 64  # Valid SHA-256 hex string


@pytest.mark.django_db
def test_p246_incomplete_input_validation(abx_env):
    """
    Scenario 2: Incomplete inputs (missing PCB, non-antibiotic product, missing Rx) are rejected.
    """
    env = abx_env
    user = env["pharmacist_a"]

    # 1. Missing PCB number
    with pytest.raises(Exception) as exc_pcb:
        AntibioticRegisterLedgerService.record_dispense_entry(
            user,
            tenant_id=env["tenant_a"].tenant_id,
            branch_id=env["branch_a"].branch_id,
            sale=env["sale"],
            sale_line=env["sale_line"],
            product=env["prod_cefix"],
            dispensed_base_units=10,
            prescription=env["prescription"],
            pharmacist_pcb_no="",  # Missing PCB
        )
    assert "pharmacist_pcb_no" in str(exc_pcb.value)

    # 2. Non-antibiotic product
    with pytest.raises(Exception) as exc_non_abx:
        AntibioticRegisterLedgerService.record_dispense_entry(
            user,
            tenant_id=env["tenant_a"].tenant_id,
            branch_id=env["branch_a"].branch_id,
            sale=env["sale"],
            sale_line=env["sale_line"],
            product=env["prod_paracetamol"],  # Not an antibiotic
            dispensed_base_units=10,
            prescription=env["prescription"],
            pharmacist_pcb_no="PCB-9921",
        )
    assert "product" in str(exc_non_abx.value)

    # 3. Missing prescription
    with pytest.raises(Exception) as exc_rx:
        AntibioticRegisterLedgerService.record_dispense_entry(
            user,
            tenant_id=env["tenant_a"].tenant_id,
            branch_id=env["branch_a"].branch_id,
            sale=env["sale"],
            sale_line=env["sale_line"],
            product=env["prod_cefix"],
            dispensed_base_units=10,
            prescription=None,  # Missing Rx
            pharmacist_pcb_no="PCB-9921",
        )
    assert "prescription" in str(exc_rx.value)


@pytest.mark.django_db
def test_p246_partial_return_and_full_reversal(abx_env):
    """
    Scenario 3: Audited sales return updates returned units and ledger state.
    """
    env = abx_env
    user = env["pharmacist_a"]

    entry = AntibioticRegisterLedgerService.record_dispense_entry(
        user,
        tenant_id=env["tenant_a"].tenant_id,
        branch_id=env["branch_a"].branch_id,
        sale=env["sale"],
        sale_line=env["sale_line"],
        product=env["prod_cefix"],
        dispensed_base_units=10,
        prescription=env["prescription"],
        pharmacist_pcb_no=env["staff_pharmacist_a"].pcb_registration_no,
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # 1. Partial Return of 3 units
    rev_res = client.post(
        f"/api/v2/pos/antibiotic-register/{entry.register_entry_id}/reverse/",
        {"return_base_units": 3, "reversal_reason": "Customer return: adverse reaction reported"},
        format="json",
    )
    assert rev_res.status_code == status.HTTP_200_OK
    assert rev_res.data["status"] == AntibioticRegisterStatus.PARTIALLY_RETURNED
    assert rev_res.data["returned_base_units"] == 3

    # 2. Return remaining 7 units -> Full reversal
    rev_res_2 = client.post(
        f"/api/v2/pos/antibiotic-register/{entry.register_entry_id}/reverse/",
        {"return_base_units": 7, "reversal_reason": "Remaining course returned and quarantined"},
        format="json",
    )
    assert rev_res_2.status_code == status.HTTP_200_OK
    assert rev_res_2.data["status"] == AntibioticRegisterStatus.REVERSED
    assert rev_res_2.data["returned_base_units"] == 10

    entry.refresh_from_db()
    assert entry.status == AntibioticRegisterStatus.REVERSED
    assert entry.returned_base_units == 10


@pytest.mark.django_db
def test_p246_dgda_compliance_ledger_export_and_summary(abx_env):
    """
    Scenario 4: DGDA statutory export produces privacy-safe records and AWaRe breakdown.
    """
    env = abx_env
    user = env["pharmacist_a"]

    # Record entry
    AntibioticRegisterLedgerService.record_dispense_entry(
        user,
        tenant_id=env["tenant_a"].tenant_id,
        branch_id=env["branch_a"].branch_id,
        sale=env["sale"],
        sale_line=env["sale_line"],
        product=env["prod_cefix"],
        dispensed_base_units=10,
        prescription=env["prescription"],
        pharmacist_pcb_no=env["staff_pharmacist_a"].pcb_registration_no,
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # 1. DGDA Export endpoint
    export_res = client.get("/api/v2/pos/antibiotic-register/dgda-export/")
    assert export_res.status_code == status.HTTP_200_OK
    data = export_res.data

    assert data["total_records"] >= 1
    assert data["total_dispensed_units"] >= 10
    assert data["aware_tier_breakdown"][PosRegulatoryClass.ANTIMICROBIAL_ACCESS] >= 1

    rec = data["records"][0]
    assert rec["product_name"] == "Cef-3 200mg Capsule"
    assert rec["doctor_bmdc_no"] == "BMDC-77412"
    assert rec["pharmacist_pcb_no"] == "PCB-ABX-8821"
    assert rec["patient_token"].startswith("PT-")

    # 2. Summary endpoint
    sum_res = client.get("/api/v2/pos/antibiotic-register/summary/")
    assert sum_res.status_code == status.HTTP_200_OK
    assert sum_res.data["total_records"] >= 1


@pytest.mark.django_db
def test_p246_unauthorized_and_cross_tenant_reversal_blocked(abx_env):
    """
    Scenario 5: Cashier (unauthorized) and cross-tenant user are blocked
    from reversing antibiotic register records.
    """
    env = abx_env
    cashier = env["cashier_a"]
    pharmacist_b = env["pharmacist_b"]

    entry = AntibioticRegisterLedgerService.record_dispense_entry(
        env["pharmacist_a"],
        tenant_id=env["tenant_a"].tenant_id,
        branch_id=env["branch_a"].branch_id,
        sale=env["sale"],
        sale_line=env["sale_line"],
        product=env["prod_cefix"],
        dispensed_base_units=10,
        prescription=env["prescription"],
        pharmacist_pcb_no=env["staff_pharmacist_a"].pcb_registration_no,
    )

    # 1. Cashier attempts to reverse -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=cashier)
    res_cashier = client_cashier.post(
        f"/api/v2/pos/antibiotic-register/{entry.register_entry_id}/reverse/",
        {"reversal_reason": "Cashier unauthorized reversal"},
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Cross-tenant user attempts to access -> 404/403
    client_b = APIClient()
    client_b.force_authenticate(user=pharmacist_b)
    res_b = client_b.post(
        f"/api/v2/pos/antibiotic-register/{entry.register_entry_id}/reverse/",
        {"reversal_reason": "Cross tenant attempt"},
        format="json",
    )
    assert res_b.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
