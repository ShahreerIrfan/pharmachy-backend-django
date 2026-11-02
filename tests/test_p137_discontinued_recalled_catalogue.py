"""
backend/tests/test_p137_discontinued_recalled_catalogue.py
Architecture Decision: ADR-0137 (P137).
Integration Tests for Discontinued & Recalled Catalogue Governance:
Safety Recalls, Market Withdrawals, POS Dispensing Blocks,
Preservation of Historical Invoices/Prescriptions, RBAC, and Audit Immutability.
"""

from __future__ import annotations

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

from apps.core.models import (
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    MedicineCurationStatus,
    MedicineProduct,
    NonMedicineCategory,
    NonMedicineRetailItem,
    PlanTier,
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
    / "catalogue_recall_lifecycle.schema.json"
)


class CatalogueRecallGovernanceIntegrationTests(TestCase):
    """End-to-end integration tests for P137 Discontinued & Recalled Catalogue."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Platform Superadmin User
        self.superadmin = PharmacyUser.objects.create_user(
            email=f"superadmin_{self.suffix}@platform.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform Superadmin",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

        # 2. Pharmacy Tenant 1 (Banani Health) & Staff
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p137-1-{self.suffix}",
            name=f"Banani Health {self.suffix}",
            subdomain=f"banani-p137-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p137-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Banani Dispensary",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.pharmacist1 = PharmacyUser.objects.create_user(
            email=f"pharmacist1_{self.suffix}@banani.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Lead Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.manager1 = PharmacyUser.objects.create_user(
            email=f"manager1_{self.suffix}@banani.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.cashier1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@banani.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Front Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        # 3. Pharmacy Tenant 2 (Gulshan Care) & Staff
        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p137-2-{self.suffix}",
            name=f"Gulshan Care {self.suffix}",
            subdomain=f"gulshan-p137-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.manager2 = PharmacyUser.objects.create_user(
            email=f"manager2_{self.suffix}@gulshan.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Gulshan Manager",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant2.tenant_id,
        )

        # 4. Suspended Tenant & Owner
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p137-susp-{self.suffix}",
            name=f"Suspended Pharmacy {self.suffix}",
            subdomain=f"susp-p137-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_owner = PharmacyUser.objects.create_user(
            email=f"susp_owner_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 5. Target Medicine Products
        self.product1 = MedicineProduct.objects.create(
            product_id=f"med-p137-1-{self.suffix}",
            brand_name=f"Paracip 500mg Syrup {self.suffix}",
            generic_name="Paracetamol",
            dosage_form="Syrup",
            strength="120mg/5ml",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("45.00"),
            cost_unit=Decimal("35.00"),
            curation_status=MedicineCurationStatus.APPROVED,
        )
        self.product2 = MedicineProduct.objects.create(
            product_id=f"med-p137-2-{self.suffix}",
            brand_name=f"OldAntibiotic 250mg {self.suffix}",
            generic_name="Erythromycin",
            dosage_form="Tablet",
            strength="250mg",
            manufacturer="Square Pharmaceuticals PLC",
            mrp_unit=Decimal("12.00"),
            cost_unit=Decimal("9.00"),
            curation_status=MedicineCurationStatus.APPROVED,
        )

        # 6. Retail Item
        self.retail_item1 = NonMedicineRetailItem.objects.create(
            item_id=f"ret-p137-{self.suffix}",
            tenant=self.tenant1,
            tenant_id_str=self.tenant1.tenant_id,
            name=f"Recalled Diagnostic Kit {self.suffix}",
            category=NonMedicineCategory.DIAGNOSTIC_DEVICES,
            mrp=Decimal("500.00"),
            cost_price=Decimal("400.00"),
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def _validate_schema(self, instance, definition_key: str):
        sub_schema = dict(self.schema_doc["definitions"][definition_key])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=instance, schema=sub_schema)

    def test_01_publish_statutory_recall_validates_json_schema(self):
        """P137 Acceptance: Publishing DGDA statutory safety recall validates JSON schema and transitions curation status."""
        self.client.force_authenticate(user=self.pharmacist1)

        payload = {
            "target_entity_type": "MEDICINE",
            "product_id": self.product1.product_id,
            "lifecycle_status": "RECALLED",
            "recall_scope": "NATIONWIDE_ALL_BATCHES",
            "regulatory_authority": "DGDA",
            "notice_number": f"DGDA/DR/2026/RC-{random.randint(1000, 9999)}",
            "reason": "Presence of diethylene glycol (DEG) impurity exceeding permissible safety limit in liquid syrup excipient.",
            "effective_date": timezone.now().isoformat(),
            "action_required": "Immediately halt dispensing and quarantine all inventory for manufacturer return.",
        }

        resp = self.client.post("/api/v2/catalog/recalls/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()

        # Schema Validation
        self._validate_schema(data, "CatalogueLifecycleNoticeRecord")

        self.assertEqual(data["lifecycle_status"], "RECALLED")
        self.assertEqual(data["recall_scope"], "NATIONWIDE_ALL_BATCHES")
        self.assertTrue(data["is_active"])
        self.assertTrue(data["is_dispensing_blocked"])
        self.assertEqual(len(data["immutable_hash"]), 64)

        # Confirm curation status transitioned
        self.product1.refresh_from_db()
        self.assertEqual(self.product1.curation_status, MedicineCurationStatus.RECALLED)

    def test_02_pos_dispensing_hard_blocked_for_recalled_product(self):
        """P137 Acceptance: POS checkout safety check and validate-dispense endpoint hard-blocks recalled product."""
        # 1. Publish recall
        self.client.force_authenticate(user=self.pharmacist1)
        notice_num = f"DGDA/RC-{random.randint(1000, 9999)}"
        self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "RECALLED",
                "regulatory_authority": "DGDA",
                "notice_number": notice_num,
                "reason": "Sub-potent active pharmaceutical ingredient batches identified.",
            },
            format="json",
        )

        # 2. Cashier checks safety status at POS
        self.client.force_authenticate(user=self.cashier1)
        check_resp = self.client.get(f"/api/v2/catalog/recalls/check/?product_id={self.product1.product_id}")
        self.assertEqual(check_resp.status_code, status.HTTP_200_OK)
        check_data = check_resp.json()
        self.assertTrue(check_data["blocked"])
        self.assertEqual(check_data["error_code"], "ERR_PRODUCT_RECALLED_DISPENSING_BLOCKED")
        self.assertEqual(check_data["notice_number"], notice_num)

        # 3. Cashier attempts to validate-dispense -> HTTP 400 Bad Request
        dispense_resp = self.client.post(
            "/api/v2/catalog/recalls/validate-dispense/",
            data={"product_id": self.product1.product_id},
            format="json",
        )
        self.assertEqual(dispense_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_PRODUCT_RECALLED_DISPENSING_BLOCKED", str(dispense_resp.json()))

    def test_03_specific_batch_recall_blocks_only_affected_batches(self):
        """P137 Acceptance: Specific batch recall blocks contaminated batch but leaves unaffected batches eligible."""
        self.client.force_authenticate(user=self.pharmacist1)
        contaminated_batch = "BAT-CONTAM-2026-X"
        safe_batch = "BAT-SAFE-2026-Y"

        self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "RECALLED",
                "recall_scope": "SPECIFIC_BATCHES",
                "affected_batch_numbers": [contaminated_batch],
                "regulatory_authority": "Beximco Quality Assurance",
                "notice_number": "BEX-QA-2026-081",
                "reason": "Seal leakage detected on specific manufacturing run.",
            },
            format="json",
        )

        # Check contaminated batch -> Blocked
        resp_contam = self.client.get(
            f"/api/v2/catalog/recalls/check/?product_id={self.product1.product_id}&batch_number={contaminated_batch}"
        )
        self.assertTrue(resp_contam.json()["blocked"])
        self.assertEqual(resp_contam.json()["lifecycle_status"], "RECALLED")

        # Check safe batch -> Allowed
        resp_safe = self.client.get(
            f"/api/v2/catalog/recalls/check/?product_id={self.product1.product_id}&batch_number={safe_batch}"
        )
        self.assertFalse(resp_safe.json()["blocked"])

    def test_04_discontinued_status_flags_item_without_hard_blocking_existing_stock(self):
        """P137 Acceptance: Discontinued product is flagged in catalogue but dispensing existing branch stock is permitted."""
        self.client.force_authenticate(user=self.manager1)
        self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product2.product_id,
                "lifecycle_status": "DISCONTINUED",
                "regulatory_authority": "Square Pharmaceuticals",
                "notice_number": "SQ-DISC-2026-01",
                "reason": "Manufacturer replaced brand with newer azithromycin formulation.",
                "action_required": "Allow depletion of on-hand branch stock. No new purchase orders permitted.",
            },
            format="json",
        )

        # Safety check: blocked is FALSE, lifecycle_status is DISCONTINUED
        resp_check = self.client.get(f"/api/v2/catalog/recalls/check/?product_id={self.product2.product_id}")
        self.assertEqual(resp_check.status_code, status.HTTP_200_OK)
        data = resp_check.json()
        self.assertFalse(data["blocked"])
        self.assertEqual(data["lifecycle_status"], "DISCONTINUED")

        # Validate dispense allows dispensing of existing stock
        val_resp = self.client.post(
            "/api/v2/catalog/recalls/validate-dispense/",
            data={"product_id": self.product2.product_id},
            format="json",
        )
        self.assertEqual(val_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(val_resp.json()["eligible_for_dispensing"])

    def test_05_historical_references_preserved_after_recall_and_discontinuation(self):
        """P137 Acceptance: Invoices and prescriptions created prior to recall maintain permanent relational integrity."""
        # Create BatchStock and SaleInvoice prior to recall
        batch = BatchStock.objects.create(
            batch_id=f"bat-p137-{self.suffix}",
            tenant=self.tenant1,
            branch=self.branch1,
            product=self.product1,
            batch_number="BATCH-HIST-01",
            quantity_base_units=100,
            expiry_date=timezone.now().date() + timezone.timedelta(days=365),
            purchase_date=timezone.now().date(),
            cost_per_unit=Decimal("35.00"),
        )
        invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-p137-{self.suffix}",
            tenant=self.tenant1,
            branch=self.branch1,
            invoice_number=f"INV-{self.suffix}",
            total_amount=Decimal("45.00"),
            net_amount=Decimal("45.00"),
            payment_method="CASH",
        )
        invoice_item = SaleInvoiceItem.objects.create(
            invoice=invoice,
            product=self.product1,
            batch=batch,
            quantity=1,
            unit_price=Decimal("45.00"),
            subtotal=Decimal("45.00"),
        )

        # Now issue recall notice
        self.client.force_authenticate(user=self.pharmacist1)
        self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "RECALLED",
                "regulatory_authority": "DGDA",
                "notice_number": "DGDA-HIST-RC-01",
                "reason": "Post-market packaging defect.",
            },
            format="json",
        )

        # Verify historical invoice remains completely intact
        invoice_item.refresh_from_db()
        self.assertEqual(invoice_item.product_id, self.product1.product_id)
        self.assertEqual(invoice_item.product.brand_name, self.product1.brand_name)
        self.assertEqual(invoice_item.subtotal, Decimal("45.00"))

    def test_06_cashier_can_check_recall_status_but_denied_from_publishing_or_lifting(self):
        """P137 Acceptance: Cashiers can query recall status for POS safety, but cannot publish or lift notices."""
        self.client.force_authenticate(user=self.cashier1)

        # Query check -> Permitted
        check_resp = self.client.get(f"/api/v2/catalog/recalls/check/?product_id={self.product1.product_id}")
        self.assertEqual(check_resp.status_code, status.HTTP_200_OK)

        # Publish notice -> Forbidden
        pub_resp = self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "RECALLED",
                "regulatory_authority": "Unauthorized Cashier",
                "notice_number": "UNAUTH-01",
                "reason": "Test unauthorized recall attempt.",
            },
            format="json",
        )
        self.assertEqual(pub_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_STAFF_ROLE_DENIED", str(pub_resp.json()))

    def test_07_cross_tenant_recall_mutation_blocked_with_403(self):
        """P137 Acceptance: Staff of Tenant 2 cannot lift or mutate recall notices owned by Tenant 1."""
        self.client.force_authenticate(user=self.pharmacist1)
        pub_resp = self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "QUARANTINED",
                "recall_scope": "TENANT_QUARANTINE",
                "regulatory_authority": "Tenant 1 Internal Audit",
                "notice_number": "TN1-QUAR-01",
                "reason": "Discoloration suspected in storage room B.",
            },
            format="json",
        )
        notice_id = pub_resp.json()["notice_id"]

        # Manager of Tenant 2 attempts to lift Tenant 1's quarantine
        self.client.force_authenticate(user=self.manager2)
        lift_resp = self.client.post(
            f"/api/v2/catalog/recalls/{notice_id}/lift/",
            data={"resolution_notes": "Unauthorized cross tenant lift"},
            format="json",
        )
        self.assertEqual(lift_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_ACCESS", str(lift_resp.json()))

    def test_08_suspended_tenant_blocked_from_publishing_with_403(self):
        """P137 Acceptance: Suspended tenant staff cannot publish recall or lifecycle notices."""
        self.client.force_authenticate(user=self.suspended_owner)
        resp = self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "RECALLED",
                "regulatory_authority": "Suspended Store",
                "notice_number": "SUSP-RC-01",
                "reason": "Suspended store attempting notice.",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        err_str = str(resp.json())
        self.assertTrue("ERR_TENANT_SUSPENDED" in err_str or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in err_str)

    def test_09_lifting_recall_restores_dispensing_eligibility_and_approved_curation(self):
        """P137 Acceptance: Lifting a recall notice deactivates the dispensing block and restores APPROVED curation status."""
        self.client.force_authenticate(user=self.pharmacist1)
        pub_resp = self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "QUARANTINED",
                "regulatory_authority": "Hospital Pharmacy Committee",
                "notice_number": "HPC-TEMP-HOLD-01",
                "reason": "Temporary quarantine pending microbiology laboratory test result.",
            },
            format="json",
        )
        notice_id = pub_resp.json()["notice_id"]

        # Confirm initially blocked
        check1 = self.client.get(f"/api/v2/catalog/recalls/check/?product_id={self.product1.product_id}")
        self.assertTrue(check1.json()["blocked"])

        # Lift the notice with lab clearance notes
        lift_resp = self.client.post(
            f"/api/v2/catalog/recalls/{notice_id}/lift/",
            data={"resolution_notes": "Lab test negative for contamination. Batch cleared by Chief Pharmacist."},
            format="json",
        )
        self.assertEqual(lift_resp.status_code, status.HTTP_200_OK)
        lift_data = lift_resp.json()
        self.assertFalse(lift_data["is_active"])
        self.assertEqual(lift_data["lifecycle_status"], "LIFTED")
        self.assertFalse(lift_data["is_dispensing_blocked"])
        self.assertEqual(lift_data["version_number"], 2)

        # Checking status now shows not blocked
        check2 = self.client.get(f"/api/v2/catalog/recalls/check/?product_id={self.product1.product_id}")
        self.assertFalse(check2.json()["blocked"])

        # Curation status restored
        self.product1.refresh_from_db()
        self.assertEqual(self.product1.curation_status, MedicineCurationStatus.APPROVED)

    def test_10_invalid_or_incomplete_input_rejected_with_400(self):
        """P137 Acceptance: Incomplete or invalid notice payloads are rejected with HTTP 400 Bad Request."""
        self.client.force_authenticate(user=self.pharmacist1)

        # 1. Missing reason
        resp1 = self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "RECALLED",
                "regulatory_authority": "DGDA",
                "notice_number": "DGDA-INV-01",
                "reason": "",  # Empty reason!
            },
            format="json",
        )
        self.assertEqual(resp1.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Invalid lifecycle status
        resp2 = self.client.post(
            "/api/v2/catalog/recalls/",
            data={
                "target_entity_type": "MEDICINE",
                "product_id": self.product1.product_id,
                "lifecycle_status": "INVALID_STATUS_VALUE",
                "regulatory_authority": "DGDA",
                "notice_number": "DGDA-INV-02",
                "reason": "Test invalid status.",
            },
            format="json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
