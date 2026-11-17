"""
backend/tests/test_p251_drug_license_record.py

Architecture Decision: ADR-0251 (P251).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Branch Drug License Tracking, Expiration Reminders & Compliance Gates:
1. Valid branch drug license registration by Pharmacy Owner with DGDA authority, dates, document hash, and live status summary.
2. Incomplete or invalid input rejections (short license number, inverted dates, invalid type).
3. Expiration evaluation and automated renewal review reminders.
4. Renewal archival and soft-deletion lifecycle.
5. RBAC & Cross-Tenant Isolation: Cashier blocked from managing (403), read-only allowed, and cross-tenant isolation enforced.
"""

import datetime
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchDrugLicenseRecord,
    DrugLicenseStatus,
    DrugLicenseType,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def license_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p251-a-{uid}", name="License Tenant A", subdomain=f"p251a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p251-a-{uid}", name="Uttara Dispensary", branch_code="UT01"
    )

    # Owner for Tenant A
    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_p251_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.PHARMACY_OWNER,
    )

    # Cashier for Tenant A
    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_p251_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p251-b-{uid}", name="License Tenant B", subdomain=f"p251b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p251-b-{uid}", name="Badda Branch", branch_code="BD01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_p251_b_{uid}@test.com",
        phone_number=f"+88018{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "cashier_a": cashier_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
        "uid": uid,
    }


@pytest.mark.django_db
def test_p251_valid_drug_license_registration_and_evaluation(license_env):
    """
    Scenario 1: Valid branch drug license registration by Pharmacy Owner with
    DGDA authority, dates, document hash, and live status summary.
    """
    env = license_env
    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])

    payload = {
        "branch_id": env["branch_a"].branch_id,
        "license_number": "DL-DHK-2024-99812",
        "issuing_authority": "Directorate General of Drug Administration (DGDA), Dhaka",
        "license_type": DrugLicenseType.FORM_20_RETAIL,
        "issue_date": "2024-01-01",
        "expiry_date": "2027-12-31",
        "document_file_uri": "s3://compliance-vault/ten-a/dl-2024.pdf",
        "review_notes": "Official retail drug license renewed and verified with DGDA inspector seal.",
    }

    res = client_owner.post("/api/v2/compliance/drug-licenses/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data
    assert data["license_record_id"].startswith("bdl-")
    assert data["license_number"] == "DL-DHK-2024-99812"
    assert data["status"] == DrugLicenseStatus.ACTIVE
    assert data["is_valid"] is True
    assert len(data["document_sha256"]) == 64
    assert data["days_until_expiry"] > 100

    # Verify branch license number synced
    env["branch_a"].refresh_from_db()
    assert env["branch_a"].license_number == "DL-DHK-2024-99812"

    # Status summary endpoint
    res_summary = client_owner.get(
        f"/api/v2/compliance/drug-licenses/status-summary/?branch_id={env['branch_a'].branch_id}"
    )
    assert res_summary.status_code == status.HTTP_200_OK
    assert res_summary.data["is_valid"] is True
    assert res_summary.data["status"] == DrugLicenseStatus.ACTIVE


@pytest.mark.django_db
def test_p251_invalid_and_incomplete_input_rejections(license_env):
    """
    Scenario 2: Validation rejects short license number, inverted dates,
    short issuing authority, or invalid license type.
    """
    env = license_env
    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])

    base_payload = {
        "branch_id": env["branch_a"].branch_id,
        "license_number": "DL-DHK-2024-99812",
        "issuing_authority": "DGDA Dhaka",
        "license_type": DrugLicenseType.FORM_20_RETAIL,
        "issue_date": "2024-01-01",
        "expiry_date": "2026-12-31",
    }

    # 1. Short license number (<4 chars)
    p1 = dict(base_payload, license_number="DL")
    r1 = client_owner.post("/api/v2/compliance/drug-licenses/", p1, format="json")
    assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Inverted dates (expiry <= issue)
    p2 = dict(base_payload, issue_date="2026-12-31", expiry_date="2024-01-01")
    r2 = client_owner.post("/api/v2/compliance/drug-licenses/", p2, format="json")
    assert r2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 3. Invalid license type
    p3 = dict(base_payload, license_type="INVALID_TYPE")
    r3 = client_owner.post("/api/v2/compliance/drug-licenses/", p3, format="json")
    assert r3.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 4. Short authority (<3 chars)
    p4 = dict(base_payload, issuing_authority="DG")
    r4 = client_owner.post("/api/v2/compliance/drug-licenses/", p4, format="json")
    assert r4.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p251_expired_license_evaluation_and_reminders(license_env):
    """
    Scenario 3: License evaluation accurately flags expired licenses and surfaces
    proactive expiration review reminders.
    """
    env = license_env
    today = datetime.date.today()
    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])

    # 1. Create expired license (expired 10 days ago)
    exp_date = today - datetime.timedelta(days=10)
    issue_date = exp_date - datetime.timedelta(days=365)
    BranchDrugLicenseRecord.objects.create(
        license_record_id=f"bdl-exp-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        license_number="DL-EXPIRED-99",
        issue_date=issue_date,
        expiry_date=exp_date,
        status=DrugLicenseStatus.ACTIVE,
        is_archived=False,
    )

    res_summary = client_owner.get(
        f"/api/v2/compliance/drug-licenses/status-summary/?branch_id={env['branch_a'].branch_id}"
    )
    assert res_summary.status_code == status.HTTP_200_OK
    assert res_summary.data["is_valid"] is False
    assert res_summary.data["status"] == DrugLicenseStatus.EXPIRED
    assert "expired" in res_summary.data["warning_en"].lower()

    # 2. Upcoming license expiring in 15 days triggers reminder
    exp_soon = today + datetime.timedelta(days=15)
    BranchDrugLicenseRecord.objects.filter(branch=env["branch_a"]).delete()
    BranchDrugLicenseRecord.objects.create(
        license_record_id=f"bdl-soon-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        license_number="DL-SOON-15",
        issue_date=today - datetime.timedelta(days=100),
        expiry_date=exp_soon,
        status=DrugLicenseStatus.ACTIVE,
        is_archived=False,
    )

    res_rem = client_owner.get("/api/v2/compliance/drug-licenses/reminders/")
    assert res_rem.status_code == status.HTTP_200_OK
    assert res_rem.data["count"] >= 1
    rem = res_rem.data["results"][0]
    assert rem["license_number"] == "DL-SOON-15"
    assert rem["urgency"] == "CRITICAL"
    assert rem["days_until_expiry"] == 15


