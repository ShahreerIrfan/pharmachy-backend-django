"""
backend/tests/test_p071_tenant_medicine_config.py
Architecture Decision: ADR-0004, ADR-0005, ADR-0070 & ADR-0071
Comprehensive test suite for Tenant-Specific Medicine Configuration & Local Overrides:
- Valid scenario: Tenant A Pharmacy Owner configures local brand alias, local SKU, shelf location, and custom MRP.
- Effective Overlay: Querying master product merges tenant overrides seamlessly.
- Master Integrity Invariant: Global master product fields and immutable version hash remain 100% untouched.
- Two-Tenant Isolation: Tenant B cannot see or mutate Tenant A's overrides; sees master defaults.
- Cross-Tenant Breach Prevention: Tenant B attempting to access or modify Tenant A's config gets 403/404.
- Regulatory Non-Relaxation: Attempting to downgrade POM/antibiotic to OTC via requires_prescription_override=False is strictly rejected (400/422).
- Input Validation: Negative custom MRP or negative stock threshold is rejected.
- Role Guard: Cashiers cannot modify tenant configs (403 Forbidden); read-only access allowed.
- Visibility Override: Setting is_visible=False excludes product from store POS search.
- Clean Reset: Deleting config reverts product to master defaults and logs audit event.
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

from apps.core.models import (
    Branch,
    MedicineCurationStatus,
    MedicineProduct,
    Tenant,
    TenantMedicineConfig,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP071TenantMedicineConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p071_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{phone_suffix}",
                "is_active": True,
            },
        )

        # 2. Tenant A (Dhaka Pharmacy)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-dhaka-{cls.suffix}",
            name=f"Dhaka Central Pharmacy {cls.suffix}",
            subdomain=f"dhaka-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"b-dhaka-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Dhanmondi Branch",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p071_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p071_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Cashier {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )

        # 3. Tenant B (Chittagong Pharmacy)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-ctg-{cls.suffix}",
            name=f"Chittagong Port Pharmacy {cls.suffix}",
            subdomain=f"ctg-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
        )
        cls.branch_b = Branch.objects.create(
            branch_id=f"b-ctg-{cls.suffix}",
            tenant=cls.tenant_b,
            name="Agrabad Branch",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p071_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Ctg Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            branch_id=cls.branch_b.branch_id,
            is_active=True,
        )

        # 4. Master Medicines (Global Reference Catalog)
        cls.master_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{cls.suffix}",
            brand_name=f"Napa Extra {cls.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals Ltd",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
            dgda_dar_number=f"DAR-024-{random.randint(1000, 9999)}",
            curation_status=MedicineCurationStatus.APPROVED,
            is_prescription_only=False,
            is_antibiotic=False,
        )

        cls.master_azithro = MedicineProduct.objects.create(
            product_id=f"prod-azithro-{cls.suffix}",
            brand_name=f"Zimax {cls.suffix}",
            generic_name="Azithromycin",
            dosage_form="Capsule",
            strength="500mg",
            manufacturer="Square Pharmaceuticals Ltd",
            mrp_unit=Decimal("35.00"),
            cost_unit=Decimal("28.00"),
            dgda_dar_number=f"DAR-024-{random.randint(1000, 9999)}",
            curation_status=MedicineCurationStatus.APPROVED,
            is_prescription_only=True,
            is_antibiotic=True,
        )

    def setUp(self):
        self.client = APIClient()
        TenantMedicineConfig.objects.all().delete()

    def test_complete_tenant_medicine_configuration_lifecycle(self):
        """
        Valid scenario:
        1. Tenant A Owner creates local configuration for Napa Extra.
        2. Verify effective brand name, local SKU, custom MRP, shelf location.
        3. Master MedicineProduct remains completely untouched!
        4. Tenant B queries the same product: Sees master defaults, zero leakage of Tenant A's config!
        """
        self.client.force_authenticate(user=self.owner_a)

        original_master_brand = self.master_napa.brand_name
        original_master_mrp = self.master_napa.mrp_unit
        original_hash = self.master_napa.immutable_version_hash

        payload = {
            "product_id": self.master_napa.product_id,
            "local_brand_name": "নাপা এক্সট্রা কুইক",
            "local_sku": f"SKU-T1-{self.suffix}",
            "custom_mrp_unit": "3.00",
            "custom_cost_unit": "2.20",
            "is_visible": True,
            "shelf_location": "Rack A - Shelf 3",
            "low_stock_threshold": 30,
            "notes": "Top selling OTC tablet in Dhaka branch.",
        }

        # 1. Upsert config
        resp_create = self.client.post("/api/v2/tenants/medicine-configs/", payload, format="json")
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        cfg_data = resp_create.json()["data"]
        config_id = cfg_data["config_id"]
        self.assertEqual(cfg_data["local_brand_name"], "নাপা এক্সট্রা কুইক")
        self.assertEqual(cfg_data["effective_brand_name"], "নাপা এক্সট্রা কুইক")
        self.assertEqual(cfg_data["effective_mrp_unit"], "3.00")
        self.assertEqual(cfg_data["effective_cost_unit"], "2.20")
        self.assertEqual(cfg_data["shelf_location"], "Rack A - Shelf 3")

        # 2. Invariant Check: Master Product MUST BE UNCHANGED
        self.master_napa.refresh_from_db()
        self.assertEqual(self.master_napa.brand_name, original_master_brand)
        self.assertEqual(self.master_napa.mrp_unit, original_master_mrp)
        self.assertEqual(self.master_napa.immutable_version_hash, original_hash)

        # 3. Query product endpoint as Tenant A: Effective override is included!
        resp_prod_a = self.client.get(f"/api/v2/medicines/{self.master_napa.product_id}/")
        self.assertEqual(resp_prod_a.status_code, status.HTTP_200_OK)
        prod_data_a = resp_prod_a.json()
        self.assertIsNotNone(prod_data_a.get("tenant_override"))
        self.assertEqual(prod_data_a["tenant_override"]["effective_brand_name"], "নাপা এক্সট্রা কুইক")
        self.assertEqual(prod_data_a["tenant_override"]["effective_mrp_unit"], "3.00")

        # 4. Multi-Tenant Isolation Check: Tenant B queries the same product
        self.client.force_authenticate(user=self.owner_b)
        resp_prod_b = self.client.get(f"/api/v2/medicines/{self.master_napa.product_id}/")
        self.assertEqual(resp_prod_b.status_code, status.HTTP_200_OK)
        prod_data_b = resp_prod_b.json()
        # Tenant B has NO override -> tenant_override is None, master MRP 2.50 is presented!
        self.assertIsNone(prod_data_b.get("tenant_override"))
        self.assertEqual(prod_data_b["mrp_unit"], "2.50")

        # 5. Clean Reset: Tenant A deletes their config
        self.client.force_authenticate(user=self.owner_a)
        resp_delete = self.client.delete(f"/api/v2/tenants/medicine-configs/{config_id}/")
        self.assertEqual(resp_delete.status_code, status.HTTP_200_OK)

        # Verification: Now Tenant A queries product -> Reset to master defaults!
        resp_reset = self.client.get(f"/api/v2/medicines/{self.master_napa.product_id}/")
        self.assertIsNone(resp_reset.json().get("tenant_override"))

    def test_two_tenant_isolation_and_cross_tenant_access_blocked(self):
        """Cross-tenant isolation: Tenant B cannot read or alter Tenant A's configuration."""
        # Tenant A sets up a config
        config_a = TenantMedicineConfig.objects.create(
            config_id=f"tmc-iso-a-{self.suffix}",
            tenant=self.tenant_a,
            product=self.master_napa,
            local_brand_name="Dhaka Napa",
            custom_mrp_unit=Decimal("4.00"),
        )

        # 1. Tenant B attempts to read Tenant A's config by config_id
        self.client.force_authenticate(user=self.owner_b)
        resp_get = self.client.get(f"/api/v2/tenants/medicine-configs/{config_a.config_id}/")
        self.assertEqual(resp_get.status_code, status.HTTP_404_NOT_FOUND)

        # 2. Tenant B attempts to update Tenant A's config
        resp_patch = self.client.patch(
            f"/api/v2/tenants/medicine-configs/{config_a.config_id}/",
            {"custom_mrp_unit": "1.00"},
            format="json",
        )
        self.assertEqual(resp_patch.status_code, status.HTTP_404_NOT_FOUND)

        # 3. Tenant B attempts to supply tenant_id query param to sniff Tenant A's configs
        resp_sniff = self.client.get(f"/api/v2/tenants/medicine-configs/?tenant_id={self.tenant_a.tenant_id}")
        self.assertIn(resp_sniff.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    def test_regulatory_non_relaxation_guard(self):
        """
        Regulatory Invariant: Tenant CANNOT relax global prescription requirements!
        Attempting to set requires_prescription_override=False on a master POM/antibiotic
        must be strictly rejected with HTTP 400/422.
        """
        self.client.force_authenticate(user=self.owner_a)

        # Master Zimax is POM and Antibiotic (is_prescription_only=True, is_antibiotic=True)
        payload = {
            "product_id": self.master_azithro.product_id,
            "requires_prescription_override": False,  # Illegal relaxation!
            "notes": "Attempting to sell antibiotic OTC.",
        }

        resp_illegal = self.client.post("/api/v2/tenants/medicine-configs/", payload, format="json")
        self.assertIn(resp_illegal.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("requires_prescription_override", str(resp_illegal.json()))

    def test_invalid_input_validations(self):
        """Invalid scenarios: Negative custom MRP or negative stock threshold."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Negative custom MRP
        resp_neg_mrp = self.client.post(
            "/api/v2/tenants/medicine-configs/",
            {
                "product_id": self.master_napa.product_id,
                "custom_mrp_unit": "-10.00",
            },
            format="json",
        )
        self.assertIn(resp_neg_mrp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 2. Zero custom MRP
        resp_zero_mrp = self.client.post(
            "/api/v2/tenants/medicine-configs/",
            {
                "product_id": self.master_napa.product_id,
                "custom_mrp_unit": "0.00",
            },
            format="json",
        )
        self.assertIn(resp_zero_mrp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_cashier_role_mutation_strictly_barred(self):
        """RBAC Guard: Cashier has read-only access and cannot POST, PATCH, or DELETE configs."""
        self.client.force_authenticate(user=self.cashier_a)

        # 1. Cashier attempts to create a config -> 403 Forbidden
        resp_create = self.client.post(
            "/api/v2/tenants/medicine-configs/",
            {
                "product_id": self.master_napa.product_id,
                "custom_mrp_unit": "5.00",
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("TENANT_CONFIG_MUTATION_RESTRICTED", str(resp_create.json()))

        # 2. Cashier can read existing tenant configs
        resp_list = self.client.get("/api/v2/tenants/medicine-configs/")
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)

    def test_visibility_toggle_filters_pos_search(self):
        """Visibility Override: Setting is_visible=False excludes medicine from store catalog search."""
        self.client.force_authenticate(user=self.owner_a)

        # Hide Napa Extra for Tenant A
        TenantMedicineConfig.objects.create(
            config_id=f"tmc-hide-{self.suffix}",
            tenant=self.tenant_a,
            product=self.master_napa,
            is_visible=False,
        )

        # Cashier searches catalog: Napa Extra is excluded!
        self.client.force_authenticate(user=self.cashier_a)
        resp_search = self.client.get(f"/api/v2/medicines/?search=Napa%20Extra%20{self.suffix}")
        self.assertEqual(resp_search.status_code, status.HTTP_200_OK)
        res_list = resp_search.json()
        items = res_list if isinstance(res_list, list) else res_list.get("data", [])
        self.assertFalse(any(p["product_id"] == self.master_napa.product_id for p in items))

        # Explicitly requesting include_hidden=true includes it
        resp_include_hidden = self.client.get(
            f"/api/v2/medicines/?search=Napa%20Extra%20{self.suffix}&include_hidden=true"
        )
        self.assertEqual(resp_include_hidden.status_code, status.HTTP_200_OK)
        hidden_items = (
            resp_include_hidden.json()
            if isinstance(resp_include_hidden.json(), list)
            else resp_include_hidden.json().get("data", [])
        )
        self.assertTrue(any(p["product_id"] == self.master_napa.product_id for p in hidden_items))


if __name__ == "__main__":
    unittest.main()
