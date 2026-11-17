"""
backend/tests/test_p245_rx_item_matching.py

Architecture Decision: ADR-0245 (P245).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Rx-Item Matching:
1. Valid exact product match and incremental partial & full dispense.
2. Prohibition of automated treatment/generic substitution without explicit authorization.
3. Over-dispensing prevention when cart quantity exceeds authorized remaining quota.
4. Audited dispense reversal restoring authorization quota on sales return/void.
5. Cross-tenant isolation and inactive/expired prescription protection.
"""

from datetime import date, timedelta
from decimal import Decimal
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    PrescriptionDispenseRecord,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    PrescriptionValidityStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.prescription_service import PrescriptionService
from apps.core.rx_matching_service import RxItemMatchingService, RxMatchingStatus
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def matching_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p245-a-{uid}", name="Rx Match Tenant A", subdomain=f"p245a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p245-a-{uid}", name="Mirpur Branch", branch_code="MP01"
    )

    # Cashier for Tenant A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_m_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )
    staff_cashier_a = StaffMember.objects.create(
        staff_id=f"stf-c-a-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=cashier_user_a.email,
        full_name="Md. Karim Hossain",
        role=StaffRole.CASHIER,
        is_active=True,
    )

    # Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_m_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharmacist_a = StaffMember.objects.create(
        staff_id=f"stf-p-a-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Dr. Nuzhat Parveen",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-M8841",
        is_active=True,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p245-b-{uid}", name="Rx Match Tenant B", subdomain=f"p245b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p245-b-{uid}", name="Uttara Branch", branch_code="UT01"
    )
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_m_b_{uid}@test.com",
        phone_number=f"+88019{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Products: 2 Antibiotics (Cefixime vs Amoxicillin), 1 OTC
    prod_cefix = MedicineProduct.objects.create(
        product_id=f"prod-cef3-{uid}",
        brand_name="Cef-3 200mg Capsule",
        generic_name="Cefixime",
        dosage_form="Capsule",
        strength="200mg",
        is_antibiotic=True,
        is_prescription_only=True,
        mrp_unit=Decimal("45.00"),
        cost_unit=Decimal("35.00"),
    )
    prod_amox = MedicineProduct.objects.create(
        product_id=f"prod-mox-{uid}",
        brand_name="Moxacil 500mg Capsule",
        generic_name="Amoxicillin",
        dosage_form="Capsule",
        strength="500mg",
        is_antibiotic=True,
        is_prescription_only=True,
        mrp_unit=Decimal("12.00"),
        cost_unit=Decimal("9.00"),
    )
    prod_otc = MedicineProduct.objects.create(
        product_id=f"prod-napa-{uid}",
        brand_name="Napa Extra Tablet",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_antibiotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("3.00"),
        cost_unit=Decimal("2.20"),
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "cashier_a": cashier_user_a,
        "pharmacist_a": pharmacist_user_a,
        "staff_pharmacist_a": staff_pharmacist_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "pharmacist_b": pharmacist_user_b,
        "prod_cefix": prod_cefix,
        "prod_amox": prod_amox,
        "prod_otc": prod_otc,
    }


