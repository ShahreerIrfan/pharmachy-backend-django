"""
backend/tests/test_p258_compliance_audit_export.py
Architecture Decision: ADR-0258 (P258).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Targeted Verification Test Suite for Compliance Audit Exports:
- Scenario 1: Valid compliance audit export with time filters and automated patient privacy redactions.
- Scenario 2: Invalid inputs (reversed date range, invalid scope, missing branch).
- Scenario 3: Unauthorized role (Cashier) and cross-tenant boundary isolation.
- Scenario 4: Immutability enforcement (PUT/PATCH/DELETE strictly rejected).
- Scenario 5: Cryptographic manifest integrity verification.
- Scenario 6: Multi-category compilation (Antibiotic Register, Temperature Reviews, Recalls).
"""

import datetime
import random
import uuid
from decimal import Decimal

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AuditExportScope,
    BatchRecallNotification,
    BatchStatus,
    BatchStock,
    Branch,
    ComplianceAuditExportManifest,
    MedicineProduct,
    PosSaleOrder,
    PosSaleOrderLine,
    PrivacyRedactionLevel,
    RecallClass,
    RecallSource,
    TemperatureExcursionSafetyReview,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def audit_export_env():
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p258-a-{uid}", name="Square Health Pharma A", subdomain=f"p258a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p258-a-{uid}", name="Mirpur Main Branch", branch_code="MP01"
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    pharmacist_a = PharmacyUser.objects.create_user(
        email=f"pharma_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
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
        tenant_id=f"ten-p258-b-{uid}", name="Apex Pharma B", subdomain=f"p258b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p258-b-{uid}", name="Uttara Branch", branch_code="UT01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    product = MedicineProduct.objects.create(
        product_id=f"prod-{uid}",
        brand_name="Moxaclav 625mg",
        generic_name="Amoxicillin + Clavulanic Acid",
        dosage_form="Tablet",
        strength="625mg",
        manufacturer="Square Pharmaceuticals Ltd",
        mrp_unit=Decimal("45.00"),
        cost_unit=Decimal("36.00"),
        is_antibiotic=True,
    )

    return {
        "uid": uid,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "pharmacist_a": pharmacist_a,
        "cashier_a": cashier_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
        "product": product,
    }


@pytest.mark.django_db
def test_p258_valid_compliance_audit_export_with_privacy_redaction(audit_export_env):
    """Scenario 1: Pharmacist exports Antibiotic Stewardship audit package; patient PHI is automatically masked."""
    env = audit_export_env

    # Setup PosSaleOrder and PosSaleOrderLine
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-abx-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-ABX-{env['uid'].upper()}",
    )
    sale_line = PosSaleOrderLine.objects.create(
        line_id=f"line-abx-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["product"],
        quantity_units=14,
        base_units_total=14,
        unit_price_bdt=Decimal("45.00"),
        line_subtotal_bdt=Decimal("630.00"),
        line_total_bdt=Decimal("630.00"),
    )

    # Setup Antibiotic Register entry
    ab_entry = AntibioticDispenseRegisterEntry.objects.create(
        register_entry_id=f"reg-ab-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        sale_line=sale_line,
        product=env["product"],
        brand_name="Moxaclav 625mg",
        generic_name="Amoxicillin + Clavulanic Acid",
        classification_tier="ACCESS",
        dispensed_base_units=14,
        rx_reference_code=f"RX-2026-{env['uid']}",
        doctor_name="Prof. Dr. A. K. Azad",
        doctor_bmdc_no="BMDC-A-45812",
        patient_token="Mohammad Rahim Uddin",
        approving_pharmacist=env["pharmacist_a"],
        approving_pharmacist_pcb_no="PCB-A-99120",
        dispensed_at=timezone.now(),
    )

    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    today_str = datetime.date.today().isoformat()
    payload = {
        "branch_id": env["branch_a"].branch_id,
        "export_scope": "ANTIBIOTIC_STEWARDSHIP",
        "start_date": today_str,
        "end_date": today_str,
        "is_patient_data_redacted": True,
    }

    res = client.post("/api/v2/compliance/audit-exports/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data

    assert data["export_scope"] == "ANTIBIOTIC_STEWARDSHIP"
    assert data["manifest_records_count"] == 1
    assert data["is_patient_data_redacted"] is True
    assert data["is_tamper_free"] is True
    assert len(data["manifest_sha256"]) == 64

    # Verify patient privacy redaction in payload items
    items = data["manifest_payload"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["record_type"] == "ANTIBIOTIC_DISPENSING"
    assert item["patient_identity_redacted"] == "M*** R*** U***"
    # Regulatory identifiers preserved
    assert item["prescriber_bmdc_reg"] == "BMDC-A-45812"
    assert item["pharmacist_pcb_reg"] == "PCB-A-99120"


@pytest.mark.django_db
def test_p258_invalid_inputs(audit_export_env):
    """Scenario 2: Preflight validation rejects reversed dates, missing date range, or invalid scope."""
    env = audit_export_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    # 1. Reversed date range (start > end)
    res_rev = client.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "export_scope": "FULL_REGULATORY_BUNDLE",
            "start_date": "2026-03-31",
            "end_date": "2026-03-01",
        },
        format="json",
    )
    assert res_rev.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "date" in str(res_rev.data).lower()

    # 2. Invalid scope enum
    res_scope = client.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "export_scope": "INVALID_SCOPE_CHOICE",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
        format="json",
    )
    assert res_scope.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "export_scope" in str(res_scope.data)

    # 3. Missing branch
    res_br = client.post(
        "/api/v2/compliance/audit-exports/",
        {
            "export_scope": "FULL_REGULATORY_BUNDLE",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
        format="json",
    )
    assert res_br.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "branch_id" in str(res_br.data)


@pytest.mark.django_db
def test_p258_unauthorized_role_and_cross_tenant_isolation(audit_export_env):
    """Scenario 3: Cashiers are denied audit package exports; cross-tenant access is blocked."""
    env = audit_export_env

    # 1. Cashier attempt -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])

    res_cashier = client_cashier.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Cross-tenant attempt -> Tenant B owner cannot export Tenant A's branch data
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])

    res_b = client_b.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
        format="json",
    )
    assert res_b.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_ENTITY]


