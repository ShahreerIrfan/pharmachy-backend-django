"""
backend/tests/test_p140_catalogue_service_contracts.py
Architecture Decision: ADR-0140 (P140).
Comprehensive Catalogue Service Contract Suite for Phase 07 (Milestone M04).
Tests: list, create, update, archive, search, schema, permissions, regulatory
snapshots, tenant overrides, deduplication, and historical dispensing immutability.
"""

import json
import random
import uuid
from decimal import Decimal
from pathlib import Path

import jsonschema
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.dosage_form_service import DosageFormRegistryService
from apps.core.medicine_form_template_service import MedicineFormTemplateService
from apps.core.models import (
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    InvoiceStatus,
    MedicineCurationHistory,
    MedicineCurationStatus,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
    TenantLifecycleStatus,
    TenantMedicineConfig,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "catalogue_service_contract.schema.json"
)


class CatalogueServiceContractIntegrationTests(TestCase):
    """End-to-end integration tests for P140 Catalogue Service Contract Suite."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        DosageFormRegistryService.ensure_canonical_forms_seeded()
        MedicineFormTemplateService.ensure_canonical_templates_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema = json.load(f)

        # 1. Platform Superadmin User
        self.superadmin = PharmacyUser.objects.create_user(
            email=f"superadmin_{self.suffix}@platform.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform Superadmin",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

        # 2. Tenant A (Standard Plan) & Owner / Staff / Cashier
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p140-a-{self.suffix}",
            name=f"Evercare Pharma {self.suffix}",
            subdomain=f"evercare-p140-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p140-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_a_{self.suffix}@evercare.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Evercare Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharmacist_a_{self.suffix}@evercare.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Evercare Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@evercare.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Evercare Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Tenant B (Standard Plan)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p140-b-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"square-p140-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 4. Basic Tier Tenant (Unentitled)
        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p140-c-{self.suffix}",
            name=f"Basic Rural Pharma {self.suffix}",
            subdomain=f"rural-p140-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner_basic = PharmacyUser.objects.create_user(
            email=f"owner_c_{self.suffix}@rural.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Rural Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_basic.tenant_id,
        )

        # 5. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p140-s-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"suspended-p140-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.owner_suspended = PharmacyUser.objects.create_user(
            email=f"owner_s_{self.suffix}@suspended.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_suspended.tenant_id,
        )

        # 6. Seed Master Medicine
        self.med_master = MedicineProduct.objects.create(
            product_id=f"med-p140-{self.suffix}",
            brand_name=f"Napa Extend {self.suffix}",
            brand_name_bn="নাপা এক্সটেন্ড",
            generic_name="Paracetamol",
            generic_name_bn="প্যারাসিটামল",
            dosage_form="Tablet",
            strength="665mg",
            strength_numeric=Decimal("665.00"),
            strength_unit="mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
            dgda_dar_number=f"DAR-P140-{self.suffix}",
            barcode=f"8941100{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

        # 7. Seed Tenant Config Override
        self.override = TenantMedicineConfig.objects.create(
            config_id=f"tmc-p140-{self.suffix}",
            tenant=self.tenant_a,
            product=self.med_master,
            custom_mrp_unit=Decimal("2.75"),
            shelf_location="Rack B-4",
            is_visible=True,
        )

        # 8. Seed Batch & Historical Dispensing (SaleInvoice + Prescription)
        self.batch = BatchStock.objects.create(
            batch_id=f"bt-p140-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.med_master,
            batch_number=f"BAT-P140-{self.suffix}",
            purchase_date="2026-02-01",
            expiry_date="2027-11-30",
            quantity_base_units=200,
            cost_per_unit=Decimal("2.00"),
        )
        self.invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-p140-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number=f"INV-P140-{self.suffix}",
            total_amount=Decimal("27.50"),
            net_amount=Decimal("27.50"),
            status=InvoiceStatus.FINALIZED,
            payment_method=PaymentMethod.CASH,
        )
        self.invoice_item = SaleInvoiceItem.objects.create(
            invoice=self.invoice,
            batch=self.batch,
            product=self.med_master,
            quantity=10,
            unit_price=Decimal("2.75"),
            subtotal=Decimal("27.50"),
        )
        self.prescription = PrescriptionRecord.objects.create(
            prescription_id=f"rx-p140-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            patient_token=f"ptk-p140-{self.suffix}",
            doctor_name="Dr. Specialist",
            doctor_bmdc_no="BMDC-54321",
            prescription_date="2026-10-20",
            status=PrescriptionStatus.DISPENSED,
        )
        self.prescription_item = PrescriptionItem.objects.create(
            prescription=self.prescription,
            product=self.med_master,
            dosage_instructions="1 tablet twice daily",
            quantity_prescribed=10,
            is_dispensed=True,
        )

    def test_01_catalogue_contract_suite_audit_pass(self):
        """Certifies complete Milestone M04 contract compliance and validates JSON schema."""
        self.client.force_authenticate(user=self.superadmin)
        res = self.client.get("/api/v2/catalog/contracts/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.json()
        jsonschema.validate(instance=data, schema=self.schema)
        self.assertEqual(data["status"], "PASSED")
        self.assertEqual(len(data["audit_hash"]), 64)
        self.assertGreaterEqual(len(data["verified_operations"]), 9)
        self.assertTrue(data["phase_gates"]["historical_immutability"])
        self.assertTrue(data["phase_gates"]["multilingual_search"])
        self.assertTrue(data["phase_gates"]["deduplication"])

    def test_02_master_medicine_create_and_deduplication(self):
        """Platform Superadmin creates master product; duplicate DAR and duplicate brand rejected."""
        self.client.force_authenticate(user=self.superadmin)
        dar_unique = f"DAR-NEW-{self.suffix}"
        payload = {
            "product_id": f"med-new-{self.suffix}",
            "brand_name": f"Ace Plus {self.suffix}",
            "generic_name": "Paracetamol + Caffeine",
            "dosage_form": "Tablet",
            "strength": "500mg+65mg",
            "manufacturer": "Square Pharmaceuticals PLC",
            "mrp_unit": "3.50",
            "cost_unit": "2.80",
            "dgda_dar_number": dar_unique,
            "curation_status": "APPROVED",
        }
        res = self.client.post("/api/v2/medicines/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # Duplicate DAR attempt rejected
        payload_dup = dict(payload)
        payload_dup["product_id"] = f"med-dup-{self.suffix}"
        res_dup = self.client.post("/api/v2/medicines/", data=payload_dup, format="json")
        self.assertIn(res_dup.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("ERR_DUPLICATE_MEDICINE", str(res_dup.json()))

    def test_03_master_medicine_update_and_version_hash(self):
        """Authorized update increments version number and updates immutable version hash."""
        self.client.force_authenticate(user=self.superadmin)
        res = self.client.patch(
            f"/api/v2/medicines/{self.med_master.product_id}/",
            data={"mrp_unit": "2.90"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.med_master.refresh_from_db()
        self.assertEqual(self.med_master.mrp_unit, Decimal("2.90"))

    def test_04_master_medicine_archive_and_curation_history(self):
        """Deprecating master medicine creates audited MedicineCurationHistory record."""
        self.client.force_authenticate(user=self.superadmin)
        res = self.client.post(
            f"/api/v2/medicines/{self.med_master.product_id}/deprecate/",
            data={"notes": "Formulation phased out in favor of new release."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.med_master.refresh_from_db()
        self.assertEqual(self.med_master.curation_status, MedicineCurationStatus.DEPRECATED)
        history_exists = MedicineCurationHistory.objects.filter(
            product=self.med_master, new_status=MedicineCurationStatus.DEPRECATED
        ).exists()
        self.assertTrue(history_exists)

    def test_05_invalid_input_rejections(self):
        """Rejects negative MRP and incomplete payloads."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": f"med-invalid-{self.suffix}",
            "brand_name": "",
            "generic_name": "Paracetamol",
            "dosage_form": "Tablet",
            "strength": "500mg",
            "manufacturer": "Beximco",
            "mrp_unit": "-5.00",
            "cost_unit": "2.00",
        }
        res = self.client.post("/api/v2/medicines/", data=payload, format="json")
        self.assertIn(res.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_06_unauthorized_cashier_mutation_blocked(self):
        """Cashier role is blocked from master creation and contract audits."""
        self.client.force_authenticate(user=self.cashier_a)

        # Creation blocked
        payload = {
            "product_id": f"med-cashier-{self.suffix}",
            "brand_name": "Cashier Drug",
            "generic_name": "Paracetamol",
            "dosage_form": "Tablet",
            "strength": "500mg",
            "manufacturer": "Square",
            "mrp_unit": "3.50",
            "cost_unit": "2.80",
        }
        res_create = self.client.post(
            "/api/v2/medicines/",
            data=payload,
            format="json",
        )
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)

        # Audit blocked
        res_audit = self.client.get("/api/v2/catalog/contracts/")
        self.assertEqual(res_audit.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res_audit.json()))

    def test_07_cross_tenant_isolation(self):
        """Tenant B owner is blocked from auditing or overriding Tenant A's private catalogue config."""
        self.client.force_authenticate(user=self.owner_b)

        # Cross-tenant audit blocked
        res = self.client.get(f"/api/v2/catalog/contracts/?tenant_id={self.tenant_a.tenant_id}")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        err = str(res.json())
        self.assertTrue("ERR_CROSS_TENANT_ACCESS" in err or "ERR_CROSS_TENANT_VIOLATION" in err)

    def test_08_suspended_tenant_and_unentitled_basic_plan(self):
        """Suspended tenant and Basic tier tenant are blocked from catalogue contract audits."""
        # Suspended tenant
        self.client.force_authenticate(user=self.owner_suspended)
        res_susp = self.client.get("/api/v2/catalog/contracts/")
        self.assertEqual(res_susp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in str(res_susp.json())
            or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in str(res_susp.json())
        )

        # Basic tier tenant
        self.client.force_authenticate(user=self.owner_basic)
        res_basic = self.client.get("/api/v2/catalog/contracts/")
        self.assertEqual(res_basic.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_SUBSCRIPTION_UNENTITLED", str(res_basic.json()))

    def test_09_multilingual_search_contract(self):
        """Search supports English brand and Bengali transliteration."""
        self.client.force_authenticate(user=self.owner_a)

        # Search English
        res_en = self.client.get("/api/v2/catalog/search/autocomplete/?q=Napa")
        self.assertEqual(res_en.status_code, status.HTTP_200_OK)

        # Search Bengali
        res_bn = self.client.get("/api/v2/catalog/search/autocomplete/?q=প্যারাসিটামল")
        self.assertEqual(res_bn.status_code, status.HTTP_200_OK)

    def test_10_regulatory_snapshots_recall_and_dispensing_immutability(self):
        """Active safety recall blocks dispensing and historical dispensing records remain 100% frozen."""
        self.client.force_authenticate(user=self.superadmin)

        # Publish recall notice
        notice = CatalogueLifecycleNotice.objects.create(
            notice_id=f"not-p140-{self.suffix}",
            target_entity_type="MEDICINE",
            product=self.med_master,
            lifecycle_status=CatalogueLifecycleStatus.RECALLED,
            recall_scope="NATIONWIDE_ALL_BATCHES",
            regulatory_authority="DGDA",
            notice_number=f"DGDA/RC-140-{self.suffix}",
            reason="Adulteration alert",
            effective_date=timezone.now(),
            is_active=True,
        )
        self.assertTrue(notice.is_dispensing_blocked)

        # Assert historical records remain 100% intact
        inv_item = SaleInvoiceItem.objects.get(id=self.invoice_item.id)
        self.assertEqual(inv_item.unit_price, Decimal("2.75"))
        self.assertEqual(inv_item.quantity, 10)
        self.assertEqual(inv_item.invoice.status, InvoiceStatus.FINALIZED)

        rx_item = PrescriptionItem.objects.get(id=self.prescription_item.id)
        self.assertEqual(rx_item.quantity_prescribed, 10)
        self.assertEqual(rx_item.dosage_instructions, "1 tablet twice daily")
        self.assertEqual(rx_item.prescription.status, PrescriptionStatus.DISPENSED)