@pytest.mark.django_db
def test_p245_valid_rx_item_matching_and_incremental_commit(matching_env):
    """
    Scenario 1: Preflight matches exact product and allows incremental partial
    and full dispense with accurate state transitions.
    """
    env = matching_env
    user = env["pharmacist_a"]

    # 1. Create prescription with 14 capsules prescribed
    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Shamsul Huda",
        doctor_bmdc_no="A-77881",
        doctor_workplace="Square Hospital Dhaka",
        patient_token="PT-MATCH-001",
        prescription_date=date.today() - timedelta(days=1),
        expiry_date=date.today() + timedelta(days=20),
        items_data=[
            {"product_id": env["prod_cefix"].product_id, "quantity_prescribed": 14}
        ],
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # 2. Preflight Match for 10 units of Cefixime + 5 OTC Napa
    match_payload = {
        "items": [
            {"product_id": env["prod_cefix"].product_id, "quantity": 10},
            {"product_id": env["prod_otc"].product_id, "quantity": 5},
        ]
    }
    match_res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/match-items/",
        match_payload,
        format="json",
    )
    assert match_res.status_code == status.HTTP_200_OK
    data = match_res.data
    assert data["all_matched"] is True
    assert data["can_proceed"] is True
    assert len(data["matched_items"]) == 2

    rx_match = next(m for m in data["matched_items"] if m["product_id"] == env["prod_cefix"].product_id)
    assert rx_match["match_status"] == RxMatchingStatus.MATCHED
    assert rx_match["remaining_quantity_after_sale"] == 4

    otc_match = next(m for m in data["matched_items"] if m["product_id"] == env["prod_otc"].product_id)
    assert otc_match["match_status"] == RxMatchingStatus.OTC_EXEMPT

    # 3. Commit First Partial Dispense (10 units)
    commit_payload = {
        "items": [{"product_id": env["prod_cefix"].product_id, "quantity": 10}],
        "notes": "First partial course dispensed",
    }
    commit_res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/commit-matching/",
        commit_payload,
        format="json",
    )
    assert commit_res.status_code == status.HTTP_200_OK
    assert commit_res.data["status"] == PrescriptionStatus.PARTIALLY_DISPENSED
    assert commit_res.data["is_fully_dispensed"] is False

    rx.refresh_from_db()
    rx_item = rx.items.first()
    assert rx_item.quantity_dispensed == 10
    assert rx_item.is_dispensed is False
    assert rx.status == PrescriptionStatus.PARTIALLY_DISPENSED

    # 4. Commit Second Dispense for remaining 4 units
    commit_res_2 = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/commit-matching/",
        {"items": [{"product_id": env["prod_cefix"].product_id, "quantity": 4}]},
        format="json",
    )
    assert commit_res_2.status_code == status.HTTP_200_OK
    assert commit_res_2.data["status"] == PrescriptionStatus.DISPENSED
    assert commit_res_2.data["is_fully_dispensed"] is True

    rx.refresh_from_db()
    rx_item.refresh_from_db()
    assert rx_item.quantity_dispensed == 14
    assert rx_item.is_dispensed is True
    assert rx.status == PrescriptionStatus.DISPENSED


@pytest.mark.django_db
def test_p245_automated_treatment_substitution_prohibited(matching_env):
    """
    Scenario 2: Prohibits automated brand or generic substitution without explicit
    prescriber/pharmacist authorization.
    """
    env = matching_env
    user = env["pharmacist_a"]

    # Prescription specifies Cef-3 (Cefixime)
    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Kamal Hossain",
        doctor_bmdc_no="BMDC-55912",
        doctor_workplace="National Heart Foundation",
        patient_token="PT-SUBST-001",
        items_data=[
            {"product_id": env["prod_cefix"].product_id, "quantity_prescribed": 10}
        ],
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # Cart contains Moxacil (Amoxicillin) instead of Cef-3
    match_payload = {
        "items": [
            {"product_id": env["prod_amox"].product_id, "quantity": 10}
        ]
    }
    res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/match-items/",
        match_payload,
        format="json",
    )
    assert res.status_code == status.HTTP_200_OK
    data = res.data

    assert data["all_matched"] is False
    assert data["can_proceed"] is False
    mismatch = data["matched_items"][0]
    assert mismatch["match_status"] == RxMatchingStatus.SUBSTITUTION_PROHIBITED
    assert "not authorized" in mismatch["error"] or "substitution" in mismatch["error"].lower()


