"""
backend/tests/test_p252_pcb_registration_tracking.py

Architecture Decision: ADR-0252 (P252).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Pharmacy Personnel PCB Registration Tracking, Qualification Grades & Evidence Access Restrictions:
1. Valid scenario: Register PCB credential (Grade A) with branch assignment, certificate evidence, and SHA-256 hash.
2. Incomplete / invalid input cases: short reg number, invalid grade, valid_until earlier than issue_date.
3. Unauthorized & Cross-Tenant: Cashier blocked from managing; Tenant B blocked from accessing Tenant A's records.
4. Confidential evidence access restrictions: unredacted for Owner/Self; redacted / 403 for non-privileged staff.
5. Dynamic live validity transitions: Active -> Expired -> Pending Renewal.
6. Clinical credential verification gate and branch compliance audit summary.
"""

import datetime
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PcbEvidenceAccessLevel,
    PcbGrade,
    PcbRegistrationStatus,
    PharmacyPersonnelPcbRegistration,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def pcb_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p252-a-{uid}", name="PCB Tenant A", subdomain=f"p252a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p252-a-{uid}", name="Gulshan Branch", branch_code="GL01"
    )

    # Owner A
    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    # Branch Manager A
    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Licensed Pharmacist 1 for Tenant A (Credential Holder)
    pharmacist_user_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )
    staff_pharma_a = StaffMember.objects.create(
        staff_id=f"staff-pharma-{uid}",
        tenant=ten_a,
        branch=br_a,
        username=pharmacist_user_a.email,
        full_name="Md. Tanvir Ahmed (B.Pharm)",
        role=StaffRole.PHARMACIST,
        mobile_number=pharmacist_user_a.phone_number,
    )

    # Colleague Pharmacist 2 for Tenant A (Non-owner viewer)
    colleague_pharma_user = PharmacyUser.objects.create_user(
        email=f"colleague_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Cashier A
    cashier_user_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-tenant boundary test)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p252-b-{uid}", name="PCB Tenant B", subdomain=f"p252b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p252-b-{uid}", name="Dhanmondi Branch", branch_code="DH01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "uid": uid,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "pharmacist_user_a": pharmacist_user_a,
        "staff_pharma_a": staff_pharma_a,
        "colleague_pharma_user": colleague_pharma_user,
        "cashier_user_a": cashier_user_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p252_valid_pcb_registration_creation(pcb_env):
    """Scenario 1: Owner successfully registers staff member's Grade A PCB credential with evidence."""
    env = pcb_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])

    payload = {
        "branch_id": env["branch_a"].branch_id,
        "staff_member_id": env["staff_pharma_a"].staff_id,
        "registration_no": "A-12845",
        "registered_name": "Md. Tanvir Ahmed",
        "grade": "GRADE_A",
        "council_issue_date": "2022-01-15",
        "valid_until": "2027-01-14",
        "evidence_document_uri": "/vault/credentials/pcb/cert_a_12845.pdf",
        "evidence_file_name": "PCB_Cert_A12845.pdf",
        "evidence_mime_type": "application/pdf",
        "evidence_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "is_verified": True,
        "verification_notes": "Verified against Bangladesh Pharmacy Council online registry.",
    }

    res = client.post("/api/v2/compliance/pcb-registrations/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data

    assert data["registration_no"] == "A-12845"
    assert data["grade"] == "GRADE_A"
    assert data["status"] == "ACTIVE"
    assert data["is_valid_for_clinical"] is True
    assert data["evidence_accessible"] is True
    assert data["evidence_document_uri"] == "/vault/credentials/pcb/cert_a_12845.pdf"
    assert len(data["sha256_hash"]) == 64

    # Confirm StaffMember model synchronized
    env["staff_pharma_a"].refresh_from_db()
    assert env["staff_pharma_a"].pcb_registration_no == "A-12845"


@pytest.mark.django_db
def test_p252_invalid_or_incomplete_inputs(pcb_env):
    """Scenario 2: Malformed inputs (short reg, invalid grade, expiry earlier than issue date) rejected."""
    env = pcb_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])

    # 1. Registration number too short (< 3 chars)
    res_short = client.post(
        "/api/v2/compliance/pcb-registrations/",
        {
            "branch_id": env["branch_a"].branch_id,
            "staff_member_id": env["staff_pharma_a"].staff_id,
            "registration_no": "A",
            "grade": "GRADE_A",
            "council_issue_date": "2024-01-01",
            "valid_until": "2027-01-01",
        },
        format="json",
    )
    assert res_short.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "registration_no" in res_short.data.get("details", res_short.data)

    # 2. Invalid qualification grade
    res_grade = client.post(
        "/api/v2/compliance/pcb-registrations/",
        {
            "branch_id": env["branch_a"].branch_id,
            "staff_member_id": env["staff_pharma_a"].staff_id,
            "registration_no": "A-9912",
            "grade": "GRADE_INVALID_XYZ",
            "council_issue_date": "2024-01-01",
            "valid_until": "2027-01-01",
        },
        format="json",
    )
    assert res_grade.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "grade" in res_grade.data.get("details", res_grade.data)

    # 3. Validity expiry earlier than issuance date
    res_date = client.post(
        "/api/v2/compliance/pcb-registrations/",
        {
            "branch_id": env["branch_a"].branch_id,
            "staff_member_id": env["staff_pharma_a"].staff_id,
            "registration_no": "A-9912",
            "grade": "GRADE_A",
            "council_issue_date": "2026-05-01",
            "valid_until": "2024-01-01",
        },
        format="json",
    )
    assert res_date.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "valid_until" in res_date.data.get("details", res_date.data)


