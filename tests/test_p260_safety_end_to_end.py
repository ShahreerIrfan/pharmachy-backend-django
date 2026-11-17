"""
backend/tests/test_p260_safety_end_to_end.py
Architecture Decision: ADR-0260 (P260).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Safety End-to-End Test Suite:
- Scenario 1: Valid dispensing workflow with prescription attachment, registered pharmacist verification,
              antibiotic register entry generation, and Decimal financial precision.
- Scenario 2: Prohibited non-Rx dispensing: unverified sale blocked with ERR_RX_VERIFICATION_REQUIRED.
- Scenario 3: Stale classification escalation invalidates cart line clearance and requires re-verification.
- Scenario 4: Recalled batch blocked dynamically at POS dispensing gate (ERR_BATCH_RECALLED_DISPENSING_BLOCKED).
- Scenario 5: Expired prescription (> 365 days old) strictly rejected (ERR_EXPIRED_OR_FUTURE_PRESCRIPTION).
- Scenario 6: Unauthorized review: Cashier blocked with ERR_PHARMACIST_APPROVAL_REQUIRED, and cross-tenant isolation.
- Scenario 7: Privacy-safe compliance audit export with automated PHI redaction and composite SHA-256 integrity seal.
"""

import datetime
from decimal import Decimal
import random
import uuid

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.batch_recall_service import BatchRecallService
from apps.core.classification_snapshot_service import MedicineClassificationSnapshotService
from apps.core.compliance_audit_export_service import ComplianceAuditExportService
from apps.core.models import (
    AntibioticDispenseRegisterEntry,
    AuditExportScope,
    BatchRecallNotification,
    BatchStatus,
    BatchStock,
    Branch,
    ClassificationType,
    ComplianceAuditExportManifest,
    MedicineClassificationSnapshot,
    MedicineCurationStatus,
    MedicineProduct,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosRxGateStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PrescriptionRecord,
    PrescriptionStatus,
    RecallClass,
    RecallNoticeStatus,
    RecallSource,
    SaleInvoice,
    Tenant,
)
from apps.core.pos_rx_safety_service import PosRxSafetyService, _RxRejectionAbort
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def safety_e2e_env():
    uid = uuid.uuid4().hex[:6]

    # Platform Superadmin
    superadmin = PharmacyUser.objects.create_superuser(
        email=f"superadmin_{uid}@platform.gov.bd",
        phone_number=f"+88019{random.randint(10000000, 99999999)}",
        password="SuperPassword123!",
        role=UserRole.SUPERADMIN,
    )

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p260-a-{uid}", name="Square Health Pharma A", subdomain=f"p260a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p260-a-{uid}", name="Mirpur Main Branch", branch_code="MP01"
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
        pcb_registration_no=f"PCB-A-{random.randint(10000, 99999)}",
    )

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant Boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p260-b-{uid}", name="Beximco Partner B", subdomain=f"p260b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p260-b-{uid}", name="Banani Branch B", branch_code="BN01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.PHARMACY_OWNER,
    )

    # Products
    product_abx = MedicineProduct.objects.create(
        product_id=f"prod-abx-{uid}",
        brand_name="Moxaclav 625mg",
        generic_name="Amoxicillin + Clavulanic Acid",
        dosage_form="Tablet",
        strength="625mg",
        manufacturer="Square Pharmaceuticals Ltd",
        mrp_unit=Decimal("45.00"),
        cost_unit=Decimal("36.00"),
        is_antibiotic=True,
        is_prescription_only=True,
        curation_status=MedicineCurationStatus.APPROVED,
    )

    product_otc = MedicineProduct.objects.create(
        product_id=f"prod-otc-{uid}",
        brand_name="Paracetamol 500mg",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500mg",
        manufacturer="Beximco Pharmaceuticals Ltd",
        mrp_unit=Decimal("3.00"),
        cost_unit=Decimal("2.00"),
        is_antibiotic=False,
        is_prescription_only=False,
        curation_status=MedicineCurationStatus.APPROVED,
    )

    return {
        "uid": uid,
        "superadmin": superadmin,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "pharmacist_a": pharmacist_a,
        "cashier_a": cashier_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
        "product_abx": product_abx,
        "product_otc": product_otc,
    }


