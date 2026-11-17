"""
backend/tests/test_p259_regulatory_reference_register.py
Architecture Decision: ADR-0259 (P259).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Targeted Verification Test Suite for Statutory Regulatory Reference Register:
- Scenario 1: Valid reference registration and review date scheduling.
- Scenario 2: Input validations (short title, invalid URI, reversed dates).
- Scenario 3: Role-based authorization gates and cross-tenant isolation.
- Scenario 4: Reference update and status transition (SUPERSEDED).
- Scenario 5: Safe soft-archive on DELETE request.
- Scenario 6: Overdue reference detection and SHA-256 seal verification.
"""

import datetime
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    RegulatoryJurisdiction,
    RegulatoryReferenceRegister,
    RegulatoryReferenceStatus,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def reg_ref_env():
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p259-a-{uid}", name="Square Health Pharma A", subdomain=f"p259a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p259-a-{uid}", name="Mirpur Main Branch", branch_code="MP01"
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    pharmacist_a = PharmacyUser.objects.create_user(
        email=f"pharma_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-tenant boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p259-b-{uid}", name="Beximco Partner B", subdomain=f"p259b-{uid}"
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
        "pharmacist_a": pharmacist_a,
        "manager_a": manager_a,
        "cashier_a": cashier_a,
        "tenant_b": ten_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_scenario_1_valid_registration_and_review_scheduling(reg_ref_env):
    """
    Scenario 1: Credentialed pharmacist registers statutory DGDA guideline with scheduled review date.
    """
    client = APIClient()
    client.force_authenticate(user=reg_ref_env["pharmacist_a"])

    payload = {
        "title": "National Drug Policy & PMS Guidelines 2024",
        "jurisdiction": "DGDA_BANGLADESH",
        "authority_name": "Directorate General of Drug Administration",
        "source_uri": "https://dgda.gov.bd/guidelines/pms-2024.pdf",
        "version": "2024.1",
        "effective_date": "2024-01-01",
        "next_review_date": "2025-01-01",
        "summary_notes": "Statutory baseline for medicine dispensing and antimicrobial stewardship.",
    }

    url = "/api/v2/compliance/regulatory-references/"
    res = client.post(url, payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED, res.data

    data = res.data
    assert data["reference_id"].startswith("reg-")
    assert data["title"] == "National Drug Policy & PMS Guidelines 2024"
    assert data["jurisdiction"] == "DGDA_BANGLADESH"
    assert data["version"] == "2024.1"
    assert data["effective_date"] == "2024-01-01"
    assert data["next_review_date"] == "2025-01-01"
    assert data["status"] == "ACTIVE"
    assert data["is_tamper_free"] is True
    assert len(data["sha256_document_hash"]) == 64
    assert data["reviewed_by_id"] == str(reg_ref_env["pharmacist_a"].pk)


@pytest.mark.django_db
def test_scenario_2_input_validations(reg_ref_env):
    """
    Scenario 2: Validation errors for short title, invalid source URI, and inverted review dates.
    """
    client = APIClient()
    client.force_authenticate(user=reg_ref_env["pharmacist_a"])
    url = "/api/v2/compliance/regulatory-references/"

    # 1. Short title
    res1 = client.post(url, {
        "title": "DG",
        "source_uri": "https://dgda.gov.bd/pms.pdf",
        "version": "v1",
        "effective_date": "2024-01-01",
        "next_review_date": "2025-01-01",
    }, format="json")
    assert res1.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]

    # 2. Invalid URI
    res2 = client.post(url, {
        "title": "Valid Title for Guideline",
        "source_uri": "not-a-valid-uri",
        "version": "v1",
        "effective_date": "2024-01-01",
        "next_review_date": "2025-01-01",
    }, format="json")
    assert res2.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]

    # 3. Inverted dates (effective_date > next_review_date)
    res3 = client.post(url, {
        "title": "Valid Title for Guideline",
        "source_uri": "https://dgda.gov.bd/pms.pdf",
        "version": "v1",
        "effective_date": "2025-06-01",
        "next_review_date": "2024-06-01",
    }, format="json")
    assert res3.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]


