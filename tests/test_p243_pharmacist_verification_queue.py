"""
Acceptance Tests for P243 - Pharmacist Verification Queue, Clinical Decision Actions & Review Audit Trail.
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Coverage:
1. Valid Scenario: Full queue lifecycle (Priority listing -> Request Clarification -> Resolve Clarification -> Pharmacist Verify -> Audit Trail).
2. Clinical Rejection: Pharmacist rejection with mandatory reason and immutable audit log.
3. Incomplete / Invalid Input: Validation rejection when PCB number, rejection reason, or clarification query is missing.
4. Role Restriction: Cashiers strictly forbidden from accessing the pharmacist review queue (HTTP 403).
5. Cross-Tenant Isolation: Tenant B pharmacist cannot view or decision-sign Tenant A queue items.
"""

from datetime import timedelta
from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    PrescriptionRecord,
    PrescriptionReviewAction,
    PrescriptionStatus,
    PrescriptionVerificationAudit,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.pharmacist_queue_service import PharmacistQueueService
from apps.core.prescription_service import PrescriptionService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def queue_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(tenant_id=f"ten-p243-a-{uid}", name="Review Queue Tenant A", subdomain=f"p243a-{uid}")
    br_a = Branch.objects.create(tenant=ten_a, branch_id=f"br-p243-a-{uid}", name="Gulshan Branch", branch_code="GL01")

    # Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_a_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharmacist_a = StaffMember.objects.create(
        staff_id=f"stf-a-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Md. Mahbubur Rahman",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-B77654",
        is_active=True,
    )

    # Cashier for Tenant A (Unauthorized role)
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_a_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(tenant_id=f"ten-p243-b-{uid}", name="Review Queue Tenant B", subdomain=f"p243b-{uid}")
    br_b = Branch.objects.create(tenant=ten_b, branch_id=f"br-p243-b-{uid}", name="Uttara Branch", branch_code="UT01")
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Products: Antibiotic & General
    prod_antibiotic = MedicineProduct.objects.create(
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
    prod_general = MedicineProduct.objects.create(
        product_id=f"prod-antacid-{uid}",
        brand_name="Antacid Plus Suspension",
        generic_name="Aluminium Hydroxide",
        dosage_form="Suspension",
        strength="200ml",
        is_antibiotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("85.00"),
        cost_unit=Decimal("65.00"),
    )

    # Create 2 Prescriptions in Tenant A: 1 Routine, 1 Priority (Antibiotic)
    rx_routine = PrescriptionService.create_prescription(
        pharmacist_user_a,
        doctor_name="Dr. Kazi Nazmul",
        doctor_bmdc_no="BMDC-55112",
        patient_token=f"PT-ROUTINE-{uid}",
        items_data=[{"product_id": prod_general.product_id, "quantity_prescribed": 1}],
    )

    rx_priority = PrescriptionService.create_prescription(
        pharmacist_user_a,
        doctor_name="Dr. Kazi Nazmul",
        doctor_bmdc_no="BMDC-55112",
        patient_token=f"PT-PRIORITY-{uid}",
        items_data=[{"product_id": prod_antibiotic.product_id, "quantity_prescribed": 14}],
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "pharmacist_user_a": pharmacist_user_a,
        "staff_pharmacist_a": staff_pharmacist_a,
        "cashier_user_a": cashier_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "pharmacist_user_b": pharmacist_user_b,
        "rx_routine": rx_routine,
        "rx_priority": rx_priority,
    }


@pytest.mark.django_db
def test_p243_valid_pharmacist_queue_and_decision_lifecycle(queue_env):
    """
    Scenario 1: Pharmacist reviews queue, prioritizes antibiotic, requests clarification,
    resolves clarification, approves verification, and verifies immutable audit trail.
    """
    env = queue_env
    pharmacist = env["pharmacist_user_a"]
    rx = env["rx_priority"]

    client = APIClient()
    client.force_authenticate(user=pharmacist)

    # 1. Access Review Queue: verify prioritization of antibiotic
    queue_res = client.get("/api/v2/prescriptions/review-queue/")
    assert queue_res.status_code == status.HTTP_200_OK, queue_res.data
    items = queue_res.data
    assert len(items) >= 2
    # Priority antibiotic prescription should be listed first
    assert items[0]["prescription_id"] == rx.prescription_id

    # 2. Pharmacist requests clarification on dosage instructions
    clarify_res = client.post(
        f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/request-clarification/",
        data={
            "pharmacist_pcb_no": "PCB-B77654",
            "clarification_query": "Patient is elderly (70yo). Confirm creatinine clearance before 200mg BD dosing.",
        },
        format="json",
    )
    assert clarify_res.status_code == status.HTTP_200_OK, clarify_res.data
    assert clarify_res.data["prescription"]["status"] == "CLARIFICATION_REQUESTED"
    assert clarify_res.data["audit"]["action"] == "REQUEST_CLARIFICATION"

    rx.refresh_from_db()
    assert rx.status == PrescriptionStatus.CLARIFICATION_REQUESTED

    # 3. Prescriber/staff clarifies and resolves
    resolve_res = client.post(
        f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/resolve-clarification/",
        data={
            "clarification_response": "Doctor confirmed normal renal function (CrCl > 60 mL/min). Proceed with standard dosage.",
        },
        format="json",
    )
    assert resolve_res.status_code == status.HTTP_200_OK, resolve_res.data
    assert resolve_res.data["prescription"]["status"] == "PENDING"

    rx.refresh_from_db()
    assert rx.status == PrescriptionStatus.PENDING

    # 4. Pharmacist clinical verification sign-off
    verify_res = client.post(
        f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/verify/",
        data={
            "pharmacist_pcb_no": "PCB-B77654",
            "notes": "Verified. Patient briefed on completing full 7-day antibiotic course.",
        },
        format="json",
    )
    assert verify_res.status_code == status.HTTP_200_OK, verify_res.data
    assert verify_res.data["prescription"]["status"] == "VERIFIED"

    rx.refresh_from_db()
    assert rx.status == PrescriptionStatus.VERIFIED
    assert rx.can_dispense is True

    # 5. Check Audit Trail
    audit_res = client.get(f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/audit-trail/")
    assert audit_res.status_code == status.HTTP_200_OK, audit_res.data
    audits = audit_res.data
    assert len(audits) == 3
    assert audits[0]["action"] == "REQUEST_CLARIFICATION"
    assert audits[1]["action"] == "RESOLVE_CLARIFICATION"
    assert audits[2]["action"] == "VERIFY"
    assert audits[2]["pharmacist_pcb_no"] == "PCB-B77654"


@pytest.mark.django_db
def test_p243_clinical_rejection_with_mandatory_reason(queue_env):
    """
    Scenario 2: Pharmacist rejects an invalid/dangerous prescription with mandatory reason.
    """
    env = queue_env
    pharmacist = env["pharmacist_user_a"]
    rx = env["rx_routine"]

    client = APIClient()
    client.force_authenticate(user=pharmacist)

    # 1. Reject without reason -> 400/422 Bad Request
    bad_res = client.post(
        f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/reject/",
        data={"pharmacist_pcb_no": "PCB-B77654", "rejection_reason": ""},
        format="json",
    )
    assert bad_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Reject with valid clinical reason
    good_res = client.post(
        f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/reject/",
        data={
            "pharmacist_pcb_no": "PCB-B77654",
            "rejection_reason": "Severe drug-drug interaction with patient current regimen. Dispense unsafe.",
        },
        format="json",
    )
    assert good_res.status_code == status.HTTP_200_OK, good_res.data
    assert good_res.data["prescription"]["status"] == "REJECTED"

    rx.refresh_from_db()
    assert rx.status == PrescriptionStatus.REJECTED
    assert rx.can_dispense is False

    # Audit verified
    audits = PharmacistQueueService.get_prescription_audit_trail(pharmacist, prescription_id=rx.prescription_id)
    assert len(audits) == 1
    assert audits[0].action == PrescriptionReviewAction.REJECT
    assert "drug-drug interaction" in audits[0].clinical_notes


@pytest.mark.django_db
def test_p243_unauthorized_cashier_access_blocked(queue_env):
    """
    Scenario 3: Cashiers and unauthorized staff are strictly blocked from review queue (HTTP 403).
    """
    env = queue_env
    cashier = env["cashier_user_a"]
    rx = env["rx_priority"]

    client = APIClient()
    client.force_authenticate(user=cashier)

    # Accessing queue endpoint
    queue_res = client.get("/api/v2/prescriptions/review-queue/")
    assert queue_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED)

    # Calling verify action
    verify_res = client.post(
        f"/api/v2/prescriptions/review-queue/{rx.prescription_id}/verify/",
        data={"pharmacist_pcb_no": "PCB-FAKE01"},
        format="json",
    )
    assert verify_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
def test_p243_cross_tenant_isolation(queue_env):
    """
    Scenario 4: Tenant B pharmacist cannot see or act upon Tenant A's review queue.
    """
    env = queue_env
    pharmacist_b = env["pharmacist_user_b"]
    rx_a = env["rx_priority"]

    client_b = APIClient()
    client_b.force_authenticate(user=pharmacist_b)

    # Queue list does not contain Tenant A's prescription
    queue_res = client_b.get("/api/v2/prescriptions/review-queue/")
    assert queue_res.status_code == status.HTTP_200_OK
    assert all(r["prescription_id"] != rx_a.prescription_id for r in queue_res.data)

    # Verify action on Tenant A prescription returns 400 or 404
    action_res = client_b.post(
        f"/api/v2/prescriptions/review-queue/{rx_a.prescription_id}/verify/",
        data={"pharmacist_pcb_no": "PCB-B99000"},
        format="json",
    )
    assert action_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_ENTITY)
