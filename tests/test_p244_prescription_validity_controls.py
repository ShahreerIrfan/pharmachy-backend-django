"""
backend/tests/test_p244_prescription_validity_controls.py

Architecture Decision: ADR-0244 (P244).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Prescription Validity Controls:
1. Valid prescription validity evaluation (BMDC, dates, supporting scan, items).
2. Expired prescription detection and automated/pharmacist rejection.
3. Supporting document completeness check for restricted & antibiotic medications.
4. Dummy prescriber credentials rejection (BMDC formatting & workplace).
5. Unauthorized cashier rejection block and cross-tenant isolation.
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
    PrescriptionAttachment,
    PrescriptionRecord,
    PrescriptionReviewAction,
    PrescriptionStatus,
    PrescriptionValidityStatus,
    PrescriptionVerificationAudit,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.prescription_service import PrescriptionService
from apps.core.prescription_upload_service import PrescriptionUploadService
from apps.core.prescription_validity_service import PrescriptionValidityService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def validity_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p244-a-{uid}", name="Validity Tenant A", subdomain=f"p244a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p244-a-{uid}", name="Dhanmondi Branch", branch_code="DN01"
    )

    # Pharmacist for Tenant A
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_v_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharmacist_a = StaffMember.objects.create(
        staff_id=f"stf-v-a-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Dr. Mahbubul Alam",
        role=StaffRole.PHARMACIST,
        pcb_registration_no="PCB-V9921",
        is_active=True,
    )

    # Cashier for Tenant A (Unauthorized for clinical rejection)
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_v_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p244-b-{uid}", name="Validity Tenant B", subdomain=f"p244b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p244-b-{uid}", name="Banani Branch", branch_code="BN01"
    )
    pharmacist_user_b = PharmacyUser.objects.create_user(
        email=f"pharmacist_v_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Products
    prod_antibiotic = MedicineProduct.objects.create(
        product_id=f"prod-amox-{uid}",
        brand_name="Moxacil 500mg Capsule",
        generic_name="Amoxicillin",
        dosage_form="Capsule",
        strength="500mg",
        is_antibiotic=True,
        is_prescription_only=True,
        mrp_unit=Decimal("12.50"),
        cost_unit=Decimal("9.00"),
    )
    prod_otc = MedicineProduct.objects.create(
        product_id=f"prod-para-{uid}",
        brand_name="Napa 500mg Tablet",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("1.20"),
        cost_unit=Decimal("0.80"),
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "pharmacist_a": pharmacist_user_a,
        "staff_pharmacist_a": staff_pharmacist_a,
        "cashier_a": cashier_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "pharmacist_b": pharmacist_user_b,
        "prod_antibiotic": prod_antibiotic,
        "prod_otc": prod_otc,
    }


@pytest.mark.django_db
def test_p244_valid_prescription_evaluation_lifecycle(validity_env):
    """
    Scenario 1: Valid prescription with verified BMDC doctor, non-expired dates,
    and valid supporting document upload passes all validity checks.
    """
    env = validity_env
    user = env["pharmacist_a"]

    # 1. Create prescription
    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Ashraful Haque",
        doctor_bmdc_no="A-45210",
        doctor_workplace="Dhaka Medical College Hospital",
        patient_token="PT-VALID-001",
        prescription_date=date.today() - timedelta(days=2),
        expiry_date=date.today() + timedelta(days=28),
        items_data=[
            {"product_id": env["prod_antibiotic"].product_id, "quantity_prescribed": 15}
        ],
    )

    # Upload supporting scan (minimum 100 bytes)
    mock_pdf = b"%PDF-1.4 mock scan data for clinical evidence " + (b"ATTACHMENT_VALIDATION_BYTES_" * 5)
    PrescriptionUploadService.upload_attachment(
        user,
        prescription_id=rx.prescription_id,
        filename="rx_scan.pdf",
        content=mock_pdf,
        declared_mime="application/pdf",
        notes="Official hospital prescription pad scan",
    )

    # 2. Evaluate validity via API
    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/v2/prescriptions/{rx.prescription_id}/check-validity/")
    assert res.status_code == status.HTTP_200_OK
    data = res.data

    assert data["is_valid"] is True
    assert data["validity_status"] == PrescriptionValidityStatus.VALID
    assert data["prescriber_check"]["status"] == "PASS"
    assert data["prescriber_check"]["bmdc_valid"] is True
    assert data["temporal_check"]["status"] == "PASS"
    assert data["temporal_check"]["is_expired"] is False
    assert data["supporting_document_check"]["status"] == "PASS"
    assert data["supporting_document_check"]["has_valid_attachment"] is True
    assert len(data["overall_errors"]) == 0

    # Verify model record updated
    rx.refresh_from_db()
    assert rx.validity_status == PrescriptionValidityStatus.VALID
    assert rx.validity_checked_at is not None


@pytest.mark.django_db
def test_p244_expired_prescription_detection_and_rejection(validity_env):
    """
    Scenario 2: Prescription with past expiry date is flagged as EXPIRED and
    can be explicitly rejected with clinical audit log.
    """
    env = validity_env
    user = env["pharmacist_a"]

    # Create prescription expired 5 days ago
    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Farhana Islam",
        doctor_bmdc_no="B-99812",
        doctor_workplace="Square Hospital",
        patient_token="PT-EXPIRED-001",
        prescription_date=date.today() - timedelta(days=35),
        expiry_date=date.today() - timedelta(days=5),
        items_data=[{"product_id": env["prod_otc"].product_id, "quantity_prescribed": 10}],
    )

    client = APIClient()
    client.force_authenticate(user=user)

    # Check validity
    res = client.get(f"/api/v2/prescriptions/{rx.prescription_id}/check-validity/")
    assert res.status_code == status.HTTP_200_OK
    data = res.data
    assert data["is_valid"] is False
    assert data["validity_status"] == PrescriptionValidityStatus.EXPIRED
    assert data["temporal_check"]["is_expired"] is True

    # Reject via reject-validity endpoint
    reject_res = client.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/reject-validity/",
        {"reason": "Prescription validity elapsed 5 days ago", "clinical_notes": "Patient advised to consult physician"},
        format="json",
    )
    assert reject_res.status_code == status.HTTP_200_OK
    assert reject_res.data["status"] == PrescriptionStatus.REJECTED

    rx.refresh_from_db()
    assert rx.status == PrescriptionStatus.REJECTED

    # Verify audit record
    audit = PrescriptionVerificationAudit.objects.filter(prescription=rx, action=PrescriptionReviewAction.REJECT).first()
    assert audit is not None
    assert "elapsed 5 days ago" in audit.clinical_notes
    assert audit.pharmacist_pcb_no == env["staff_pharmacist_a"].pcb_registration_no


@pytest.mark.django_db
def test_p244_missing_supporting_document_for_antibiotic_flagged(validity_env):
    """
    Scenario 3: Antibiotic prescription missing scanned/uploaded evidence
    is flagged as MISSING_DOCUMENTS under DGDA policy until uploaded.
    """
    env = validity_env
    user = env["pharmacist_a"]

    rx = PrescriptionService.create_prescription(
        user,
        doctor_name="Dr. Monirul Hasan",
        doctor_bmdc_no="BMDC-77441",
        doctor_workplace="Popular Diagnostic Center",
        patient_token="PT-ANTIBIOTIC-001",
        prescription_date=date.today() - timedelta(days=1),
        expiry_date=date.today() + timedelta(days=7),
        items_data=[{"product_id": env["prod_antibiotic"].product_id, "quantity_prescribed": 14}],
    )

    # Validity check without attachments
    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/v2/prescriptions/{rx.prescription_id}/check-validity/")
    assert res.status_code == status.HTTP_200_OK
    assert res.data["is_valid"] is False
    assert res.data["validity_status"] == PrescriptionValidityStatus.MISSING_DOCUMENTS
    assert res.data["supporting_document_check"]["requires_document"] is True
    assert res.data["supporting_document_check"]["has_valid_attachment"] is False

    # Upload attachment now (minimum 100 bytes)
    mock_jpg = b"\xff\xd8\xff" + (b"JPEG_HEADER_AND_SCAN_BYTES_" * 6)
    PrescriptionUploadService.upload_attachment(
        user,
        prescription_id=rx.prescription_id,
        filename="rx_doc.jpg",
        content=mock_jpg,
        declared_mime="image/jpeg",
        notes="Prescription clear scan",
    )

    # Re-evaluate validity
    res_after = client.get(f"/api/v2/prescriptions/{rx.prescription_id}/check-validity/")
    assert res_after.status_code == status.HTTP_200_OK
    assert res_after.data["is_valid"] is True
    assert res_after.data["validity_status"] == PrescriptionValidityStatus.VALID


@pytest.mark.django_db
def test_p244_dummy_prescriber_credentials_and_rejection_matrix(validity_env):
    """
    Scenario 4: Dummy prescriber credentials (e.g. NA, TEST, 0000) fail validation.
    """
    # 1. Direct service validation
    valid_bmdc, err = PrescriptionValidityService.validate_bmdc_registration("NONE")
    assert valid_bmdc is False
    assert "placeholder" in err.lower()

    valid_bmdc_0, err_0 = PrescriptionValidityService.validate_bmdc_registration("0000")
    assert valid_bmdc_0 is False

    valid_ok, err_ok = PrescriptionValidityService.validate_bmdc_registration("A-99123")
    assert valid_ok is True
    assert err_ok is None

    # Prescriber info checks
    doc_res = PrescriptionValidityService.validate_prescriber_info(
        doctor_name="Dr.",
        doctor_bmdc_no="UNKNOWN",
        doctor_workplace="",
    )
    assert doc_res["status"] == "FAIL"
    assert len(doc_res["errors"]) >= 2


@pytest.mark.django_db
def test_p244_unauthorized_and_cross_tenant_rejection_blocked(validity_env):
    """
    Scenario 5: Cashier (unauthorized role) and cross-tenant pharmacist
    are blocked from rejecting prescriptions.
    """
    env = validity_env
    cashier = env["cashier_a"]
    pharmacist_b = env["pharmacist_b"]

    rx = PrescriptionService.create_prescription(
        env["pharmacist_a"],
        doctor_name="Dr. Standard Doctor",
        doctor_bmdc_no="BMDC-44221",
        doctor_workplace="City Hospital",
        patient_token="PT-SEC-001",
        items_data=[{"product_id": env["prod_otc"].product_id, "quantity_prescribed": 5}],
    )

    # 1. Cashier attempts to reject
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=cashier)
    res_cashier = client_cashier.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/reject-validity/",
        {"reason": "Cashier unauthorized attempt"},
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Cross-tenant pharmacist attempts to reject Tenant A's prescription
    client_b = APIClient()
    client_b.force_authenticate(user=pharmacist_b)
    res_b = client_b.post(
        f"/api/v2/prescriptions/{rx.prescription_id}/reject-validity/",
        {"reason": "Cross tenant attempt"},
        format="json",
    )
    # Filtered out by get_queryset or 404/403
    assert res_b.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