@pytest.mark.django_db
def test_scenario_1_valid_dispensing_workflow_and_antibiotic_register(safety_e2e_env):
    """
    Scenario 1: Full legitimate dispensing journey for antibiotic medicine:
    1. Attach valid prescription with BMDC doctor details.
    2. Registered pharmacist verifies prescription with PCB registration.
    3. Assert Rx gate for finalization passes.
    4. Idempotently generate AntibioticDispenseRegisterEntry on finalization.
    5. Financial metrics conform to Decimal precision (0.00 BDT).
    """
    env = safety_e2e_env
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-s1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-S1-{env['uid'].upper()}",
        subtotal_bdt=Decimal("450.00"),
    )

    line = PosSaleOrderLine.objects.create(
        line_id=f"line-s1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["product_abx"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=10,
        conversion_factor=1,
        base_units_total=10,
        unit_price_bdt=Decimal("45.00"),
        unit_cost_bdt=Decimal("36.00"),
        line_subtotal_bdt=Decimal("450.00"),
        line_total_bdt=Decimal("450.00"),
        requires_rx_verification=True,
        rx_verified=False,
        regulatory_classification=PosRegulatoryClass.ANTIMICROBIAL_ACCESS,
    )

    # 1. Attach Prescription
    sale = PosRxSafetyService.attach_prescription_to_sale(
        user=env["cashier_a"],
        sale=sale,
        data={
            "doctor_name": "Dr. Tanvir Ahmed",
            "doctor_bmdc_no": "BMDC-A-98765",
            "patient_token": "PT-S1-TEST",
            "prescription_date": timezone.now().date().isoformat(),
            "diagnosis": "Bacterial Respiratory Infection",
        },
    )
    assert sale.prescription is not None
    assert sale.prescription.doctor_bmdc_no == "BMDC-A-98765"

    # 2. Pharmacist Verification
    sale = PosRxSafetyService.verify_sale_rx(
        user=env["pharmacist_a"],
        sale=sale,
        data={"pharmacist_pcb_no": env["pharmacist_a"].pcb_registration_no},
        notes="Clinical indications verified, dosage confirmed.",
    )
    assert sale.rx_gate_status == PosRxGateStatus.APPROVED
    assert sale.rx_verified_by == env["pharmacist_a"]

    # 3. Assert Gate For Finalization (Must pass)
    summary = PosRxSafetyService.assert_rx_gate_for_finalization(
        user=env["pharmacist_a"],
        sale=sale,
    )
    assert summary["can_finalize_online"] is True

    # 4. Finalize invoice & register antibiotic entry
    invoice = SaleInvoice.objects.create(
        invoice_id=f"inv-s1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        invoice_number=f"INV-S1-{env['uid'].upper()}",
        total_amount=Decimal("450.00"),
        net_amount=Decimal("450.00"),
    )

    entries = PosRxSafetyService.record_antibiotic_register_on_finalize(
        user=env["pharmacist_a"],
        sale=sale,
        invoice=invoice,
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.doctor_bmdc_no == "BMDC-A-98765"
    assert entry.approving_pharmacist_pcb_no == env["pharmacist_a"].pcb_registration_no
    assert entry.dispensed_base_units == 10
    assert entry.patient_token == "PT-S1-TEST"

    # Verify Decimal Precision
    assert isinstance(line.line_total_bdt, Decimal)
    assert line.line_total_bdt == Decimal("450.00")


@pytest.mark.django_db
def test_scenario_2_prohibited_non_rx_dispensing(safety_e2e_env):
    """
    Scenario 2: Prohibited non-Rx dispensing:
    Attempting to finalize an unverified prescription-only order is strictly blocked
    with ERR_RX_VERIFICATION_REQUIRED.
    """
    env = safety_e2e_env
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-s2-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-S2-{env['uid'].upper()}",
    )

    PosSaleOrderLine.objects.create(
        line_id=f"line-s2-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["product_abx"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=5,
        conversion_factor=1,
        base_units_total=5,
        unit_price_bdt=Decimal("45.00"),
        unit_cost_bdt=Decimal("36.00"),
        line_subtotal_bdt=Decimal("225.00"),
        line_total_bdt=Decimal("225.00"),
        requires_rx_verification=True,
        rx_verified=False,
        regulatory_classification=PosRegulatoryClass.ANTIMICROBIAL_ACCESS,
    )

    # Finalization attempt without pharmacist sign-off -> ValidationError
    with pytest.raises(ValidationError) as exc_info:
        PosRxSafetyService.assert_rx_gate_for_finalization(
            user=env["cashier_a"],
            sale=sale,
        )

    error_dict = exc_info.value.detail
    assert error_dict["error_code"] == "ERR_RX_VERIFICATION_REQUIRED"


