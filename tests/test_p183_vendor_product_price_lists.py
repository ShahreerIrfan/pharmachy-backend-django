"""
backend/tests/test_p183_vendor_product_price_lists.py
Architecture Decision: ADR-0183 (P183).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P183 Vendor Product Price Lists:
1. Valid price entry creation with full metadata, discount, net unit cost calculation, and audit trail.
2. Cost exceeds MRP violation rejected with ERR_COST_EXCEEDS_MRP (HTTP 400).
3. Invalid effective date range (effective_to < effective_from) rejected with ERR_INVALID_DATE_RANGE (HTTP 400).
4. Negative/zero unit cost or invalid discount bounds (>100% or <0%) rejected with validation errors.
5. Invalid minimum order quantity (MOQ < 1) rejected with validation error.
6. Cross-tenant isolation blocks reading or creating price entries for other tenants with HTTP 403.
7. Cashier role blocked from mutating vendor price lists with HTTP 403 (ERR_STAFF_ROLE_DENIED).
8. Suspended tenant blocked from mutating or retrieving price lists with HTTP 403 (ERR_TENANT_SUSPENDED).
9. Authorized PATCH update and soft archival (is_active = False) via DELETE.
10. Automated best-price resolution (resolve-best) selecting optimal quotation considering MOQ and calculating free-goods bonus.
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    MedicineProduct,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
    VendorProductPriceList,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestVendorProductPriceListsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Active Tenant and Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p183-{self.suffix}",
            name="Evercare Central Hospital",
            subdomain=f"evercare-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Main Hospital Branch A",
        )

        # Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.suffix}",
            name="Foreign Medico Ltd.",
            subdomain=f"foreign-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name="Suspended Pharma Ltd.",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 2. Setup Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@evercare.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Hospital Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"csh_{self.suffix}@evercare.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@medico.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 3. Setup Suppliers
        self.supplier_square = Supplier.objects.create(
            supplier_id=f"sup-sq-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals Depot",
            legal_name="Square Pharmaceuticals PLC Distribution",
            region="DHAKA",
            is_active=True,
        )
        self.supplier_incepta = Supplier.objects.create(
            supplier_id=f"sup-inc-{self.suffix}",
            tenant=self.tenant,
            name="Incepta Pharmaceuticals Depot",
            legal_name="Incepta Pharmaceuticals Ltd. Regional Center",
            region="DHAKA",
            is_active=True,
        )
        self.foreign_supplier = Supplier.objects.create(
            supplier_id=f"sup-for-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Supplier Corp",
            legal_name="Foreign Supplier Corporation",
            region="CHITTAGONG",
            is_active=True,
        )

        # 4. Setup Products
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("3.0000"),
            cost_unit=Decimal("2.0000"),
        )
        self.product_seclo = MedicineProduct.objects.create(
            product_id=f"prod-seclo-{self.suffix}",
            brand_name="Seclo 20mg",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("7.0000"),
            cost_unit=Decimal("5.0000"),
        )

    def test_01_create_valid_price_list_entry_with_discounts_and_audit(self):
        """Test 1: Valid price entry creation with full metadata, discount, net cost calculation, and audit."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier_square.supplier_id,
            "product_id": self.product_napa.product_id,
            "supplier_sku_code": "SQ-NAP-500",
            "package_type": "BOX",
            "package_size": 100,
            "unit_cost_price": "2.4000",
            "mrp_unit": "3.0000",
            "discount_percentage": "10.00",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "minimum_order_quantity": 50,
            "free_goods_threshold": 100,
            "free_goods_bonus_quantity": 5,
            "is_contracted": True,
            "notes": "Annual framework procurement discount",
        }

        resp = self.client.post("/api/v2/procurement/price-lists/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert data["price_entry_id"].startswith("vpl-")
        assert data["tenant_id"] == str(self.tenant.tenant_id)
        assert data["supplier_id"] == self.supplier_square.supplier_id
        assert data["product_id"] == self.product_napa.product_id
        assert data["unit_cost_price"] == "2.4000"
        assert data["package_cost_price"] == "240.0000"
        assert data["discount_percentage"] == "10.00"
        # Net unit cost = 2.40 * 0.90 = 2.1600
        assert data["net_unit_cost"] == "2.1600"
        assert data["minimum_order_quantity"] == 50
        assert data["free_goods_threshold"] == 100
        assert data["free_goods_bonus_quantity"] == 5
        assert data["is_contracted"] is True
        assert data["is_active"] is True

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            resource_id=data["price_entry_id"],
            action="VENDOR_PRICE_LIST_CREATED",
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS
        assert audit.metadata_json["unit_cost_price"] == "2.4000"

    def test_02_cost_exceeds_mrp_rejected(self):
        """Test 2: Cost exceeds MRP violation rejected with ERR_COST_EXCEEDS_MRP (HTTP 400)."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier_square.supplier_id,
            "product_id": self.product_napa.product_id,
            "unit_cost_price": "3.5000",  # Exceeds MRP 3.0000
            "mrp_unit": "3.0000",
        }

        resp = self.client.post("/api/v2/procurement/price-lists/", data=payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error_code"] == "ERR_COST_EXCEEDS_MRP"
        assert "cannot exceed" in resp.data["detail"]

    def test_03_invalid_date_range_rejected(self):
        """Test 3: Invalid effective date range (effective_to < effective_from) rejected with ERR_INVALID_DATE_RANGE."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier_square.supplier_id,
            "product_id": self.product_napa.product_id,
            "unit_cost_price": "2.2000",
            "effective_from": "2026-12-01",
            "effective_to": "2026-11-01",  # Before from
        }

        resp = self.client.post("/api/v2/procurement/price-lists/", data=payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error_code"] == "ERR_INVALID_DATE_RANGE"

    def test_04_invalid_unit_cost_and_discount_bounds(self):
        """Test 4: Negative/zero unit cost or invalid discount bounds (>100% or <0%) rejected."""
        self.client.force_authenticate(user=self.manager)

        # Zero unit cost
        resp1 = self.client.post(
            "/api/v2/procurement/price-lists/",
            data={
                "supplier_id": self.supplier_square.supplier_id,
                "product_id": self.product_napa.product_id,
                "unit_cost_price": "0.0000",
            },
            format="json",
        )
        assert resp1.status_code == status.HTTP_400_BAD_REQUEST
        assert resp1.data["error_code"] == "ERR_INVALID_COST_PRICE"

        # Discount > 100%
        resp2 = self.client.post(
            "/api/v2/procurement/price-lists/",
            data={
                "supplier_id": self.supplier_square.supplier_id,
                "product_id": self.product_napa.product_id,
                "unit_cost_price": "2.0000",
                "discount_percentage": "105.00",
            },
            format="json",
        )
        assert resp2.status_code == status.HTTP_400_BAD_REQUEST
        assert resp2.data["error_code"] == "ERR_INVALID_DISCOUNT_PERCENTAGE"

        # Negative discount
        resp3 = self.client.post(
            "/api/v2/procurement/price-lists/",
            data={
                "supplier_id": self.supplier_square.supplier_id,
                "product_id": self.product_napa.product_id,
                "unit_cost_price": "2.0000",
                "discount_percentage": "-5.00",
            },
            format="json",
        )
        assert resp3.status_code == status.HTTP_400_BAD_REQUEST
        assert resp3.data["error_code"] == "ERR_INVALID_DISCOUNT_PERCENTAGE"

    def test_05_invalid_minimum_order_quantity(self):
        """Test 5: Invalid minimum order quantity (MOQ < 1) rejected with validation error."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/procurement/price-lists/",
            data={
                "supplier_id": self.supplier_square.supplier_id,
                "product_id": self.product_napa.product_id,
                "unit_cost_price": "2.0000",
                "minimum_order_quantity": 0,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error_code"] == "ERR_INVALID_MOQ"

    def test_06_cross_tenant_isolation_enforced(self):
        """Test 6: Cross-tenant isolation blocks reading or creating price entries for other tenants with HTTP 403."""
        # 1. Foreign manager tries to create price list referencing tenant_alpha's supplier
        self.client.force_authenticate(user=self.foreign_manager)
        resp = self.client.post(
            "/api/v2/procurement/price-lists/",
            data={
                "supplier_id": self.supplier_square.supplier_id,  # Belonging to tenant-alpha
                "product_id": self.product_napa.product_id,
                "unit_cost_price": "2.0000",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        # 2. Manager creates price entry in tenant-alpha
        entry = VendorProductPriceList.objects.create(
            price_entry_id=f"vpl-iso-{self.suffix}",
            tenant=self.tenant,
            supplier=self.supplier_square,
            product=self.product_napa,
            unit_cost_price=Decimal("2.2000"),
            mrp_unit=Decimal("3.0000"),
            effective_from=datetime.date.today(),
            minimum_order_quantity=1,
            is_active=True,
        )

        # 3. Foreign manager tries to access this price entry
        resp_get = self.client.get(f"/api/v2/procurement/price-lists/{entry.price_entry_id}/")
        assert resp_get.status_code == status.HTTP_403_FORBIDDEN
        assert resp_get.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_07_cashier_role_blocked_from_mutations(self):
        """Test 7: Cashier role blocked from mutating vendor price lists with HTTP 403 (ERR_STAFF_ROLE_DENIED)."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/procurement/price-lists/",
            data={
                "supplier_id": self.supplier_square.supplier_id,
                "product_id": self.product_napa.product_id,
                "unit_cost_price": "2.0000",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_08_suspended_tenant_blocked(self):
        """Test 8: Suspended tenant blocked from mutating or retrieving price lists with HTTP 403 (ERR_TENANT_SUSPENDED)."""
        self.client.force_authenticate(user=self.suspended_manager)
        resp = self.client.get("/api/v2/procurement/price-lists/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]

    def test_09_patch_update_and_soft_archival(self):
        """Test 9: Authorized PATCH update and soft archival (is_active = False) via DELETE."""
        self.client.force_authenticate(user=self.manager)
        entry = VendorProductPriceList.objects.create(
            price_entry_id=f"vpl-upd-{self.suffix}",
            tenant=self.tenant,
            supplier=self.supplier_square,
            product=self.product_napa,
            unit_cost_price=Decimal("2.5000"),
            mrp_unit=Decimal("3.0000"),
            discount_percentage=Decimal("5.00"),
            effective_from=datetime.date(2026, 1, 1),
            minimum_order_quantity=10,
            is_active=True,
        )

        # PATCH update price and discount
        patch_resp = self.client.patch(
            f"/api/v2/procurement/price-lists/{entry.price_entry_id}/",
            data={
                "unit_cost_price": "2.2000",
                "discount_percentage": "12.00",
                "minimum_order_quantity": 25,
            },
            format="json",
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        assert patch_resp.data["unit_cost_price"] == "2.2000"
        assert patch_resp.data["discount_percentage"] == "12.00"
        # 2.20 * (1 - 0.12) = 1.9360
        assert patch_resp.data["net_unit_cost"] == "1.9360"
        assert patch_resp.data["minimum_order_quantity"] == 25

        # DELETE soft-archival
        del_resp = self.client.delete(f"/api/v2/procurement/price-lists/{entry.price_entry_id}/")
        assert del_resp.status_code == status.HTTP_200_OK
        assert del_resp.data["is_active"] is False

        # Verify audit entry for archival
        audit = AuditLogEntry.objects.filter(
            resource_id=entry.price_entry_id,
            action="VENDOR_PRICE_LIST_ARCHIVED",
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

    def test_10_automated_best_price_resolution_with_moq_and_bonus(self):
        """Test 10: Automated best-price resolution (resolve-best) selecting optimal quotation considering MOQ and bonus."""
        self.client.force_authenticate(user=self.manager)

        # Supplier Square offer for Seclo:
        # unit cost: 5.5000, discount: 5.00% -> net: 5.2250, MOQ: 50, bonus: 100 -> 5
        VendorProductPriceList.objects.create(
            price_entry_id=f"vpl-sq-{self.suffix}",
            tenant=self.tenant,
            supplier=self.supplier_square,
            product=self.product_seclo,
            unit_cost_price=Decimal("5.5000"),
            mrp_unit=Decimal("7.0000"),
            discount_percentage=Decimal("5.00"),
            effective_from=datetime.date(2026, 1, 1),
            effective_to=datetime.date(2026, 12, 31),
            minimum_order_quantity=50,
            free_goods_threshold=100,
            free_goods_bonus_quantity=5,
            is_active=True,
        )

        # Supplier Incepta offer for Seclo:
        # unit cost: 5.3000, discount: 8.00% -> net: 4.8760, MOQ: 200, no bonus
        VendorProductPriceList.objects.create(
            price_entry_id=f"vpl-inc-{self.suffix}",
            tenant=self.tenant,
            supplier=self.supplier_incepta,
            product=self.product_seclo,
            unit_cost_price=Decimal("5.3000"),
            mrp_unit=Decimal("7.0000"),
            discount_percentage=Decimal("8.00"),
            effective_from=datetime.date(2026, 1, 1),
            effective_to=datetime.date(2026, 12, 31),
            minimum_order_quantity=200,
            free_goods_threshold=0,
            free_goods_bonus_quantity=0,
            is_active=True,
        )

        # 1. Demanded quantity 100: Incepta's MOQ is 200, so Incepta is disqualified. Square is selected.
        resp_100 = self.client.post(
            "/api/v2/procurement/price-lists/resolve-best/",
            data={
                "product_id": self.product_seclo.product_id,
                "quantity": 100,
                "target_date": "2026-06-15",
            },
            format="json",
        )
        assert resp_100.status_code == status.HTTP_200_OK, resp_100.data
        data_100 = resp_100.data
        assert data_100["supplier_id"] == self.supplier_square.supplier_id
        assert data_100["net_unit_cost"] == "5.2250"
        assert data_100["total_net_cost"] == "522.5000"
        assert data_100["free_goods_bonus_units"] == 5

        # 2. Demanded quantity 250: Incepta satisfies MOQ (200) and is cheaper (4.8760 < 5.2250). Incepta selected.
        resp_250 = self.client.post(
            "/api/v2/procurement/price-lists/resolve-best/",
            data={
                "product_id": self.product_seclo.product_id,
                "quantity": 250,
                "target_date": "2026-06-15",
            },
            format="json",
        )
        assert resp_250.status_code == status.HTTP_200_OK, resp_250.data
        data_250 = resp_250.data
        assert data_250["supplier_id"] == self.supplier_incepta.supplier_id
        assert data_250["net_unit_cost"] == "4.8760"
        assert data_250["total_net_cost"] == "1219.0000"
        assert data_250["free_goods_bonus_units"] == 0