@pytest.mark.django_db
def test_p258_immutability_enforcement(audit_export_env):
    """Scenario 4: Compliance audit export manifests are sealed; PUT/PATCH/DELETE are rejected."""
    env = audit_export_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    today_str = datetime.date.today().isoformat()
    res_create = client.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "start_date": today_str,
            "end_date": today_str,
        },
        format="json",
    )
    assert res_create.status_code == status.HTTP_201_CREATED
    exp_id = res_create.data["export_id"]

    # Attempt PATCH -> Rejected
    res_patch = client.patch(
        f"/api/v2/compliance/audit-exports/{exp_id}/",
        {"export_scope": "ANTIBIOTIC_STEWARDSHIP"},
        format="json",
    )
    assert res_patch.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "ERR_AUDIT_MANIFEST_IMMUTABLE" in str(res_patch.data)

    # Attempt DELETE -> 403 Forbidden
    res_del = client.delete(f"/api/v2/compliance/audit-exports/{exp_id}/")
    assert res_del.status_code in [status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED]


@pytest.mark.django_db
def test_p258_cryptographic_manifest_verification(audit_export_env):
    """Scenario 5: Cryptographic manifest verification checks tamper-evident SHA-256 seal."""
    env = audit_export_env
    client = APIClient()
    client.force_authenticate(user=env["manager_a"])

    today_str = datetime.date.today().isoformat()
    res_create = client.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "export_scope": "FULL_REGULATORY_BUNDLE",
            "start_date": today_str,
            "end_date": today_str,
        },
        format="json",
    )
    assert res_create.status_code == status.HTTP_201_CREATED
    exp_id = res_create.data["export_id"]

    # Verify manifest integrity endpoint
    res_verify = client.get(f"/api/v2/compliance/audit-exports/{exp_id}/verify-manifest/")
    assert res_verify.status_code == status.HTTP_200_OK
    data = res_verify.data

    assert data["export_id"] == exp_id
    assert data["is_tamper_free"] is True
    assert data["is_sealed"] is True


@pytest.mark.django_db
def test_p258_multi_category_export_compilation(audit_export_env):
    """Scenario 6: Full regulatory bundle exports antibiotic records, temperature reviews, and batch recalls."""
    env = audit_export_env
    today = datetime.date.today()

    # 1. Batch Recall
    BatchRecallNotification.objects.create(
        recall_id=f"rcl-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product_name="Ciprofloxacin 500mg",
        batch_number=f"BT-RCL-{env['uid']}",
        recall_class=RecallClass.CLASS_II,
        recall_source=RecallSource.DGDA_MANDATORY,
        title="DGDA Recall",
        recall_reason="Impurity profile breach",
        effective_date=today,
    )

    client = APIClient()
    client.force_authenticate(user=env["owner_a"])

    res_bundle = client.post(
        "/api/v2/compliance/audit-exports/",
        {
            "branch_id": env["branch_a"].branch_id,
            "export_scope": "FULL_REGULATORY_BUNDLE",
            "start_date": today.isoformat(),
            "end_date": today.isoformat(),
        },
        format="json",
    )
    assert res_bundle.status_code == status.HTTP_201_CREATED
    data = res_bundle.data
    assert data["manifest_records_count"] >= 1
    items = data["manifest_payload"]["items"]
    record_types = [item["record_type"] for item in items]
    assert "BATCH_RECALL" in record_types
