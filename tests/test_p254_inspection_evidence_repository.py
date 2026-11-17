"""
backend/tests/test_p254_inspection_evidence_repository.py

Architecture Decision: ADR-0254 (P254).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Statutory Inspection Evidence Repository:
1. Valid creation & retrieval: Refrigerator temperature log, expired stock disposal, training proof.
2. Incomplete & malformed inputs: short title, invalid type, future date, invalid temp/decimal.
3. Clearance RBAC & cross-tenant isolation: Cashier blocked, restricted level gate, cross-tenant 404.
4. Confidential document redactions & access clearance.
5. Tamper-evident DGDA audit package export with Decimal totals and composite SHA-256 manifest.
6. Verification sign-off and soft-archival audit tracking.
"""

from decimal import Decimal
import datetime
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    InspectionEvidenceAccessLevel,
    InspectionEvidenceRecord,
    InspectionEvidenceType,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def evidence_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p254-a-{uid}", name="Compliance Pharmacy A", subdomain=f"p254a-{uid}"
    )
    br_a1 = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p254-a1-{uid}", name="Gulshan Main Branch", branch_code="GL01"
    )
    br_a2 = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p254-a2-{uid}", name="Banani Branch", branch_code="BN01"
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    manager_a1 = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a1.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    pharmacist_a1 = PharmacyUser.objects.create_user(
        email=f"pharmacist_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a1.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    cashier_a1 = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a1.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-tenant boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p254-b-{uid}", name="Compliance Pharmacy B", subdomain=f"p254b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p254-b-{uid}", name="Dhanmondi Branch", branch_code="DH01"
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
        "branch_a1": br_a1,
        "branch_a2": br_a2,
        "owner_a": owner_a,
        "manager_a1": manager_a1,
        "pharmacist_a1": pharmacist_a1,
        "cashier_a1": cashier_a1,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p254_valid_evidence_creation_and_retrieval(evidence_env):
    """Scenario 1: Owner and Pharmacist record valid cold-chain and expired stock records."""
    env = evidence_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])

    # 1. Record Refrigerator Temperature Log (Cold-chain compliant 3.5°C - 6.0°C)
    payload_fridge = {
        "branch_id": env["branch_a1"].branch_id,
        "evidence_type": "REFRIGERATOR_TEMPERATURE_LOG",
        "title": "Chiller Unit A Morning Temperature Log",
        "description": "Routine daily check of vaccine and insulin storage temperature.",
        "log_date": "2026-11-15",
        "evidence_document_uri": "/vault/compliance/chiller_log_20261115.pdf",
        "file_name": "Chiller_Log_20261115.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1048576,
        "access_level": "CONFIDENTIAL",
        "metadata": {
            "refrigerator_id": "FRIDGE-01",
            "min_temp_celsius": 3.5,
            "max_temp_celsius": 6.0,
        },
        "is_verified": True,
        "verification_notes": "Checked against calibrated NIST thermometer.",
    }
    res_fridge = client.post("/api/v2/compliance/inspection-evidence/", payload_fridge, format="json")
    assert res_fridge.status_code == status.HTTP_201_CREATED
    assert res_fridge.data["evidence_type"] == "REFRIGERATOR_TEMPERATURE_LOG"
    assert res_fridge.data["metadata"]["is_cold_chain_excursion"] is False
    assert len(res_fridge.data["sha256_hash"]) == 64

    # 2. Pharmacist records Expired Stock Disposal manifest with Decimal BDT valuation
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a1"])
    payload_disposal = {
        "branch_id": env["branch_a1"].branch_id,
        "evidence_type": "EXPIRED_STOCK_DISPOSAL",
        "title": "Batch Expired Stock Neutralization Manifest",
        "description": "Quarantined expired antibiotics incinerated under pharmacist supervision.",
        "log_date": "2026-11-16",
        "evidence_document_uri": "/vault/compliance/disposal_manifest_nov2026.pdf",
        "file_name": "Disposal_Nov2026.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 524288,
        "access_level": "INTERNAL",
        "metadata": {
            "witness_pharmacist": "Md. Tanvir Ahmed",
            "disposal_method": "INCINERATION",
            "disposal_value_bdt": "14850.75",
        },
    }
    res_disposal = client_pharma.post("/api/v2/compliance/inspection-evidence/", payload_disposal, format="json")
    assert res_disposal.status_code == status.HTTP_201_CREATED
    assert res_disposal.data["metadata"]["disposal_value_bdt"] == "14850.75"

    # List evidence
    res_list = client_pharma.get("/api/v2/compliance/inspection-evidence/")
    assert res_list.status_code == status.HTTP_200_OK
    assert res_list.data["count"] == 2