@pytest.mark.django_db
def test_p252_unauthorized_and_cross_tenant_isolation(pcb_env):
    """Scenario 3: Cashier cannot register/mutate, Tenant B cannot access Tenant A records."""
    env = pcb_env

    # 1. Cashier attempts to register PCB credential -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_user_a"])
    res_cashier = client_cashier.post(
        "/api/v2/compliance/pcb-registrations/",
        {
            "branch_id": env["branch_a"].branch_id,
            "staff_member_id": env["staff_pharma_a"].staff_id,
            "registration_no": "A-8833",
            "grade": "GRADE_A",
            "council_issue_date": "2024-01-01",
            "valid_until": "2028-01-01",
        },
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # Create record in Tenant A
    reg_a = PharmacyPersonnelPcbRegistration.objects.create(
        registration_id=f"pcb-test-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        staff_member=env["staff_pharma_a"],
        registration_no="A-7711",
        grade=PcbGrade.GRADE_A,
        registered_name="Md. Tanvir Ahmed",
        council_issue_date=datetime.date(2023, 1, 1),
        valid_until=datetime.date(2027, 1, 1),
    )

    # 2. Tenant B Owner attempts to access Tenant A registration -> 404 Not Found
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res_cross = client_b.get(f"/api/v2/compliance/pcb-registrations/{reg_a.registration_id}/")
    assert res_cross.status_code == status.HTTP_404_NOT_FOUND

    # 3. Tenant B Owner attempts to delete Tenant A registration -> 404 Not Found
    res_cross_del = client_b.delete(f"/api/v2/compliance/pcb-registrations/{reg_a.registration_id}/")
    assert res_cross_del.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_p252_evidence_access_restrictions(pcb_env):
    """Scenario 4: Evidence URI is redacted for colleagues, unredacted for Owner and Credential Holder."""
    env = pcb_env

    reg = PharmacyPersonnelPcbRegistration.objects.create(
        registration_id=f"pcb-ev-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        staff_member=env["staff_pharma_a"],
        pharmacist_user=env["pharmacist_user_a"],
        registration_no="A-9900",
        grade=PcbGrade.GRADE_A,
        registered_name="Md. Tanvir Ahmed",
        council_issue_date=datetime.date(2023, 1, 1),
        valid_until=datetime.date(2028, 1, 1),
        evidence_document_uri="/vault/confidential/cert_9900.pdf",
        evidence_sha256="abc123def456",
        evidence_access_level=PcbEvidenceAccessLevel.CONFIDENTIAL,
    )

    # 1. Colleague Pharmacist can view list/detail but gets REDACTED evidence URI
    client_colleague = APIClient()
    client_colleague.force_authenticate(user=env["colleague_pharma_user"])
    res_list = client_colleague.get(f"/api/v2/compliance/pcb-registrations/{reg.registration_id}/")
    assert res_list.status_code == status.HTTP_200_OK
    assert res_list.data["evidence_accessible"] is False
    assert res_list.data["evidence_document_uri"] == "[REDACTED_CONFIDENTIAL_EVIDENCE]"

    # Colleague accessing direct /evidence/ endpoint -> 403 Forbidden
    res_ev_colleague = client_colleague.get(f"/api/v2/compliance/pcb-registrations/{reg.registration_id}/evidence/")
    assert res_ev_colleague.status_code == status.HTTP_403_FORBIDDEN
    assert res_ev_colleague.data["error_code"] == "ERR_PCB_EVIDENCE_ACCESS_RESTRICTED"

    # 2. Pharmacy Owner has full access to evidence
    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])
    res_ev_owner = client_owner.get(f"/api/v2/compliance/pcb-registrations/{reg.registration_id}/evidence/")
    assert res_ev_owner.status_code == status.HTTP_200_OK
    assert res_ev_owner.data["evidence_document_uri"] == "/vault/confidential/cert_9900.pdf"

    # 3. Staff Pharmacist (Self / credential holder) has full access to their own evidence
    client_self = APIClient()
    client_self.force_authenticate(user=env["pharmacist_user_a"])
    res_ev_self = client_self.get(f"/api/v2/compliance/pcb-registrations/{reg.registration_id}/evidence/")
    assert res_ev_self.status_code == status.HTTP_200_OK
    assert res_ev_self.data["evidence_document_uri"] == "/vault/confidential/cert_9900.pdf"


