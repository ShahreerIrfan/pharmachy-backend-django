"""
backend/tests/test_p156_local_inventory_fields.py
Architecture Decision: ADR-0156 (P156).
Integration test suite for Local Inventory-Specific Fields.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    LocalInventoryConfig,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestLocalInventoryFieldsIntegration:
    """Integration test suite for P156 Local Inventory Fields."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p156-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p156-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p156-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p156-b-{self.suffix}",
            name=f"Beximco Health {self.suffix}",
            subdomain=f"beximco-p156-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p156-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p156-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"suspended-p156-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p156-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_tenant_b = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@beximco.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Beximco Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Susp Owner {self.suffix}",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 3. Products
        self.product_1 = MedicineProduct.objects.create(
            product_id=f"prod-1-{self.suffix}",
            brand_name=f"Ace Plus {self.suffix}",
            generic_name="Paracetamol 500mg + Caffeine 65mg",
            dosage_form="TABLET",
            strength="500mg+65mg",
            manufacturer="Square Pharmaceuticals",
            cost_unit=Decimal("2.00"),
            mrp_unit=Decimal("2.50"),
        )
        self.product_2 = MedicineProduct.objects.create(
            product_id=f"prod-2-{self.suffix}",
            brand_name=f"Napa Rapid {self.suffix}",
            generic_name="Paracetamol Actizorb",
            dosage_form="TABLET",
            strength="500mg",
            manufacturer="Beximco Pharmaceuticals",
            cost_unit=Decimal("1.80"),
            mrp_unit=Decimal("2.25"),
        )

    def test_inventory_schema(self):
        """Validates canonical local inventory schema endpoint returns constraints."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/local-inventory/schema/")
        assert res.status_code == 200
        data = res.json()
        assert data["editor_type"] == "LOCAL_INVENTORY_FORM"
        assert data["min_selling_price_bdt"] == 0.01
        assert data["default_reorder_level"] == 10

    def test_validate_inventory_valid(self):
        """Dry-run validation passes with valid local SKU, selling price and location."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "branch_id": self.branch_a.branch_id,
            "local_sku": f"SKU-DH-{self.suffix[:4]}",
            "local_selling_price_bdt": 2.40,
            "reorder_level": 25,
            "shelf_location": "Rack-A2 / Shelf-3 / Bin-14",
            "is_visible": True,
            "min_order_quantity": 10,
            "max_capacity_units": 500,
        }
        res = self.client.post("/api/v2/catalog/local-inventory/validate-inventory/", data=payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["local_selling_price_bdt"] == 2.40
        assert data["reorder_level"] == 25
        assert data["shelf_location"] == "Rack-A2 / Shelf-3 / Bin-14"

    def test_validate_inventory_invalid_sku_format(self):
        """Invalid characters in local SKU triggers 422 ERR_INVALID_SKU_FORMAT."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "local_sku": "SKU#BAD@CHARS!",
        }
        res = self.client.post("/api/v2/catalog/local-inventory/validate-inventory/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_SKU_FORMAT"

    def test_validate_inventory_invalid_price(self):
        """Selling price below 0.01 BDT triggers 422 ERR_INVALID_SELLING_PRICE."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "local_selling_price_bdt": 0.00,
        }
        res = self.client.post("/api/v2/catalog/local-inventory/validate-inventory/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_SELLING_PRICE"

    def test_validate_inventory_capacity_below_min_order(self):
        """Max capacity less than min order quantity triggers 422 ERR_CAPACITY_BELOW_MIN_ORDER."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "min_order_quantity": 50,
            "max_capacity_units": 20,
        }
        res = self.client.post("/api/v2/catalog/local-inventory/validate-inventory/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_CAPACITY_BELOW_MIN_ORDER"

    def test_create_local_inventory_config_by_pharmacist(self):
        """Pharmacist can create branch inventory override with audit log entry."""
        self.client.force_authenticate(user=self.user_pharmacist)
        sku = f"SKU-DH-{self.suffix}"
        payload = {
            "product_id": str(self.product_1.product_id),
            "branch_id": self.branch_a.branch_id,
            "local_sku": sku,
            "local_selling_price_bdt": 2.45,
            "reorder_level": 15,
            "shelf_location": "Rack-1 / Bin-A",
            "is_visible": True,
            "notes": "Fast moving OTC item",
        }
        res = self.client.post("/api/v2/catalog/local-inventory/", data=payload, format="json")
        assert res.status_code == 201
        data = res.json()
        assert data["local_sku"] == sku
        assert data["local_selling_price_bdt"] == 2.45
        assert data["reorder_level"] == 15
        assert data["branch_name"] == "Dhanmondi Branch"

        # Verify DB entry
        config_id = data["config_id"]
        cfg = LocalInventoryConfig.objects.get(pk=config_id)
        assert cfg.shelf_location == "Rack-1 / Bin-A"

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(resource_id=config_id, action="LOCAL_INVENTORY_CREATED").first()
        assert audit is not None
        assert audit.actor_email == self.user_pharmacist.email

    def test_cashier_write_forbidden(self):
        """Cashier role cannot create or modify local inventory configurations."""
        self.client.force_authenticate(user=self.user_cashier)
        payload = {
            "product_id": str(self.product_1.product_id),
            "local_sku": "SKU-CASHIER",
        }
        res = self.client.post("/api/v2/catalog/local-inventory/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_suspended_tenant_mutation_blocked(self):
        """Suspended tenant user is blocked from modifying inventory configurations."""
        self.client.force_authenticate(user=self.user_suspended)
        payload = {
            "product_id": str(self.product_1.product_id),
            "local_sku": "SKU-SUSP",
        }
        res = self.client.post("/api/v2/catalog/local-inventory/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_list_inventory_configs_tenant_isolation(self):
        """Tenant A cannot view Tenant B's local inventory configurations."""
        cfg_a = LocalInventoryConfig.objects.create(
            config_id=f"cfg-a-{self.suffix}",
            product=self.product_1,
            tenant=self.tenant_a,
            branch=self.branch_a,
            local_sku=f"SKUA-{self.suffix}",
            reorder_level=20,
        )
        cfg_b = LocalInventoryConfig.objects.create(
            config_id=f"cfg-b-{self.suffix}",
            product=self.product_2,
            tenant=self.tenant_b,
            branch=self.branch_b,
            local_sku=f"SKUB-{self.suffix}",
            reorder_level=30,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/local-inventory/")
        assert res.status_code == 200
        ids = [c["config_id"] for c in res.json()]
        assert cfg_a.config_id in ids
        assert cfg_b.config_id not in ids

    def test_cross_tenant_patch_prohibited(self):
        """Tenant B user cannot update Tenant A's inventory configuration."""
        cfg_a = LocalInventoryConfig.objects.create(
            config_id=f"cfg-cross-{self.suffix}",
            product=self.product_1,
            tenant=self.tenant_a,
            branch=self.branch_a,
            local_sku=f"SKUCROSS-{self.suffix}",
            reorder_level=10,
        )

        self.client.force_authenticate(user=self.user_tenant_b)
        res = self.client.patch(
            f"/api/v2/catalog/local-inventory/{cfg_a.config_id}/",
            data={"shelf_location": "Illegal modification"},
            format="json",
        )
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS_DENIED"

    def test_partial_update_inventory_config(self):
        """Pharmacist can update shelf location and reorder level."""
        cfg = LocalInventoryConfig.objects.create(
            config_id=f"cfg-upd-{self.suffix}",
            product=self.product_1,
            tenant=self.tenant_a,
            branch=self.branch_a,
            local_sku=f"SKUUPD-{self.suffix}",
            reorder_level=10,
            shelf_location="Old Location",
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.patch(
            f"/api/v2/catalog/local-inventory/{cfg.config_id}/",
            data={"shelf_location": "Rack-C2 / Bin-05", "reorder_level": 30},
            format="json",
        )
        assert res.status_code == 200
        data = res.json()
        assert data["shelf_location"] == "Rack-C2 / Bin-05"
        assert data["reorder_level"] == 30

    def test_filter_by_sku_and_visibility(self):
        """Filter endpoint supports search_sku and is_visible query parameters."""
        sku = f"SKU-VIS-{self.suffix}"
        LocalInventoryConfig.objects.create(
            config_id=f"cfg-vis-{self.suffix}",
            product=self.product_1,
            tenant=self.tenant_a,
            branch=self.branch_a,
            local_sku=sku,
            is_visible=True,
        )
        LocalInventoryConfig.objects.create(
            config_id=f"cfg-invis-{self.suffix}",
            product=self.product_2,
            tenant=self.tenant_a,
            branch=self.branch_a,
            local_sku=f"SKU-HID-{self.suffix}",
            is_visible=False,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get(f"/api/v2/catalog/local-inventory/?search_sku={sku}")
        assert res.status_code == 200
        assert len(res.json()) == 1
        assert res.json()[0]["local_sku"] == sku

    def test_soft_delete_and_archive(self):
        """Archiving an inventory config sets is_active=False and records audit log."""
        cfg = LocalInventoryConfig.objects.create(
            config_id=f"cfg-del-{self.suffix}",
            product=self.product_1,
            tenant=self.tenant_a,
            branch=self.branch_a,
            local_sku=f"SKUDEL-{self.suffix}",
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.delete(f"/api/v2/catalog/local-inventory/{cfg.config_id}/")
        assert res.status_code == 200
        assert res.json()["status"] == "ARCHIVED"

        cfg.refresh_from_db()
        assert cfg.is_active is False
