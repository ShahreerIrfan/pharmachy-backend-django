"""
backend/tests/test_p128_override_precedence.py
Architecture Decision: ADR-0128 (P128).
Integration Tests for Global and Tenant Override Precedence, Moderation Rules,
Anti-Gouging Thresholds, Regulatory POM Non-Relaxation, and Audit Provenance.
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

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
    TenantMedicineConfig,
)
from apps.core.override_precedence_service import GlobalTenantOverridePrecedenceService
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "global_tenant_override_precedence.schema.json"
)


class OverridePrecedenceIntegrationTests(TestCase):
    """End-to-end integration tests for P128 Global & Tenant Override Precedence."""

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

        # 2. Pharmacy Tenant 1 (Uttara Pharmacy) & Staff Users
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p128-1-{self.suffix}",
            name=f"Uttara Pharmacy {self.suffix}",
            subdomain=f"uttara-p128-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p128-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Uttara Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner1 = PharmacyUser.objects.create_user(
            email=f"owner1_{self.suffix}@uttara.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Uttara Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.cashier1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@uttara.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Uttara Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        # 3. Pharmacy Tenant 2 (Gulshan Pharmacy)
        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p128-2-{self.suffix}",
            name=f"Gulshan Pharmacy {self.suffix}",
            subdomain=f"gulshan-p128-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner2 = PharmacyUser.objects.create_user(
            email=f"owner2_{self.suffix}@gulshan.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Gulshan Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant2.tenant_id,
        )

        # 4. Master Medicines
        # Product 1: Napa 500mg (OTC)
        self.med_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa 500mg",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="500mg",
            strength_numeric=Decimal("500"),
            strength_unit="mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.90"),
            barcode=f"8901234{random.randint(100000, 999999)}",
            is_prescription_only=False,
            is_narcotic=False,
            is_antibiotic=False,
        )

        # Product 2: Seclo 20mg (Prescription-Only POM)
        self.med_seclo = MedicineProduct.objects.create(
            product_id=f"prod-seclo-{self.suffix}",
            brand_name="Seclo 20mg",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            strength_numeric=Decimal("20"),
            strength_unit="mg",
            manufacturer="Square Pharmaceuticals PLC",
            mrp_unit=Decimal("6.00"),
            cost_unit=Decimal("4.80"),
            barcode=f"8901235{random.randint(100000, 999999)}",
            is_prescription_only=True,
            is_narcotic=False,
            is_antibiotic=False,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def test_01_master_values_take_effect_when_no_tenant_override(self):
        """P128 Acceptance: When no override exists, resolution cleanly defaults to global master values."""
        self.client.force_authenticate(user=self.owner1)
        response = self.client.get(
            f"/api/v2/catalog/overrides/resolve/?product_id={self.med_napa.product_id}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]

        # Validate against JSON Schema
        sub_schema = dict(self.schema_doc["definitions"]["ResolvedMedicineResponse"])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=data, schema=sub_schema)

        self.assertEqual(data["effective_brand_name"], "Napa 500mg")
        self.assertEqual(data["effective_mrp_unit"], "1.20")
        self.assertEqual(data["effective_cost_unit"], "0.90")
        self.assertFalse(data["effective_requires_prescription"])
        self.assertFalse(data["is_overridden"])
        self.assertEqual(data["provenance"]["brand_name"]["source"], "GLOBAL_MASTER")
        self.assertEqual(data["provenance"]["mrp_unit"]["source"], "GLOBAL_MASTER")
        self.assertEqual(data["provenance"]["mrp_unit"]["rule"], "MASTER_DEFAULT")

    def test_02_create_valid_override_and_verify_precedence(self):
        """P128 Acceptance: Pharmacy Owner creates valid override; effective values take precedence."""
        self.client.force_authenticate(user=self.owner1)
        payload = {
            "product_id": self.med_napa.product_id,
            "local_brand_name": "নাপা ৫০০ (স্টোর স্পেশাল)",
            "local_sku": "SKU-UTT-NAPA-01",
            "custom_mrp_unit": "1.50",
            "custom_cost_unit": "1.10",
            "shelf_location": "Rack A-3",
            "low_stock_threshold": 25,
        }
        response = self.client.post("/api/v2/catalog/overrides/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()["data"]

        self.assertEqual(data["effective_brand_name"], "নাপা ৫০০ (স্টোর স্পেশাল)")
        self.assertEqual(data["effective_mrp_unit"], "1.50")
        self.assertEqual(data["effective_cost_unit"], "1.10")
        self.assertEqual(data["effective_shelf_location"], "Rack A-3")
        self.assertEqual(data["effective_low_stock_threshold"], 25)
        self.assertTrue(data["is_overridden"])
        self.assertEqual(data["moderation_status"], "APPROVED")
        self.assertEqual(data["provenance"]["brand_name"]["source"], "TENANT_OVERRIDE")
        self.assertEqual(data["provenance"]["mrp_unit"]["source"], "TENANT_OVERRIDE")

    def test_03_excessive_price_hike_triggers_pending_review_and_master_fallback(self):
        """P128 Anti-Gouging: Price > +50% of global MRP triggers PENDING_REVIEW and enforces master MRP."""
        self.client.force_authenticate(user=self.owner1)
        # Global MRP is 1.20 BDT. Propose 3.00 BDT (> 1.20 * 1.50 = 1.80 BDT)
        payload = {
            "product_id": self.med_napa.product_id,
            "local_brand_name": "Napa Premium",
            "custom_mrp_unit": "3.00",
        }
        response = self.client.post("/api/v2/catalog/overrides/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()["data"]

        self.assertTrue(data["is_overridden"])
        self.assertEqual(data["moderation_status"], "PENDING_REVIEW")
        # Precedence Rule: Unapproved excessive price falls back to canonical master MRP (1.20)!
        self.assertEqual(data["effective_mrp_unit"], "1.20")
        self.assertEqual(data["provenance"]["mrp_unit"]["source"], "GLOBAL_MASTER")
        self.assertEqual(data["provenance"]["mrp_unit"]["rule"], "MODERATION_PENDING_FALLBACK")

    def test_04_superadmin_approves_pending_price_override(self):
        """P128 Moderation: Platform Superadmin reviews and approves pending price hike."""
        # 1. Propose excessive price
        self.client.force_authenticate(user=self.owner1)
        res = self.client.post(
            "/api/v2/catalog/overrides/",
            {"product_id": self.med_napa.product_id, "custom_mrp_unit": "2.50"},
            format="json",
        )
        override_id = res.json()["data"]["override_id"]

        # 2. Superadmin approves
        self.client.force_authenticate(user=self.superadmin)
        mod_res = self.client.post(
            f"/api/v2/catalog/overrides/moderate/{override_id}/",
            {"action": "APPROVE", "reason": "Verified special compounding formulation packaging."},
            format="json",
        )
        self.assertEqual(mod_res.status_code, status.HTTP_200_OK)
        mod_data = mod_res.json()["data"]
        self.assertEqual(mod_data["moderation_status"], "APPROVED")
        # Once approved, tenant price takes precedence!
        self.assertEqual(mod_data["effective_mrp_unit"], "2.50")
        self.assertEqual(mod_data["provenance"]["mrp_unit"]["source"], "TENANT_OVERRIDE")

    def test_05_regulatory_pom_non_relaxation_is_strictly_forbidden(self):
        """P128 Regulatory Safety: Attempt to relax prescription requirement on POM drug is rejected."""
        self.client.force_authenticate(user=self.owner1)
        payload = {
            "product_id": self.med_seclo.product_id,
            "requires_prescription_override": False,  # Illegal attempt to relax POM
        }
        response = self.client.post("/api/v2/catalog/overrides/", payload, format="json")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("requires_prescription_override", str(response.json()))
        self.assertIn("ERR_REGULATORY_RELAXATION_FORBIDDEN", str(response.json()))

    def test_06_stricter_prescription_policy_on_otc_is_permitted(self):
        """P128 Acceptance: Stricter policy on OTC drug (making it Rx locally) is permitted."""
        self.client.force_authenticate(user=self.owner1)
        payload = {
            "product_id": self.med_napa.product_id,
            "requires_prescription_override": True,  # Pharmacist counsel requirement
        }
        response = self.client.post("/api/v2/catalog/overrides/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()["data"]
        self.assertTrue(data["effective_requires_prescription"])
        self.assertEqual(data["provenance"]["is_prescription_required"]["source"], "TENANT_OVERRIDE")

    def test_07_invalid_pricing_input_rejected(self):
        """P128 Acceptance: Incomplete or negative price input is rejected with HTTP 400."""
        self.client.force_authenticate(user=self.owner1)
        payload = {
            "product_id": self.med_napa.product_id,
            "custom_mrp_unit": "-5.00",  # Negative price
        }
        response = self.client.post("/api/v2/catalog/overrides/", payload, format="json")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("custom_mrp_unit", str(response.json()))

    def test_08_cross_tenant_access_blocked(self):
        """P128 Security: Staff from Tenant 2 cannot query or mutate overrides for Tenant 1."""
        self.client.force_authenticate(user=self.owner2)
        # Attempt to resolve Tenant 1 overrides
        response = self.client.get(
            f"/api/v2/catalog/overrides/resolve/?product_id={self.med_napa.product_id}&tenant_id={self.tenant1.tenant_id}"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "ERR_CROSS_TENANT_VIOLATION" in str(response.json()) or "ERR_CROSS_TENANT_ACCESS" in str(response.json())
        )

    def test_09_cashier_role_denied_mutation(self):
        """P128 RBAC: Cashiers cannot create or mutate tenant overrides."""
        self.client.force_authenticate(user=self.cashier1)
        payload = {
            "product_id": self.med_napa.product_id,
            "local_brand_name": "Cashier Napa",
        }
        response = self.client.post("/api/v2/catalog/overrides/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_STAFF_ROLE_DENIED", str(response.json()))

    def test_10_suspended_tenant_blocked(self):
        """P128 Lifecycle: Suspended tenant cannot mutate overrides."""
        self.tenant1.status = TenantLifecycleStatus.SUSPENDED
        self.tenant1.save()

        self.client.force_authenticate(user=self.owner1)
        response = self.client.post(
            "/api/v2/catalog/overrides/",
            {"product_id": self.med_napa.product_id, "local_brand_name": "Hacked Napa"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in str(response.json())
            or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in str(response.json())
        )

    def test_11_reset_override_to_master_defaults(self):
        """P128 Lifecycle: Resetting override cleanly reverts all attributes to global master."""
        self.client.force_authenticate(user=self.owner1)
        # First create override
        self.client.post(
            "/api/v2/catalog/overrides/",
            {"product_id": self.med_napa.product_id, "local_brand_name": "Temp Override", "custom_mrp_unit": "1.40"},
            format="json",
        )
        self.assertTrue(TenantMedicineConfig.objects.filter(tenant=self.tenant1, product=self.med_napa).exists())

        # Now reset
        res = self.client.post(
            "/api/v2/catalog/overrides/reset/",
            {"product_id": self.med_napa.product_id},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]
        self.assertFalse(data["is_overridden"])
        self.assertEqual(data["effective_brand_name"], "Napa 500mg")
        self.assertEqual(data["effective_mrp_unit"], "1.20")
        self.assertFalse(TenantMedicineConfig.objects.filter(tenant=self.tenant1, product=self.med_napa).exists())

    def test_12_audit_trail_recorded(self):
        """P128 Audit: Verify immutable audit log history captures override mutations."""
        self.client.force_authenticate(user=self.owner1)
        self.client.post(
            "/api/v2/catalog/overrides/",
            {"product_id": self.med_napa.product_id, "local_brand_name": "Audited Napa", "custom_mrp_unit": "1.35"},
            format="json",
        )

        history = GlobalTenantOverridePrecedenceService.get_audit_history(
            tenant_id=self.tenant1.tenant_id,
            product_id=self.med_napa.product_id,
        )
        self.assertTrue(len(history) >= 1)
        actions = [h["action"] for h in history]
        self.assertTrue(any("TENANT_OVERRIDE" in a for a in actions))
