"""
backend/tests/test_p139_form_template_migration.py
Architecture Decision: ADR-0139 (P139).
Integration Tests for Medicine Form Template Migration Plan, Declarative Field
Transformations, Dry-Run Validation, 100% Immutable Historical Dispensing,
Reversible Snapshot Rollbacks, and Multi-Tenant RBAC & Subscription Gates.
"""

import json
import random
import uuid
from decimal import Decimal
from pathlib import Path

import jsonschema
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.dosage_form_service import DosageFormRegistryService
from apps.core.form_template_migration_service import FormTemplateMigrationService
from apps.core.medicine_form_template_service import MedicineFormTemplateService
from apps.core.models import (
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    DosageFormDefinition,
    InvoiceStatus,
    MedicineFormTemplate,
    MedicineProduct,
    MigrationPlanStatus,
    MigrationStrategy,
    PaymentMethod,
    PlanTier,
    PrescriptionItem,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "form_template_migration_plan.schema.json"
)


class FormTemplateMigrationIntegrationTests(TestCase):
    """End-to-end integration tests for P139 Form Template Migration Plan."""

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

        # 1. Superadmin User
        self.superadmin = PharmacyUser.objects.create_user(
            email=f"superadmin_{self.suffix}@platform.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform Superadmin",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

        # 2. Tenant A (Standard Plan) & Owner / Cashier / Pharmacist
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p139-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-p139-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p139-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharmacist_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Tenant B (Standard Plan)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p139-b-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p139-{self.suffix}",
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

        # 4. Tenant C (Basic Plan - Unentitled)
        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p139-c-{self.suffix}",
            name=f"Basic Rural Pharmacy {self.suffix}",
            subdomain=f"rural-p139-{self.suffix}",
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
            tenant_id=f"tn-p139-s-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"suspended-p139-{self.suffix}",
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

        # 6. Source Template v1.0 and Target Template v2.0
        self.dosage_form_tablet = DosageFormDefinition.objects.get(form_code="TABLET")
        self.source_template = MedicineFormTemplate.objects.get(template_id="tmpl-tablet-v1")

        self.target_template = MedicineFormTemplate.objects.create(
            template_id=f"tmpl-tablet-v2-{self.suffix}",
            dosage_form=self.dosage_form_tablet,
            template_code="TABLET_STANDARD",
            template_version="v2.0",
            title_en="Standard Solid Oral Tablet Form Template v2.0",
            title_bn="স্ট্যান্ডার্ড সলিড ট্যাবলেট ফরম টেমপ্লেট সংস্করণ ২.০",
            description="Enhanced layout with scored tablet indicators and coating classification.",
            is_active=True,
            is_default=False,
            layout_rules={"grid_columns": 12, "sections": []},
            allowed_values={"strength_units": ["mg", "mcg", "g"]},
            validation_rules=[],
        )

        # Injection template for invalid incompatible migration test
        self.dosage_form_injection = DosageFormDefinition.objects.get(form_code="INJECTION")
        self.injection_template = MedicineFormTemplate.objects.get(template_id="tmpl-injection-v1")

        # 7. Seed active MedicineProduct
        self.med1 = MedicineProduct.objects.create(
            product_id=f"med-p139-1-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="TABLET",
            strength="500mg+65mg",
            strength_numeric=Decimal("500.00"),
            strength_unit="mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("3.50"),
            cost_unit=Decimal("2.80"),
            dgda_dar_number=f"DAR-P139-{self.suffix}",
            source_metadata={
                "form_template_id": self.source_template.template_id,
                "form_template_version": "v1.0",
                "old_strength_note": "solid tablet oral",
                "temp_code": "code123",
            },
        )

        # 8. Seed BatchStock & Historical Dispensing (SaleInvoice + Prescription)
        self.batch = BatchStock.objects.create(
            batch_id=f"bt-p139-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.med1,
            batch_number=f"BAT-P139-{self.suffix}",
            purchase_date="2026-01-01",
            expiry_date="2027-12-31",
            quantity_base_units=100,
            cost_per_unit=Decimal("2.80"),
        )
        self.invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-p139-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number=f"INV-P139-{self.suffix}",
            customer_name="Historical Patient",
            total_amount=Decimal("35.00"),
            net_amount=Decimal("35.00"),
            status=InvoiceStatus.FINALIZED,
            payment_method=PaymentMethod.CASH,
        )
        self.invoice_item = SaleInvoiceItem.objects.create(
            invoice=self.invoice,
            batch=self.batch,
            product=self.med1,
            quantity=10,
            unit_price=Decimal("3.50"),
            subtotal=Decimal("35.00"),
        )
        self.prescription = PrescriptionRecord.objects.create(
            prescription_id=f"rx-p139-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            patient_token=f"ptk-{self.suffix}",
            doctor_name="Dr. Clinical Specialist",
            doctor_bmdc_no="BMDC-98765",
            prescription_date="2026-10-15",
            status=PrescriptionStatus.DISPENSED,
        )
        self.prescription_item = PrescriptionItem.objects.create(
            prescription=self.prescription,
            product=self.med1,
            dosage_instructions="1 tablet orally after meal",
            quantity_prescribed=10,
            is_dispensed=True,
        )

    def test_01_create_migration_plan_success(self):
        """Valid plan creation by superadmin, schema validation, and SHA-256 plan hash."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "plan_name": "Tablet Template v1.0 to v2.0 Migration",
            "from_template_id": self.source_template.template_id,
            "to_template_id": self.target_template.template_id,
            "strategy": MigrationStrategy.COERCE_SAFE,
            "field_mappings": {
                "field_renames": {"old_strength_note": "strength_notes"},
                "default_values": {"scored_tablet": True, "coating_type": "FILM_COATED"},
                "value_transforms": {"temp_code": {"action": "UPPERCASE"}},
                "deprecated_fields": ["obsolete_flag"],
            },
        }
        res = self.client.post("/api/v2/catalog/template-migrations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        data = res.json()
        jsonschema.validate(instance=data, schema=self.schema)
        self.assertEqual(data["status"], MigrationPlanStatus.DRAFT)
        self.assertEqual(len(data["plan_hash"]), 64)
        self.assertEqual(data["from_template_id"], self.source_template.template_id)
        self.assertEqual(data["to_template_id"], self.target_template.template_id)

    def test_02_validate_dry_run_success(self):
        """Dry-run audit certifies historical immutability and generates diff without DB writes."""
        self.client.force_authenticate(user=self.superadmin)
        plan_data = FormTemplateMigrationService.create_plan(
            payload={
                "plan_name": "Dry Run Test Plan",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
                "field_mappings": {
                    "field_renames": {"old_strength_note": "strength_notes"},
                    "default_values": {"scored_tablet": True},
                },
            },
            user=self.superadmin,
        )
        plan_id = plan_data["plan_id"]

        res = self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/dry-run/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.json()
        jsonschema.validate(instance=data, schema=self.schema)
        self.assertEqual(data["status"], MigrationPlanStatus.VALIDATED)
        self.assertGreaterEqual(data["affected_medicine_count"], 1)
        self.assertGreaterEqual(data["historical_dispensing_audited_count"], 2)

        # Assert zero database mutations happened during dry run
        med_fresh = MedicineProduct.objects.get(product_id=self.med1.product_id)
        self.assertEqual(med_fresh.source_metadata.get("form_template_version"), "v1.0")
        self.assertIn("old_strength_note", med_fresh.source_metadata)
        self.assertNotIn("scored_tablet", med_fresh.source_metadata)

    def test_03_execute_migration_plan_success(self):
        """Execution updates active medicines, injects defaults, renames fields, and increments version."""
        self.client.force_authenticate(user=self.superadmin)
        plan_data = FormTemplateMigrationService.create_plan(
            payload={
                "plan_name": "Execution Test Plan",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
                "field_mappings": {
                    "field_renames": {"old_strength_note": "strength_notes"},
                    "default_values": {"scored_tablet": True},
                    "value_transforms": {"temp_code": {"action": "UPPERCASE"}},
                },
            },
            user=self.superadmin,
        )
        plan_id = plan_data["plan_id"]

        # Dry run first
        self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/dry-run/")

        # Execute
        res = self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/execute/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.json()
        jsonschema.validate(instance=data, schema=self.schema)
        self.assertEqual(data["status"], MigrationPlanStatus.COMPLETED)
        self.assertTrue(data["rollback_snapshot_available"])

        # Check medicine product state
        med_fresh = MedicineProduct.objects.get(product_id=self.med1.product_id)
        self.assertEqual(med_fresh.source_metadata["form_template_version"], "v2.0")
        self.assertEqual(med_fresh.source_metadata["form_template_id"], self.target_template.template_id)
        self.assertNotIn("old_strength_note", med_fresh.source_metadata)
        self.assertEqual(med_fresh.source_metadata["strength_notes"], "solid tablet oral")
        self.assertEqual(med_fresh.source_metadata["scored_tablet"], True)
        self.assertEqual(med_fresh.source_metadata["temp_code"], "CODE123")
        self.assertGreater(med_fresh.version_number, 1)

    def test_04_historical_dispensing_immutability_guaranteed(self):
        """Historical SaleInvoiceItem and PrescriptionItem remain 100% frozen and unmodified."""
        self.client.force_authenticate(user=self.superadmin)
        plan_data = FormTemplateMigrationService.create_plan(
            payload={
                "plan_name": "Immutability Audit Plan",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            user=self.superadmin,
        )
        plan_id = plan_data["plan_id"]
        self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/execute/")

        # Verify historical sale item is intact
        inv_item = SaleInvoiceItem.objects.get(id=self.invoice_item.id)
        self.assertEqual(inv_item.quantity, 10)
        self.assertEqual(inv_item.unit_price, Decimal("3.50"))
        self.assertEqual(inv_item.subtotal, Decimal("35.00"))
        self.assertEqual(inv_item.invoice.status, InvoiceStatus.FINALIZED)

        # Verify historical prescription item is intact
        rx_item = PrescriptionItem.objects.get(id=self.prescription_item.id)
        self.assertEqual(rx_item.dosage_instructions, "1 tablet orally after meal")
        self.assertEqual(rx_item.quantity_prescribed, 10)
        self.assertEqual(rx_item.prescription.status, PrescriptionStatus.DISPENSED)

    def test_05_rollback_plan_success(self):
        """Rollback restores before-state snapshot of updated medicine products."""
        self.client.force_authenticate(user=self.superadmin)
        plan_data = FormTemplateMigrationService.create_plan(
            payload={
                "plan_name": "Rollback Test Plan",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
                "field_mappings": {
                    "field_renames": {"old_strength_note": "strength_notes"},
                    "default_values": {"scored_tablet": True},
                },
            },
            user=self.superadmin,
        )
        plan_id = plan_data["plan_id"]
        self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/execute/")

        # Verify it was changed
        med_changed = MedicineProduct.objects.get(product_id=self.med1.product_id)
        self.assertEqual(med_changed.source_metadata["form_template_version"], "v2.0")

        # Rollback
        res = self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/rollback/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data["status"], MigrationPlanStatus.ROLLED_BACK)

        # Verify restored state
        med_restored = MedicineProduct.objects.get(product_id=self.med1.product_id)
        self.assertEqual(med_restored.source_metadata.get("form_template_version"), "v1.0")
        self.assertIn("old_strength_note", med_restored.source_metadata)
        self.assertNotIn("strength_notes", med_restored.source_metadata)
        self.assertNotIn("scored_tablet", med_restored.source_metadata)

    def test_06_invalid_input_rejections(self):
        """Rejects identical templates, empty plan_name, and incompatible dosage forms."""
        self.client.force_authenticate(user=self.superadmin)

        # Case A: Identical source and target
        res1 = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "Invalid Identical",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.source_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)

        # Case B: Empty plan name
        res2 = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

        # Case C: Incompatible dosage form (Tablet -> Injection)
        res3 = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "Incompatible Dosage Form",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.injection_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)

    def test_07_cashier_role_denied(self):
        """Cashier role is strictly denied migration plan creation and execution."""
        self.client.force_authenticate(user=self.cashier_a)

        res = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "Cashier Attempted Plan",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        err = str(res.json())
        self.assertTrue("ERR_PERMISSION_DENIED" in err or "permission" in err.lower())

    def test_08_cross_tenant_access_denied(self):
        """Tenant B owner is blocked from accessing or executing Tenant A's migration plan."""
        # Tenant A owner creates plan for Tenant A
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "Tenant A Plan",
                "tenant_id": self.tenant_a.tenant_id,
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        plan_id = res.json()["plan_id"]

        # Tenant B owner tries to execute Tenant A's plan
        self.client.force_authenticate(user=self.owner_b)
        res_cross = self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/execute/")
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)
        err = str(res_cross.json())
        self.assertTrue("ERR_CROSS_TENANT_ACCESS" in err or "ERR_CROSS_TENANT_VIOLATION" in err)

    def test_09_suspended_tenant_and_unentitled_basic_plan(self):
        """Suspended tenant and Basic tier unentitled tenants are blocked."""
        # Suspended tenant blocked
        self.client.force_authenticate(user=self.owner_suspended)
        res_susp = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "Suspended Attempt",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res_susp.status_code, status.HTTP_403_FORBIDDEN)
        err_susp = str(res_susp.json())
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in err_susp
            or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in err_susp
        )

        # Basic tier tenant blocked from template migration feature
        self.client.force_authenticate(user=self.owner_basic)
        res_basic = self.client.post(
            "/api/v2/catalog/template-migrations/",
            data={
                "plan_name": "Basic Tier Attempt",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            format="json",
        )
        self.assertEqual(res_basic.status_code, status.HTTP_403_FORBIDDEN)
        err_basic = str(res_basic.json())
        self.assertTrue("ERR_SUBSCRIPTION_UNENTITLED" in err_basic)

    def test_10_cannot_reexecute_completed_plan(self):
        """Cannot execute an already completed migration plan."""
        self.client.force_authenticate(user=self.superadmin)
        plan_data = FormTemplateMigrationService.create_plan(
            payload={
                "plan_name": "Double Execution Test",
                "from_template_id": self.source_template.template_id,
                "to_template_id": self.target_template.template_id,
            },
            user=self.superadmin,
        )
        plan_id = plan_data["plan_id"]
        res1 = self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/execute/")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Re-execute attempt
        res2 = self.client.post(f"/api/v2/catalog/template-migrations/{plan_id}/execute/")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been executed", str(res2.json()))