@pytest.mark.django_db
def test_scenario_3_stale_classification_invalidation(safety_e2e_env):
    """
    Scenario 3: Stale classification escalation invalidates prior cart clearance:
    1. Initially OTC product is placed on cart (snapshot_version = 1).
    2. Product is escalated to PRESCRIPTION_ONLY via new snapshot (snapshot_version = 2).
    3. PosRxSafetyService detects stale snapshot and escalates requirement to requires_rx_verification=True.
    """
    env = safety_e2e_env
    product = env["product_otc"]

    # Initial snapshot v1 as OTC
    snap1 = MedicineClassificationSnapshotService.create_snapshot(
        product_id=product.product_id,
        data={
            "classification_type": ClassificationType.OTC,
            "regulatory_source": "DGDA Initial List",
            "effective_from": timezone.now() - datetime.timedelta(days=30),
            "review_notes": "Initial OTC status",
        },
        actor=env["superadmin"],
    )
    assert snap1["version_number"] == 1

    # Cart created with line at snapshot version 1
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-s3-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-S3-{env['uid'].upper()}",
    )

    line = PosSaleOrderLine.objects.create(
        line_id=f"line-s3-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=product,
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=10,
        conversion_factor=1,
        base_units_total=10,
        unit_price_bdt=Decimal("3.00"),
        unit_cost_bdt=Decimal("2.00"),
        line_subtotal_bdt=Decimal("30.00"),
        line_total_bdt=Decimal("30.00"),
        requires_rx_verification=False,
        rx_verified=True,  # Initially cleared as OTC
        regulatory_classification=PosRegulatoryClass.OTC,
        classification_version_snapshot=1,
    )

    # Escalate product to PRESCRIPTION_ONLY (snapshot v2)
    snap2 = MedicineClassificationSnapshotService.create_snapshot(
        product_id=product.product_id,
        data={
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "DGDA Gazette Notification 2026",
            "effective_from": timezone.now(),
            "review_notes": "Immediate escalation to POM due to safety directive",
        },
        actor=env["superadmin"],
    )
    assert snap2["version_number"] == 2

    # Live sync re-evaluation: Must invalidate prior clearance and demand Rx
    summary = PosRxSafetyService.evaluate_and_sync_sale_rx_gate(sale, actor_user=env["cashier_a"], mutate=True)
    line.refresh_from_db()

    assert summary["requires_rx_verification"] is True
    assert line.requires_rx_verification is True
    assert line.rx_verified is False
    assert line.classification_version_snapshot == 2


@pytest.mark.django_db
def test_scenario_4_recalled_batch_dispensing_gate(safety_e2e_env):
    """
    Scenario 4: Recalled batch blocking:
    Dispensing gate blocks recalled batches with ERR_BATCH_RECALLED_DISPENSING_BLOCKED.
    """
    env = safety_e2e_env
    recalled_batch_no = f"RECALL-BATCH-{env['uid'].upper()}"

    # Publish recall notice
    BatchRecallNotification.objects.create(
        recall_id=f"rec-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        batch_number=recalled_batch_no,
        product=env["product_abx"],
        product_name=env["product_abx"].brand_name,
        recall_class=RecallClass.CLASS_I,
        recall_source=RecallSource.DGDA_MANDATORY,
        status=RecallNoticeStatus.PUBLISHED,
        title="Microbial contamination recall",
        recall_reason="Microbial contamination detected in sample testing.",
        published_by=env["pharmacist_a"],
    )

    # POS dispensing gate check must fail
    check = BatchRecallService.check_dispensing_gate(
        tenant_id=env["tenant_a"].tenant_id,
        batch_number=recalled_batch_no,
        branch_id=env["branch_a"].branch_id,
    )
    assert check["is_dispensing_allowed"] is False
    assert check["error_code"] == "ERR_BATCH_RECALLED_DISPENSING_BLOCKED"
    assert check["recall_class"] == RecallClass.CLASS_I


@pytest.mark.django_db
def test_scenario_5_expired_prescription_rejected(safety_e2e_env):
    """
    Scenario 5: Expired prescription (> 365 days old) is strictly rejected with ERR_EXPIRED_OR_FUTURE_PRESCRIPTION.
    """
    env = safety_e2e_env
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-s5-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-S5-{env['uid'].upper()}",
    )

    # Date > 365 days ago (e.g. 400 days in the past)
    expired_date = (timezone.now().date() - datetime.timedelta(days=400)).isoformat()

    with pytest.raises(ValidationError) as exc_info:
        PosRxSafetyService.attach_prescription_to_sale(
            user=env["cashier_a"],
            sale=sale,
            data={
                "doctor_name": "Dr. Rafiqul Islam",
                "doctor_bmdc_no": "BMDC-A-12345",
                "patient_token": "PT-S5-TEST",
                "prescription_date": expired_date,
            },
        )

    error_dict = exc_info.value.detail
    assert error_dict["error_code"] == "ERR_EXPIRED_OR_FUTURE_PRESCRIPTION"