@pytest.mark.django_db
def test_scenario_3_role_authorization_and_cross_tenant_isolation(reg_ref_env):
    """
    Scenario 3: Cashier cannot register or edit references; Tenant B cannot access Tenant A references.
    """
    # 1. Cashier attempts to register reference
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=reg_ref_env["cashier_a"])

    url = "/api/v2/compliance/regulatory-references/"
    res_cashier = client_cashier.post(url, {
        "title": "Cashier Unauthorized Guideline",
        "source_uri": "https://dgda.gov.bd/pms.pdf",
        "version": "1.0",
        "effective_date": "2024-01-01",
        "next_review_date": "2025-01-01",
    }, format="json")
    assert res_cashier.status_code in [status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED]

    # 2. Create reference under Tenant A
    client_owner_a = APIClient()
    client_owner_a.force_authenticate(user=reg_ref_env["owner_a"])
    res_a = client_owner_a.post(url, {
        "title": "Tenant A Standard SOP",
        "source_uri": "https://dgda.gov.bd/sop.pdf",
        "version": "1.0",
        "effective_date": "2024-01-01",
        "next_review_date": "2025-01-01",
    }, format="json")
    assert res_a.status_code == status.HTTP_201_CREATED
    ref_id = res_a.data["reference_id"]

    # 3. Cashier CAN read Tenant A references
    res_read = client_cashier.get(f"{url}{ref_id}/")
    assert res_read.status_code == status.HTTP_200_OK

    # 4. Tenant B attempts to read Tenant A reference -> NotFound
    client_b = APIClient()
    client_b.force_authenticate(user=reg_ref_env["owner_b"])
    res_b = client_b.get(f"{url}{ref_id}/")
    assert res_b.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_scenario_4_update_and_status_transition(reg_ref_env):
    """
    Scenario 4: Branch Manager updates reference version and marks it SUPERSEDED with updated hash.
    """
    client = APIClient()
    client.force_authenticate(user=reg_ref_env["manager_a"])
    url = "/api/v2/compliance/regulatory-references/"

    create_res = client.post(url, {
        "title": "Antimicrobial Treatment Guidelines 2023",
        "jurisdiction": "WHO_INTERNATIONAL",
        "authority_name": "World Health Organization",
        "source_uri": "https://who.int/guidelines/awarre-2023.pdf",
        "version": "2023.1",
        "effective_date": "2023-01-01",
        "next_review_date": "2024-01-01",
    }, format="json")
    assert create_res.status_code == status.HTTP_201_CREATED
    ref_id = create_res.data["reference_id"]
    initial_hash = create_res.data["sha256_document_hash"]

    # Update version and status to SUPERSEDED
    patch_res = client.patch(f"{url}{ref_id}/", {
        "version": "2023.2-revised",
        "status": "SUPERSEDED",
        "summary_notes": "Superseded by 2024 National Guideline release.",
    }, format="json")
    assert patch_res.status_code == status.HTTP_200_OK
    assert patch_res.data["version"] == "2023.2-revised"
    assert patch_res.data["status"] == "SUPERSEDED"
    assert patch_res.data["sha256_document_hash"] != initial_hash
    assert patch_res.data["is_tamper_free"] is True


@pytest.mark.django_db
def test_scenario_5_soft_archive_on_delete(reg_ref_env):
    """
    Scenario 5: DELETE request executes safe soft-archive (status=ARCHIVED) without data destruction.
    """
    client = APIClient()
    client.force_authenticate(user=reg_ref_env["owner_a"])
    url = "/api/v2/compliance/regulatory-references/"

    create_res = client.post(url, {
        "title": "Temporary COVID-19 Pharmacy SOP",
        "jurisdiction": "DGDA_BANGLADESH",
        "source_uri": "https://dgda.gov.bd/covid-sop.pdf",
        "version": "1.0",
        "effective_date": "2020-04-01",
        "next_review_date": "2021-04-01",
    }, format="json")
    assert create_res.status_code == status.HTTP_201_CREATED
    ref_id = create_res.data["reference_id"]

    # Delete reference
    del_res = client.delete(f"{url}{ref_id}/")
    assert del_res.status_code == status.HTTP_200_OK
    assert del_res.data["status"] == "ARCHIVED"

    # Confirm record still exists in database as ARCHIVED
    db_record = RegulatoryReferenceRegister.objects.get(reference_id=ref_id)
    assert db_record.status == RegulatoryReferenceStatus.ARCHIVED


@pytest.mark.django_db
def test_scenario_6_overdue_detection_and_hash_verification(reg_ref_env):
    """
    Scenario 6: Test overdue review query endpoint and cryptographic hash verification.
    """
    client = APIClient()
    client.force_authenticate(user=reg_ref_env["pharmacist_a"])
    url = "/api/v2/compliance/regulatory-references/"

    # Create an overdue reference (next_review_date in past)
    past_date = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    eff_date = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()

    create_res = client.post(url, {
        "title": "Overdue Inspection Protocol",
        "jurisdiction": "PCB_BANGLADESH",
        "authority_name": "Pharmacy Council of Bangladesh",
        "source_uri": "https://pcb.gov.bd/inspection.pdf",
        "version": "2022.0",
        "effective_date": eff_date,
        "next_review_date": past_date,
    }, format="json")
    assert create_res.status_code == status.HTTP_201_CREATED
    ref_id = create_res.data["reference_id"]
    assert create_res.data["is_review_overdue"] is True

    # Check overdue endpoint
    overdue_res = client.get(f"{url}overdue/")
    assert overdue_res.status_code == status.HTTP_200_OK
    overdue_ids = [item["reference_id"] for item in overdue_res.data["results"]]
    assert ref_id in overdue_ids

    # Check verify-hash endpoint
    verify_res = client.get(f"{url}{ref_id}/verify-hash/")
    assert verify_res.status_code == status.HTTP_200_OK
    assert verify_res.data["is_tamper_free"] is True
    assert verify_res.data["is_review_overdue"] is True
    assert len(verify_res.data["sha256_document_hash"]) == 64
