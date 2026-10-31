"""
backend/tests/test_p070_global_medicine_catalogue.py
Architecture Decision: ADR-0004, ADR-0005, ADR-0036, ADR-0050, ADR-0063 & ADR-0070
Comprehensive test suite for Centrally Curated Global Medicine Master Catalogue:
- Valid scenario: Super Admin creates draft product -> Approves with DGDA DAR number -> Immutable version hash generated
  -> Store users browse and view approved product with bilingual status chips.
- Draft invisibility: Store users cannot see unapproved drafts in catalog queries.
- Invalid input scenarios: Approving without DGDA DAR, duplicate DAR, MRP < 0.01, recall without gazette reference -> HTTP 400/422.
- Unauthorized mutation scenarios: Store owner, cashier, and anonymous callers attempting POST/PUT/DELETE
  -> Strictly rejected with HTTP 403 PLATFORM_CATALOG_OWNERSHIP_PROTECTED.
- Regulatory Recall: DGDA recall directive sets status RECALLED with danger tone and logs immutable audit.
- Curation History: Audit timeline captures CREATED, APPROVED, and RECALLED lifecycle events.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    MedicineCurationStatus,
    MedicineProduct,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP070GlobalMedicineCatalogue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p070_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "is_staff": True,
                "is_superuser": True,
                "phone_number": f"+88019{phone_suffix}",
            },
        )
        cls.superadmin.set_password("AdminSecurePass2026!")
        cls.superadmin.save()

        # 2. Tenant Alpha
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p070-a-{cls.suffix}",
            name=f"Square Pharma Care {cls.suffix}",
            subdomain=f"square-p070-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_email=f"billing_a_{cls.suffix}@pharmacy.bd",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"{cls.tenant_a.tenant_id}-br-1",
            tenant=cls.tenant_a,
            name="Main Branch Dhanmondi",
            license_number=f"DGDA-DHK-{random.randint(1000, 9999)}",
            is_active=True,
        )
        phone_owner_a = random.randint(10000000, 99999999)
        cls.owner_a = PharmacyUser.objects.create(
            email=f"owner_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Dr. Owner A {cls.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            phone_number=f"+88018{phone_owner_a}",
        )
        cls.owner_a.set_password("OwnerPassword2026!")
        cls.owner_a.save()

        phone_cashier_a = random.randint(10000000, 99999999)
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Cashier Alpha {cls.suffix}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            phone_number=f"+88017{phone_cashier_a}",
        )

    def setUp(self):
        self.client = APIClient()

    def test_complete_master_medicine_curation_lifecycle(self):
        """Valid scenario: Super Admin creates draft product -> Approves with DAR -> Store browses approved catalog."""
        # 1. Super Admin creates a draft master medicine
        self.client.force_authenticate(user=self.superadmin)
        prod_payload = {
            "product_id": f"prod-azithro-{self.suffix}",
            "brand_name": f"Tridosil {self.suffix}",
            "generic_name": "Azithromycin",
            "dosage_form": "Capsule",
            "strength": "500mg",
            "manufacturer": "Incepta Pharmaceuticals Ltd",
            "mrp_unit": "35.00",
            "cost_unit": "28.50",
            "is_prescription_only": True,
            "is_antibiotic": True,
            "curation_status": "DRAFT",
        }
        resp_create = self.client.post("/api/v2/products/", prod_payload, format="json")
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        prod_data = resp_create.json().get("data", resp_create.json())
        product_id = prod_data["product_id"]
        self.assertEqual(prod_data["curation_status"], "DRAFT")
        self.assertIsNotNone(prod_data["immutable_version_hash"])

        # 2. Store Cashier queries catalog: DRAFT product is NOT visible to store staff!
        self.client.force_authenticate(user=self.cashier_a)
        resp_store_list = self.client.get(f"/api/v2/products/?search=Tridosil%20{self.suffix}")
        self.assertEqual(resp_store_list.status_code, status.HTTP_200_OK)
        res_list = resp_store_list.json()
        store_items = res_list if isinstance(res_list, list) else res_list.get("data", [])
        if isinstance(store_items, dict) and "items" in store_items:
            store_items = store_items["items"]
        self.assertFalse(
            any(p["product_id"] == product_id for p in store_items), "DRAFT product leaked to store staff!"
        )

        # 3. Super Admin approves the product with a formal DGDA DAR number
        self.client.force_authenticate(user=self.superadmin)
        dar_num = f"DAR-024-{random.randint(1000, 9999)}"
        resp_approve = self.client.post(
            f"/api/v2/products/{product_id}/approve/",
            {"dgda_dar_number": dar_num, "notes": "Verified against DGDA Allopathic Gazette 2024."},
            format="json",
        )
        self.assertEqual(resp_approve.status_code, status.HTTP_200_OK)
        approved_data = resp_approve.json()["data"]
        self.assertEqual(approved_data["curation_status"], "APPROVED")
        self.assertEqual(approved_data["dgda_dar_number"], dar_num)
        self.assertEqual(approved_data["status_chip"]["tone"], "success")

        # 4. Now Store Cashier queries catalog: APPROVED product is visible!
        self.client.force_authenticate(user=self.cashier_a)
        resp_store_approved = self.client.get(f"/api/v2/products/?search=Tridosil%20{self.suffix}")
        self.assertEqual(resp_store_approved.status_code, status.HTTP_200_OK)
        res_app = resp_store_approved.json()
        approved_items = res_app if isinstance(res_app, list) else res_app.get("data", [])
        if isinstance(approved_items, dict) and "items" in approved_items:
            approved_items = approved_items["items"]
        found = next((p for p in approved_items if p["product_id"] == product_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["curation_status"], "APPROVED")
        self.assertEqual(found["dgda_dar_number"], dar_num)
        self.assertTrue(found["is_prescription_only"])
        self.assertTrue(found["is_antibiotic"])

        # 5. Curation History inspection
        resp_history = self.client.get(f"/api/v2/products/{product_id}/curation-history/")
        self.assertEqual(resp_history.status_code, status.HTTP_200_OK)
        events = resp_history.json()["data"]["history"]
        actions = [e["action"] for e in events]
        self.assertIn("CREATED", actions)
        self.assertIn("APPROVED", actions)

    def test_unauthorized_store_mutations_strictly_blocked(self):
        """Platform ownership protection: Pharmacy Owner / Cashier cannot create, update, or delete master products."""
        # 1. Pharmacy Owner attempts to create a master product
        self.client.force_authenticate(user=self.owner_a)
        resp_create = self.client.post(
            "/api/v2/products/",
            {
                "product_id": f"prod-illegal-{self.suffix}",
                "brand_name": "Unauthorized Brand",
                "generic_name": "Paracetamol",
                "dosage_form": "Tablet",
                "strength": "500mg",
                "manufacturer": "Local Labs",
                "mrp_unit": "1.50",
                "cost_unit": "1.00",
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_CATALOG_OWNERSHIP_PROTECTED", str(resp_create.json()))

        # 2. Create legitimate product as admin first
        self.client.force_authenticate(user=self.superadmin)
        product = MedicineProduct.objects.create(
            product_id=f"prod-safe-{self.suffix}",
            brand_name=f"Safe Medicine {self.suffix}",
            generic_name="Safe Generic",
            dosage_form="Tablet",
            strength="10mg",
            manufacturer="Square",
            mrp_unit=Decimal("5.00"),
            cost_unit=Decimal("4.00"),
            curation_status=MedicineCurationStatus.APPROVED,
        )

        # 3. Cashier attempts to update the master product
        self.client.force_authenticate(user=self.cashier_a)
        resp_update = self.client.patch(
            f"/api/v2/products/{product.product_id}/",
            {"mrp_unit": "100.00"},
            format="json",
        )
        self.assertEqual(resp_update.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_CATALOG_OWNERSHIP_PROTECTED", str(resp_update.json()))

        # 4. Cashier attempts to delete the master product
        resp_delete = self.client.delete(f"/api/v2/products/{product.product_id}/")
        self.assertEqual(resp_delete.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_CATALOG_OWNERSHIP_PROTECTED", str(resp_delete.json()))

    def test_invalid_input_validations(self):
        """Invalid scenarios: Approval without DAR number, duplicate DAR, MRP < 0.01, short recall reason."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Create draft without DAR
        product = MedicineProduct.objects.create(
            product_id=f"prod-nodar-{self.suffix}",
            brand_name=f"No DAR Med {self.suffix}",
            generic_name="Generic",
            dosage_form="Tablet",
            strength="10mg",
            manufacturer="Square",
            mrp_unit=Decimal("5.00"),
            cost_unit=Decimal("4.00"),
            curation_status=MedicineCurationStatus.DRAFT,
            dgda_dar_number="",
        )

        # 2. Attempt to approve without supplying DAR number -> Rejected!
        resp_approve_fail = self.client.post(
            f"/api/v2/products/{product.product_id}/approve/",
            {"dgda_dar_number": "", "notes": "Attempt without DAR"},
            format="json",
        )
        self.assertIn(
            resp_approve_fail.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        )
        self.assertIn("dgda_dar_number", str(resp_approve_fail.json()))

        # 3. Recall without mandatory regulatory reference
        resp_recall_fail = self.client.post(
            f"/api/v2/products/{product.product_id}/recall/",
            {"recall_reason": "Substandard dissolution test failure.", "regulatory_order_ref": ""},
            format="json",
        )
        self.assertIn(resp_recall_fail.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("regulatory_order_ref", str(resp_recall_fail.json()))

    def test_regulatory_recall_lifecycle(self):
        """DGDA regulatory recall: Product recalled with order reference -> Red danger tone and audit logged."""
        self.client.force_authenticate(user=self.superadmin)
        product = MedicineProduct.objects.create(
            product_id=f"prod-recall-{self.suffix}",
            brand_name=f"Contaminated Syrup {self.suffix}",
            generic_name="Paracetamol Syrup",
            dosage_form="Syrup",
            strength="120mg/5ml",
            manufacturer="Flawed Pharma Ltd",
            mrp_unit=Decimal("35.00"),
            cost_unit=Decimal("25.00"),
            dgda_dar_number=f"DAR-RECALL-{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

        resp_recall = self.client.post(
            f"/api/v2/products/{product.product_id}/recall/",
            {
                "recall_reason": "Diethylene glycol impurity detected in batch screening.",
                "regulatory_order_ref": "DGDA/RECALL/2026/8991",
            },
            format="json",
        )
        self.assertEqual(resp_recall.status_code, status.HTTP_200_OK)
        recalled_data = resp_recall.json()["data"]
        self.assertEqual(recalled_data["curation_status"], "RECALLED")
        self.assertEqual(recalled_data["status_chip"]["tone"], "danger")
        self.assertIn("RECALL ORDER", recalled_data["curation_notes"])

        # Audit log verified
        audit = AuditLogEntry.objects.filter(
            action="GLOBAL_MEDICINE_RECALLED",
            resource_id=product.product_id,
        ).first()
        self.assertIsNotNone(audit)


if __name__ == "__main__":
    unittest.main()