@pytest.mark.django_db
def test_scenario_6_unauthorized_review_and_cross_tenant_isolation(safety_e2e_env):
    """
    Scenario 6: Role authorization and cross-tenant isolation:
    1. Cashier attempting pharmacist approval is blocked with ERR_PHARMACIST_APPROVAL_REQUIRED.
    2. Tenant B user cannot access or verify Tenant A sale order.
    """
    env = safety_e2e_env
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-s6-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-S6-{env['uid'].upper()}",
    )

    # 1. Cashier attempts verification -> PermissionDenied
    with pytest.raises(PermissionDenied) as exc_cashier:
        PosRxSafetyService.verify_sale_rx(
            user=env["cashier_a"],
            sale=sale,
            data={"pharmacist_pcb_no": "PCB-FAKE-000"},
        )
    assert exc_cashier.value.detail["error_code"] == "ERR_PHARMACIST_APPROVAL_REQUIRED"

    # 2. Tenant B attempts verification -> NotFound / PermissionDenied (Tenant boundary)
    with pytest.raises((PermissionDenied, NotFound)):
        PosRxSafetyService.verify_sale_rx(
            user=env["owner_b"],
            sale=sale,
            data={"pharmacist_pcb_no": "PCB-B-12345"},
        )


@pytest.mark.django_db
def test_scenario_7_safe_compliance_audit_extraction(safety_e2e_env):
    """
    Scenario 7: Safe compliance audit export extracts antibiotic register
    with automated PHI redactions and composite cryptographic digest integrity.
    """
    env = safety_e2e_env

    # Populate verified antibiotic register entry
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-s7-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier_a"],
        order_number=f"SO-S7-{env['uid'].upper()}",
    )
    line = PosSaleOrderLine.objects.create(
        line_id=f"line-s7-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=env["product_abx"],
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=10,
        conversion_factor=1,
        base_units_total=10,
        unit_price_bdt=Decimal("45.00"),
        unit_cost_bdt=Decimal("36.00"),
        line_subtotal_bdt=Decimal("450.00"),
        line_total_bdt=Decimal("450.00"),
        requires_rx_verification=True,
        rx_verified=True,
        regulatory_classification=PosRegulatoryClass.ANTIMICROBIAL_ACCESS,
    )
    invoice = SaleInvoice.objects.create(
        invoice_id=f"inv-s7-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        invoice_number=f"INV-S7-{env['uid'].upper()}",
        total_amount=Decimal("450.00"),
        net_amount=Decimal("450.00"),
    )

    AntibioticDispenseRegisterEntry.objects.create(
        register_entry_id=f"abx-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale_line=line,
        sale=sale,
        invoice=invoice,
        product=env["product_abx"],
        brand_name=env["product_abx"].brand_name,
        generic_name=env["product_abx"].generic_name,
        dispensed_base_units=10,
        doctor_name="Dr. Kamal Hossain",
        doctor_bmdc_no="BMDC-A-44332",
        approving_pharmacist=env["pharmacist_a"],
        approving_pharmacist_pcb_no=env["pharmacist_a"].pcb_registration_no,
        patient_token="PT-SECURE-9988",
    )

    today = timezone.now().date()
    yesterday = today - datetime.timedelta(days=1)
    tomorrow = today + datetime.timedelta(days=1)

    export_res = ComplianceAuditExportService.generate_audit_export(
        user=env["pharmacist_a"],
        data={
            "branch_id": env["branch_a"].branch_id,
            "export_scope": AuditExportScope.ANTIBIOTIC_STEWARDSHIP,
            "audit_period_start": yesterday.isoformat(),
            "audit_period_end": tomorrow.isoformat(),
            "is_patient_data_redacted": True,
        },
    )

    assert export_res["is_sealed"] is True
    assert len(export_res["manifest_sha256"]) == 64
    assert export_res["manifest_records_count"] >= 1
    assert export_res["is_patient_data_redacted"] is True

    # Verify integrity verification
    ver = ComplianceAuditExportService.verify_manifest_integrity(
        user=env["pharmacist_a"],
        export_id=export_res["export_id"],
    )
    assert ver["is_tamper_free"] is True