@pytest.mark.django_db
def test_p254_invalid_and_incomplete_inputs(evidence_env):
    """Scenario 2: Malformed inputs (short title, future date, inverted temps, negative value) rejected."""
    env = evidence_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])

    # 1. Short title (< 3 chars)
    res_short = client.post(
        "/api/v2/compliance/inspection-evidence/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "evidence_type": "REFRIGERATOR_TEMPERATURE_LOG",
            "title": "A",
            "log_date": "2026-11-10",
            "evidence_document_uri": "/vault/doc.pdf",
        },
        format="json",
    )
    assert res_short.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "title" in res_short.data.get("details", res_short.data)

    # 2. Future log date
    tomorrow = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    res_future = client.post(
        "/api/v2/compliance/inspection-evidence/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "evidence_type": "STAFF_TRAINING_PROOF",
            "title": "GPP Compliance Workshop",
            "log_date": tomorrow,
            "evidence_document_uri": "/vault/training.pdf",
        },
        format="json",
    )
    assert res_future.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "log_date" in res_future.data.get("details", res_future.data)

    # 3. Inverted refrigerator temperatures (min > max)
    res_temp = client.post(
        "/api/v2/compliance/inspection-evidence/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "evidence_type": "REFRIGERATOR_TEMPERATURE_LOG",
            "title": "Faulty Temperature Sensor Log",
            "log_date": "2026-11-10",
            "evidence_document_uri": "/vault/temp.pdf",
            "metadata": {"min_temp_celsius": 10.5, "max_temp_celsius": 4.0},
        },
        format="json",
    )
    assert res_temp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 4. Negative expired stock disposal valuation
    res_neg = client.post(
        "/api/v2/compliance/inspection-evidence/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "evidence_type": "EXPIRED_STOCK_DISPOSAL",
            "title": "Invalid Destruction Manifest",
            "log_date": "2026-11-10",
            "evidence_document_uri": "/vault/destruct.pdf",
            "metadata": {"disposal_value_bdt": "-500.00"},
        },
        format="json",
    )
    assert res_neg.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p254_clearance_rbac_and_cross_tenant_isolation(evidence_env):
    """Scenario 3: Cashier blocked from managing, Pharmacist blocked from RESTRICTED, Tenant B blocked."""
    env = evidence_env

    # 1. Cashier attempts to create inspection evidence -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a1"])
    res_cashier = client_cashier.post(
        "/api/v2/compliance/inspection-evidence/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "evidence_type": "REFRIGERATOR_TEMPERATURE_LOG",
            "title": "Cashier Unapproved Log",
            "log_date": "2026-11-10",
            "evidence_document_uri": "/vault/doc.pdf",
        },
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Staff Pharmacist attempts to create RESTRICTED DGDA memo -> 403 Forbidden
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a1"])
    res_pharma_rest = client_pharma.post(
        "/api/v2/compliance/inspection-evidence/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "evidence_type": "DGDA_INSPECTION_MEMO",
            "title": "Classified DGDA Regulatory Finding",
            "log_date": "2026-11-10",
            "evidence_document_uri": "/vault/dgda_notice.pdf",
            "access_level": "RESTRICTED",
        },
        format="json",
    )
    assert res_pharma_rest.status_code == status.HTTP_403_FORBIDDEN

    # Create evidence in Tenant A
    record_a = InspectionEvidenceRecord.objects.create(
        evidence_id=f"iev-iso-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        evidence_type=InspectionEvidenceType.STAFF_TRAINING_PROOF,
        title="GPP Hygiene Training",
        log_date=datetime.date(2026, 11, 1),
        evidence_document_uri="/vault/training_secret.pdf",
        file_name="Training_Cert.pdf",
        access_level=InspectionEvidenceAccessLevel.CONFIDENTIAL,
    )

    # 3. Tenant B Owner attempts to access Tenant A evidence -> 404 Not Found
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res_cross = client_b.get(f"/api/v2/compliance/inspection-evidence/{record_a.evidence_id}/")
    assert res_cross.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_p254_confidential_redaction_and_document_access(evidence_env):
    """Scenario 4: Document URI redacted for Cashier; direct /document/ returns 403 on insufficient clearance."""
    env = evidence_env

    rec = InspectionEvidenceRecord.objects.create(
        evidence_id=f"iev-redact-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        evidence_type=InspectionEvidenceType.DGDA_INSPECTION_MEMO,
        title="DGDA Formal Inspection Findings",
        log_date=datetime.date(2026, 11, 5),
        evidence_document_uri="/vault/classified/dgda_audit_finding.pdf",
        file_name="DGDA_Audit.pdf",
        access_level=InspectionEvidenceAccessLevel.RESTRICTED,
    )

    # 1. Cashier attempting to get classified record -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a1"])
    res_cashier_get = client_cashier.get(f"/api/v2/compliance/inspection-evidence/{rec.evidence_id}/")
    assert res_cashier_get.status_code == status.HTTP_403_FORBIDDEN

    # 2. Staff Pharmacist gets record but URI is redacted and document_accessible is False
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a1"])
    res_pharma = client_pharma.get(f"/api/v2/compliance/inspection-evidence/{rec.evidence_id}/")
    assert res_pharma.status_code == status.HTTP_200_OK
    assert res_pharma.data["document_accessible"] is False
    assert res_pharma.data["evidence_document_uri"] == "[REDACTED_CONFIDENTIAL_EVIDENCE]"

    # Pharmacist accessing direct /document/ -> 403 Forbidden
    res_pharma_doc = client_pharma.get(f"/api/v2/compliance/inspection-evidence/{rec.evidence_id}/document/")
    assert res_pharma_doc.status_code == status.HTTP_403_FORBIDDEN
    assert res_pharma_doc.data["error_code"] == "ERR_INSPECTION_EVIDENCE_ACCESS_RESTRICTED"

    # 3. Pharmacy Owner has full unredacted access
    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])
    res_owner_doc = client_owner.get(f"/api/v2/compliance/inspection-evidence/{rec.evidence_id}/document/")
    assert res_owner_doc.status_code == status.HTTP_200_OK
    assert res_owner_doc.data["evidence_document_uri"] == "/vault/classified/dgda_audit_finding.pdf"