@pytest.mark.django_db
def test_p252_dynamic_live_validity_and_renewal_transitions(pcb_env):
    """Scenario 5: Expired valid_until date transitions status to EXPIRED; renewal transitions to PENDING_RENEWAL."""
    env = pcb_env

    # 1. Registration with past valid_until date
    reg = PharmacyPersonnelPcbRegistration.objects.create(
        registration_id=f"pcb-exp-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        staff_member=env["staff_pharma_a"],
        registration_no="B-4411",
        grade=PcbGrade.GRADE_B,
        registered_name="Md. Tanvir Ahmed",
        council_issue_date=datetime.date(2020, 1, 1),
        valid_until=datetime.date(2023, 1, 1),  # Past date
    )
    assert reg.evaluate_live_status() == PcbRegistrationStatus.EXPIRED
    assert reg.is_active_for_clinical_practice() is False

    # 2. File renewal documentation
    reg.renewal_submitted_at = datetime.date(2026, 10, 1)
    reg.save()
    assert reg.evaluate_live_status() == PcbRegistrationStatus.PENDING_RENEWAL
    assert reg.is_active_for_clinical_practice() is True


@pytest.mark.django_db
def test_p252_credential_verification_gate_and_audit_summary(pcb_env):
    """Scenario 6: Gate checks credential validity and grade requirements; branch summary returns metrics."""
    env = pcb_env
    client = APIClient()
    client.force_authenticate(user=env["manager_a"])

    # Create active Grade A registration
    PharmacyPersonnelPcbRegistration.objects.create(
        registration_id=f"pcb-gate-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        staff_member=env["staff_pharma_a"],
        registration_no="A-5522",
        grade=PcbGrade.GRADE_A,
        registered_name="Md. Tanvir Ahmed",
        council_issue_date=datetime.date(2024, 1, 1),
        valid_until=datetime.date(2028, 1, 1),
    )

    # 1. Gate verification for active Grade A -> Authorized
    res_gate = client.post(
        "/api/v2/compliance/pcb-registrations/verify-credential/",
        {"registration_no": "A-5522", "required_grade": "GRADE_A"},
        format="json",
    )
    assert res_gate.status_code == status.HTTP_200_OK
    assert res_gate.data["is_authorized"] is True

    # 2. Gate verification for non-existent credential -> Unauthorized
    res_none = client.post(
        "/api/v2/compliance/pcb-registrations/verify-credential/",
        {"registration_no": "A-99999-NOTFOUND"},
        format="json",
    )
    assert res_none.status_code == status.HTTP_200_OK
    assert res_none.data["is_authorized"] is False
    assert res_none.data["error_code"] == "ERR_PCB_REGISTRATION_NOT_FOUND"

    # 3. Branch audit summary endpoint
    res_sum = client.get(
        f"/api/v2/compliance/pcb-registrations/branch-audit-summary/?branch_id={env['branch_a'].branch_id}"
    )
    assert res_sum.status_code == status.HTTP_200_OK
    assert res_sum.data["total_registered_personnel"] >= 1
    assert res_sum.data["active_count"] >= 1
    assert "GRADE_A" in res_sum.data["grade_distribution"]
