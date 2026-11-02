"""
Integration tests for P127: Manufacturer & Supplier Catalogs (ADR-0127).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path

from django.test import TestCase
from jsonschema import validate
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    Manufacturer,
    PlanTier,
    Supplier,
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
    / "manufacturer_supplier_catalogs.schema.json"
)


class ManufacturerSupplierCatalogsIntegrationTests(TestCase):
    """End-to-end integration tests for P127 Manufacturer & Supplier Catalogs, Merges and Archival."""

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

        # 2. Pharmacy Tenant 1 (Popular Pharma) & Users
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p127-1-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p127-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p127-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner1 = PharmacyUser.objects.create_user(
            email=f"owner1_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant1.tenant_id,
        )
        self.cashier1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        # 3. Pharmacy Tenant 2 (Square Care) & Owner
        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p127-2-{self.suffix}",
            name=f"Square Care {self.suffix}",
            subdomain=f"sqr-p127-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner2 = PharmacyUser.objects.create_user(
            email=f"owner2_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant2.tenant_id,
        )

        # 4. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-susp-p127-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p127-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"suspended_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_create_manufacturer_by_superadmin_and_validate_json_schema(self):
        """Superadmin creates canonical manufacturer and validates against JSON Schema."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.post(
            "/api/v2/catalog/manufacturers/",
            data={
                "name": f"Square Pharmaceuticals Ltd. {self.suffix}",
                "name_bn": "স্কয়ার ফার্মাসিউটিক্যালস লি.",
                "code": f"SQUARE_{self.suffix}",
                "dgda_license_no": f"DGDA-ML-{self.suffix}",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        self.assertEqual(data["entity_type"], "MANUFACTURER")
        self.assertTrue(data["is_active"])
        self.assertIsNone(data["merged_into_id"])

    def test_02_merge_manufacturers_by_superadmin(self):
        """Superadmin merges duplicate manufacturer into canonical target."""
        self.client.force_authenticate(user=self.superadmin)
        target = Manufacturer.objects.create(
            manufacturer_id=f"mfr-target-{self.suffix}",
            name=f"Beximco Pharmaceuticals Ltd. {self.suffix}",
            code=f"BEX_TGT_{self.suffix}",
            dgda_license_no="DGDA-002",
        )
        source = Manufacturer.objects.create(
            manufacturer_id=f"mfr-source-{self.suffix}",
            name=f"Beximco Pharma (Duplicate) {self.suffix}",
            code=f"BEX_SRC_{self.suffix}",
            dgda_license_no="DGDA-002",
        )

        resp = self.client.post(
            f"/api/v2/catalog/manufacturers/{source.manufacturer_id}/merge/",
            data={"target_id": target.manufacturer_id, "reason": "Duplicate registration reconciliation"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["is_merged"])
        self.assertEqual(data["target_id"], target.manufacturer_id)

        # Verify source is now inactive and references target
        source.refresh_from_db()
        self.assertFalse(source.is_active)
        self.assertEqual(source.merged_into_id, target.manufacturer_id)

    def test_03_cannot_merge_manufacturer_into_self(self):
        """Self-merge is rejected with ERR_CANNOT_MERGE_SELF."""
        self.client.force_authenticate(user=self.superadmin)
        m = Manufacturer.objects.create(
            manufacturer_id=f"mfr-self-{self.suffix}",
            name=f"Incepta Pharmaceuticals {self.suffix}",
            code=f"INCEPTA_{self.suffix}",
        )
        resp = self.client.post(
            f"/api/v2/catalog/manufacturers/{m.manufacturer_id}/merge/",
            data={"target_id": m.manufacturer_id},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertEqual(resp.json().get("error_code"), "ERR_CANNOT_MERGE_SELF")

    def test_04_tenant_owner_cannot_mutate_manufacturers(self):
        """Pharmacy owner cannot create or mutate global manufacturers; blocked with PLATFORM_CATALOG_OWNERSHIP_PROTECTED."""
        self.client.force_authenticate(user=self.owner1)
        resp = self.client.post(
            "/api/v2/catalog/manufacturers/",
            data={"name": "Tenant Unauthorized Pharma", "code": "UNAUTH"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json().get("error_code"), "PLATFORM_CATALOG_OWNERSHIP_PROTECTED")

    def test_05_create_and_retrieve_tenant_supplier(self):
        """Pharmacy owner creates tenant supplier and verifies schema contract."""
        self.client.force_authenticate(user=self.owner1)
        resp = self.client.post(
            "/api/v2/suppliers/",
            data={
                "name": f"Square Dhanmondi Depot {self.suffix}",
                "contact_person": "Mr. Asif",
                "phone": "+8801711223344",
                "email": "dhanmondi@square.com",
                "dl_number": f"DL-DHK-{self.suffix}",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        self.assertEqual(data["entity_type"], "SUPPLIER")
        self.assertEqual(data["tenant_id"], self.tenant1.tenant_id)
        self.assertTrue(data["is_active"])

    def test_06_merge_suppliers_within_tenant(self):
        """Pharmacy owner merges duplicate supplier into canonical target supplier."""
        self.client.force_authenticate(user=self.owner1)
        target = Supplier.objects.create(
            supplier_id=f"sup-tgt-{self.suffix}",
            tenant=self.tenant1,
            name=f"Square Main Depot {self.suffix}",
            phone="01711111111",
        )
        source = Supplier.objects.create(
            supplier_id=f"sup-src-{self.suffix}",
            tenant=self.tenant1,
            name=f"Square Depot Duplicate {self.suffix}",
            phone="01711111111",
        )

        resp = self.client.post(
            f"/api/v2/suppliers/{source.supplier_id}/merge/",
            data={"target_id": target.supplier_id, "reason": "Local vendor record deduplication"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["is_merged"])
        self.assertEqual(data["target_id"], target.supplier_id)

        source.refresh_from_db()
        self.assertFalse(source.is_active)
        self.assertEqual(source.merged_into_id, target.supplier_id)

    def test_07_cross_tenant_supplier_access_blocked(self):
        """Tenant 1 user cannot access or merge Tenant 2 supplier; blocked with ERR_CROSS_TENANT_ACCESS."""
        self.client.force_authenticate(user=self.owner1)
        sup_t2 = Supplier.objects.create(
            supplier_id=f"sup-t2-{self.suffix}",
            tenant=self.tenant2,
            name="Confidential Supplier Tenant 2",
        )

        # Attempt to retrieve Tenant 2 supplier
        resp = self.client.get(f"/api/v2/suppliers/{sup_t2.supplier_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json().get("error_code"), "ERR_CROSS_TENANT_ACCESS")

    def test_08_cashier_role_denied_supplier_merge_or_archive(self):
        """Cashier role is denied merge and archive actions on suppliers."""
        self.client.force_authenticate(user=self.cashier1)
        s = Supplier.objects.create(
            supplier_id=f"sup-cashier-test-{self.suffix}",
            tenant=self.tenant1,
            name="Test Supplier Cashier",
        )
        resp_merge = self.client.post(
            f"/api/v2/suppliers/{s.supplier_id}/merge/",
            data={"target_id": "dummy_target"},
            format="json",
        )
        self.assertEqual(resp_merge.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_merge.json().get("error_code"), "ERR_STAFF_ROLE_DENIED")

        resp_arch = self.client.post(f"/api/v2/suppliers/{s.supplier_id}/archive/")
        self.assertEqual(resp_arch.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_arch.json().get("error_code"), "ERR_STAFF_ROLE_DENIED")

    def test_09_suspended_tenant_blocked(self):
        """User from a suspended tenant is blocked with HTTP 403."""
        self.client.force_authenticate(user=self.suspended_user)
        self.client.handler._force_tenant = self.suspended_tenant
        resp = self.client.get(
            "/api/v2/suppliers/",
            HTTP_X_TENANT_ID=self.suspended_tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_10_unauthenticated_request_rejected(self):
        """Unauthenticated request returns HTTP 401."""
        resp = self.client.get("/api/v2/suppliers/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