@pytest.mark.django_db
def test_p254_dgda_audit_package_export(evidence_env):
    """Scenario 5: Owner exports statutory DGDA audit bundle with composite SHA-256 checksum."""
    env = evidence_env

    # Seed 3 records: 1 cold-chain with excursion, 1 cold-chain normal, 1 expired stock
    InspectionEvidenceRecord.objects.create(
        evidence_id=f"iev-ex1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        evidence_type=InspectionEvidenceType.REFRIGERATOR_TEMPERATURE_LOG,
        title="Chiller Excursion Log",
        log_date=datetime.date(2026, 11, 2),
        evidence_document_uri="/vault/log1.pdf",
        metadata={"min_temp_celsius": 1.2, "max_temp_celsius": 5.0, "is_cold_chain_excursion": True},
    )
    InspectionEvidenceRecord.objects.create(
        evidence_id=f"iev-norm-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        evidence_type=InspectionEvidenceType.REFRIGERATOR_TEMPERATURE_LOG,
        title="Chiller Normal Log",
        log_date=datetime.date(2026, 11, 3),
        evidence_document_uri="/vault/log2.pdf",
        metadata={"min_temp_celsius": 3.0, "max_temp_celsius": 6.5, "is_cold_chain_excursion": False},
    )
    InspectionEvidenceRecord.objects.create(
        evidence_id=f"iev-exp-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        evidence_type=InspectionEvidenceType.EXPIRED_STOCK_DISPOSAL,
        title="Antibiotic Disposal Record",
        log_date=datetime.date(2026, 11, 4),
        evidence_document_uri="/vault/disp.pdf",
        metadata={"disposal_value_bdt": "22450.50"},
    )

    client_owner = APIClient()
    client_owner.force_authenticate(user=env["owner_a"])

    export_payload = {
        "branch_id": env["branch_a1"].branch_id,
        "start_date": "2026-11-01",
        "end_date": "2026-11-10",
    }
    res_pkg = client_owner.post(
        "/api/v2/compliance/inspection-evidence/export-audit-package/",
        export_payload,
        format="json",
    )
    assert res_pkg.status_code == status.HTTP_200_OK
    data = res_pkg.data
    assert data["total_records"] == 3
    assert data["cold_chain_excursion_count"] == 1
    assert data["total_expired_stock_disposal_bdt"] == "22450.50"
    assert len(data["package_manifest_sha256"]) == 64
    assert data["is_inspection_ready"] is True
    assert len(data["evidence_manifest"]) == 3


@pytest.mark.django_db
def test_p254_soft_archival_and_verification(evidence_env):
    """Scenario 6: Soft archival preserves records and verification tracks pharmacist sign-off."""
    env = evidence_env
    client_mgr = APIClient()
    client_mgr.force_authenticate(user=env["manager_a1"])

    record = InspectionEvidenceRecord.objects.create(
        evidence_id=f"iev-arc-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        evidence_type=InspectionEvidenceType.PEST_CONTROL_SANITATION,
        title="Quarterly Pest Treatment",
        log_date=datetime.date(2026, 10, 15),
        evidence_document_uri="/vault/pest.pdf",
    )

    # 1. Update verification status
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a1"])
    res_patch = client_pharma.patch(
        f"/api/v2/compliance/inspection-evidence/{record.evidence_id}/",
        {"is_verified": True, "verification_notes": "Certificate verified with pest control provider."},
        format="json",
    )
    assert res_patch.status_code == status.HTTP_200_OK
    assert res_patch.data["is_verified"] is True
    assert res_patch.data["verified_by_id"] == str(env["pharmacist_a1"].user_id)

    # 2. Archive record
    res_del = client_mgr.delete(
        f"/api/v2/compliance/inspection-evidence/{record.evidence_id}/",
        {"reason": "Superseded by annual audit certificate"},
        format="json",
    )
    assert res_del.status_code == status.HTTP_200_OK
    assert res_del.data["is_archived"] is True

    # 3. Default list excludes archived record
    res_active = client_mgr.get("/api/v2/compliance/inspection-evidence/")
    assert res_active.status_code == status.HTTP_200_OK
    assert res_active.data["count"] == 0

    # 4. Explicit include_archived returns it
    res_all = client_mgr.get("/api/v2/compliance/inspection-evidence/?include_archived=true")
    assert res_all.status_code == status.HTTP_200_OK
    assert res_all.data["count"] == 1