@pytest.mark.django_db
def test_p251_soft_archive_and_renewal_lifecycle(license_env):
    """
    Scenario 4: Registering a replacement license archives the old license,
    and DELETE soft-archives the active record.
    """
    env = license_env
    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])

    # 1. Register first license
    r1 = client_owner.post(
        "/api/v2/compliance/drug-licenses/",
        {
            "branch_id": env["branch_a"].branch_id,
            "license_number": "DL-FIRST-01",
            "issuing_authority": "DGDA Dhaka",
            "issue_date": "2023-01-01",
            "expiry_date": "2025-12-31",
        },
        format="json",
    )
    assert r1.status_code == status.HTTP_201_CREATED
    id1 = r1.data["license_record_id"]

    # 2. Register renewal license -> archives id1
    r2 = client_owner.post(
        "/api/v2/compliance/drug-licenses/",
        {
            "branch_id": env["branch_a"].branch_id,
            "license_number": "DL-RENEWED-02",
            "issuing_authority": "DGDA Dhaka",
            "issue_date": "2026-01-01",
            "expiry_date": "2028-12-31",
        },
        format="json",
    )
    assert r2.status_code == status.HTTP_201_CREATED
    id2 = r2.data["license_record_id"]

    lic1 = BranchDrugLicenseRecord.objects.get(license_record_id=id1)
    lic2 = BranchDrugLicenseRecord.objects.get(license_record_id=id2)
    assert lic1.is_archived is True
    assert lic2.is_archived is False

    # 3. Soft archive via DELETE
    r_del = client_owner.delete(f"/api/v2/compliance/drug-licenses/{id2}/")
    assert r_del.status_code == status.HTTP_204_NO_CONTENT
    lic2.refresh_from_db()
    assert lic2.is_archived is True


@pytest.mark.django_db
def test_p251_cashier_rbac_and_cross_tenant_isolation(license_env):
    """
    Scenario 5:
    a) Cashier cannot create or modify licenses (403 Forbidden).
    b) Cashier can read status summary (200 OK).
    c) Cross-tenant owner blocked with 403/404.
    """
    env = license_env

    # a) Cashier attempts license creation -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])
    r_cashier = client_cashier.post(
        "/api/v2/compliance/drug-licenses/",
        {
            "branch_id": env["branch_a"].branch_id,
            "license_number": "DL-CASHIER-TRY",
            "issuing_authority": "DGDA Dhaka",
            "issue_date": "2024-01-01",
            "expiry_date": "2026-12-31",
        },
        format="json",
    )
    assert r_cashier.status_code == status.HTTP_403_FORBIDDEN

    # b) Cashier reads status summary -> 200 OK
    r_summary = client_cashier.get(
        f"/api/v2/compliance/drug-licenses/status-summary/?branch_id={env['branch_a'].branch_id}"
    )
    assert r_summary.status_code == status.HTTP_200_OK

    # c) Cross-tenant Owner B attempts creating license for Tenant A's branch -> 404 / 403
    client_cross = APIClient()
    client_cross.force_authenticate(user=env["owner_b"])
    r_cross = client_cross.post(
        "/api/v2/compliance/drug-licenses/",
        {
            "branch_id": env["branch_a"].branch_id,
            "license_number": "DL-CROSS-TENANT",
            "issuing_authority": "DGDA Dhaka",
            "issue_date": "2024-01-01",
            "expiry_date": "2026-12-31",
        },
        format="json",
    )
    assert r_cross.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