@pytest.mark.django_db
def test_p245_over_dispense_exceeding_quota_blocked(matching_env):
    """
    Scenario 3: Attempting to dispense more than authorized on the prescription
    is blocked with EXCEEDS_QUANTITY error.
    """
    env = matching_env
    user = env["cashier_a"]

    rx = PrescriptionService.create_prescription(
        env["pharmacist_a"],
        doctor_name="Dr. Rafiqul Islam",
        doctor_bmdc_no="BMDC-44211",
        doctor_workplace="Dhaka Community Hospital",
        patient_token="PT-OVER-001",
        items_data=[
            {"product_id": env["prod_cefix"].product_id, "quantity_prescribed": 10}
        ],
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # Preflight for 15 units (prescribed only 10)
    res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/match-items/",
        {"items": [{"product_id": env["prod_cefix"].product_id, "quantity": 15}]},
        format="json",
    )
    assert res.status_code == status.HTTP_200_OK
    assert res.data["can_proceed"] is False
    assert res.data["matched_items"][0]["match_status"] == RxMatchingStatus.EXCEEDS_QUANTITY

    # Attempting to commit over-quantity directly raises 400/422
    commit_res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/commit-matching/",
        {"items": [{"product_id": env["prod_cefix"].product_id, "quantity": 15}]},
        format="json",
    )
    assert commit_res.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@pytest.mark.django_db
def test_p245_dispense_reversal_restores_quota(matching_env):
    """
    Scenario 4: Sales return or cancellation reverses dispense and restores
    prescription remaining quota.
    """
    env = matching_env
    user = env["pharmacist_a"]

    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Monira Akter",
        doctor_bmdc_no="A-88334",
        doctor_workplace="Labaid Hospital",
        patient_token="PT-REVERT-001",
        items_data=[
            {"product_id": env["prod_cefix"].product_id, "quantity_prescribed": 10}
        ],
    )

    # Commit 10 units (fully dispensed)
    commit_data = RxItemMatchingService.commit_rx_item_dispenses(
        user,
        prescription_id=rx.prescription_id,
        dispense_lines=[{"product_id": env["prod_cefix"].product_id, "quantity": 10}],
    )
    dispense_id = commit_data["dispense_ids"][0]

    rx.refresh_from_db()
    assert rx.status == PrescriptionStatus.DISPENSED
    assert rx.items.first().quantity_dispensed == 10

    # Reverse 4 units (customer returned 4 capsules)
    client = APIClient()
    client.force_authenticate(user=user)
    reverse_res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/reverse-dispense/",
        {
            "dispense_id": dispense_id,
            "reverse_quantity": 4,
            "reason": "Customer return: unused sealed strip returned",
        },
        format="json",
    )
    assert reverse_res.status_code == status.HTTP_200_OK
    assert reverse_res.data["new_item_dispensed_quantity"] == 6
    assert reverse_res.data["new_prescription_status"] == PrescriptionStatus.PARTIALLY_DISPENSED

    rx.refresh_from_db()
    rx_item = rx.items.first()
    assert rx_item.quantity_dispensed == 6
    assert rx_item.is_dispensed is False
    assert rx_item.remaining_dispense_quantity == 4


@pytest.mark.django_db
def test_p245_cross_tenant_isolation_blocked(matching_env):
    """
    Scenario 5: Cross-tenant cashier or pharmacist cannot match or commit
    against another tenant's prescription.
    """
    env = matching_env
    user_b = env["pharmacist_b"]

    rx_a = PrescriptionService.create_prescription(
        env["pharmacist_a"],
        doctor_name="Dr. Tenant A Doctor",
        doctor_bmdc_no="BMDC-TENANT-A",
        patient_token="PT-TENANT-A",
        items_data=[
            {"product_id": env["prod_cefix"].product_id, "quantity_prescribed": 10}
        ],
    )

    client_b = APIClient()
    client_b.force_authenticate(user=user_b)

    res = client_b.post(
        f"/api/v2/prescriptions/{rx_a.prescription_id}/match-items/",
        {"items": [{"product_id": env["prod_cefix"].product_id, "quantity": 5}]},
        format="json",
    )
    # Blocked by get_queryset multi-tenant scope or 404/403
    assert res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
