"""
backend/tests/test_p186_purchase_order_line_modeling.py
Architecture Decision: ADR-0186 (P186).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P186 Purchase Order Line Modeling:
1. Valid purchase order creation with full line modeling (pack units, conversions, line taxes, discounts, delivery dates).
2. Dynamic totals recalculation when adding a line item to a DRAFT purchase order via POST /orders/{pk}/items/.
3. Updating an existing line item via PATCH /orders/{pk}/items/{item_id}/ with automatic totals recalculation.
4. Deleting a line item via DELETE /orders/{pk}/items/{item_id}/ with automatic totals recalculation.
5. Deletion of the last remaining line item is strictly barred (ERR_CANNOT_DELETE_LAST_ITEM).
6. Locked PO guard: Add, update, or delete line items on non-DRAFT (APPROVED) PO rejected with ERR_PO_LOCKED (HTTP 400).
7. Regulatory DGDA MRP ceiling guard: Quoted unit_cost_price exceeding mrp_unit rejected with ERR_COST_EXCEEDS_MRP (HTTP 400).
8. Parameter boundary validations: invalid quantity, negative cost, discount > 100%, tax > 100% rejected with HTTP 400.
9. Cross-tenant isolation: Foreign tenant barred from viewing or mutating lines on another tenant's purchase order (HTTP 403).
10. RBAC and tenant lifecycle guards: Cashier role blocked (HTTP 403 ERR_STAFF_ROLE_DENIED) and suspended tenant blocked.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPurchaseOrderLineModelingIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Active Tenants and Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p186-{self.suffix}",
            name="Apollo Square Hospital",
            subdomain=f"apollo-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Apollo Central Dispensary",
        )

        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.suffix}",
            name="Foreign Apex Pharma",
            subdomain=f"apex-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-for-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Apex Foreign Branch",
        )

        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name="Suspended Care Ltd.",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 2. Setup Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@apollo.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"csh_{self.suffix}@apollo.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@apex.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@care.com",
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
            name="Square Pharmaceuticals PLC",
            legal_name="Square Pharmaceuticals PLC Distribution",
            region="DHAKA",
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
            cost_unit=Decimal("2.4000"),
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
        self.product_azithro = MedicineProduct.objects.create(
            product_id=f"prod-azithro-{self.suffix}",
            brand_name="Zithrox 500mg",
            generic_name="Azithromycin",
            dosage_form="Tablet",
            strength="500mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("35.0000"),
            cost_unit=Decimal("28.0000"),
        )

    def test_01_create_purchase_order_with_complete_line_modeling(self):
        """Test 1: Valid PO creation with complete line modeling (pack units, conversions, line taxes, delivery dates)."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "payment_terms_days": 30,
            "notes": "P186 Line items contract validation",
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "pack_unit": "BOX",
                    "package_size": 100,
                    "ordered_packages": 10,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "discount_percentage": "5.00",
                    "tax_percentage": "7.50",
                    "expected_delivery_date": "2026-11-20",
                    "free_goods_bonus_quantity": 20,
                    "notes": "Emergency fever season stock",
                }
            ],
        }

        resp = self.client.post("/api/v2/procurement/orders/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data
        assert data["status"] == "DRAFT"
        assert len(data["items"]) == 1

        item = data["items"][0]
        assert item["pack_unit"] == "BOX"
        assert item["package_size"] == 100
        assert item["ordered_packages"] == 10
        assert item["ordered_quantity"] == 1000
        assert item["free_goods_bonus_quantity"] == 20
        assert Decimal(item["package_cost_price"]) == Decimal("200.0000")
        assert Decimal(item["unit_cost_price"]) == Decimal("2.0000")
        assert Decimal(item["mrp_unit"]) == Decimal("3.0000")
        assert Decimal(item["discount_percentage"]) == Decimal("5.00")
        assert Decimal(item["tax_percentage"]) == Decimal("7.50")

        # Calculations:
        # net_unit_cost = 2.00 * (1 - 0.05) = 1.9000
        assert Decimal(item["net_unit_cost"]) == Decimal("1.9000")
        # line_subtotal = 1.9000 * 1000 = 1900.0000
        assert Decimal(item["line_subtotal"]) == Decimal("1900.0000")
        # tax_amount = 1900.0000 * 0.075 = 142.5000
        assert Decimal(item["tax_amount"]) == Decimal("142.5000")
        # line_total = 1900.0000 + 142.5000 = 2042.5000
        assert Decimal(item["line_total"]) == Decimal("2042.5000")
        assert item["expected_delivery_date"] == "2026-11-20"

        # Parent PO aggregation:
        assert Decimal(data["subtotal_amount"]) == Decimal("1900.0000")
        assert Decimal(data["tax_amount"]) == Decimal("142.5000")
        assert Decimal(data["total_amount"]) == Decimal("2042.5000")

    def test_02_add_line_item_to_draft_po_via_api(self):
        """Test 2: Dynamic totals recalculation when adding line item via POST /orders/{pk}/items/."""
        self.client.force_authenticate(user=self.manager)
        # Create PO with 1 item
        init_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "pack_unit": "BOX",
                    "package_size": 10,
                    "ordered_packages": 10,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "discount_percentage": "0.00",
                    "tax_percentage": "0.00",
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/orders/", init_payload, format="json")
        assert create_resp.status_code == status.HTTP_201_CREATED
        po_id = create_resp.data["po_id"]
        assert Decimal(create_resp.data["subtotal_amount"]) == Decimal("200.0000")

        # Add second item via POST /items/
        item2_payload = {
            "product_id": self.product_seclo.product_id,
            "pack_unit": "STRIP",
            "package_size": 10,
            "ordered_packages": 5,
            "unit_cost_price": "5.0000",
            "mrp_unit": "7.0000",
            "discount_percentage": "10.00",  # net = 4.50, subtotal = 50 * 4.50 = 225.0000
            "tax_percentage": "5.00",        # tax = 225 * 0.05 = 11.2500
            "expected_delivery_date": "2026-11-28",
        }
        add_resp = self.client.post(f"/api/v2/procurement/orders/{po_id}/items/", item2_payload, format="json")
        assert add_resp.status_code == status.HTTP_201_CREATED, add_resp.data
        added_item = add_resp.data
        assert added_item["product_id"] == self.product_seclo.product_id
        assert Decimal(added_item["line_subtotal"]) == Decimal("225.0000")
        assert Decimal(added_item["tax_amount"]) == Decimal("11.2500")
        assert Decimal(added_item["line_total"]) == Decimal("236.2500")

        # Verify parent PO totals updated
        po_resp = self.client.get(f"/api/v2/procurement/orders/{po_id}/")
        assert po_resp.status_code == status.HTTP_200_OK
        po_data = po_resp.data
        assert len(po_data["items"]) == 2
        # Subtotal: 200 + 225 = 425.0000
        assert Decimal(po_data["subtotal_amount"]) == Decimal("425.0000")
        # Tax: 0 + 11.2500 = 11.2500
        assert Decimal(po_data["tax_amount"]) == Decimal("11.2500")
        # Total: 425 + 11.2500 = 436.2500
        assert Decimal(po_data["total_amount"]) == Decimal("436.2500")

    def test_03_update_line_item_via_api(self):
        """Test 3: Updating existing line item via PATCH /orders/{pk}/items/{item_id}/ with dynamic recalculation."""
        self.client.force_authenticate(user=self.manager)
        init_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "pack_unit": "BOX",
                    "package_size": 10,
                    "ordered_packages": 5,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "discount_percentage": "0.00",
                    "tax_percentage": "0.00",
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/orders/", init_payload, format="json")
        po_id = create_resp.data["po_id"]
        item_id = create_resp.data["items"][0]["item_id"]
        assert Decimal(create_resp.data["subtotal_amount"]) == Decimal("100.0000")

        # Update line item: increase ordered_packages to 20 and apply 5% discount
        patch_payload = {
            "ordered_packages": 20,
            "discount_percentage": "5.00",
            "notes": "Increased order due to doctor forecast",
        }
        patch_resp = self.client.patch(f"/api/v2/procurement/orders/{po_id}/items/{item_id}/", patch_payload, format="json")
        assert patch_resp.status_code == status.HTTP_200_OK, patch_resp.data
        updated_item = patch_resp.data
        assert updated_item["ordered_packages"] == 20
        assert updated_item["ordered_quantity"] == 200
        # net_unit_cost = 2.00 * (1 - 0.05) = 1.9000
        assert Decimal(updated_item["net_unit_cost"]) == Decimal("1.9000")
        # line_subtotal = 1.9000 * 200 = 380.0000
        assert Decimal(updated_item["line_subtotal"]) == Decimal("380.0000")

        # Verify parent PO totals
        po_resp = self.client.get(f"/api/v2/procurement/orders/{po_id}/")
        assert Decimal(po_resp.data["subtotal_amount"]) == Decimal("380.0000")
        assert Decimal(po_resp.data["total_amount"]) == Decimal("380.0000")

    def test_04_delete_line_item_via_api(self):
        """Test 4: Deleting line item via DELETE /orders/{pk}/items/{item_id}/ with dynamic recalculation."""
        self.client.force_authenticate(user=self.manager)
        init_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "pack_unit": "BOX",
                    "package_size": 10,
                    "ordered_packages": 5,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                },
                {
                    "product_id": self.product_seclo.product_id,
                    "pack_unit": "BOX",
                    "package_size": 10,
                    "ordered_packages": 2,
                    "unit_cost_price": "5.0000",
                    "mrp_unit": "7.0000",
                },
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/orders/", init_payload, format="json")
        po_id = create_resp.data["po_id"]
        # item 1: 50 * 2 = 100, item 2: 20 * 5 = 100 -> Total = 200
        assert Decimal(create_resp.data["subtotal_amount"]) == Decimal("200.0000")
        item2_id = create_resp.data["items"][1]["item_id"]

        del_resp = self.client.delete(f"/api/v2/procurement/orders/{po_id}/items/{item2_id}/")
        assert del_resp.status_code == status.HTTP_200_OK, del_resp.data
        assert del_resp.data.get("deleted") is True

        # Verify parent PO totals now reflect only item 1
        po_resp = self.client.get(f"/api/v2/procurement/orders/{po_id}/")
        assert len(po_resp.data["items"]) == 1
        assert Decimal(po_resp.data["subtotal_amount"]) == Decimal("100.0000")
        assert Decimal(po_resp.data["total_amount"]) == Decimal("100.0000")

    def test_05_prevent_delete_last_line_item(self):
        """Test 5: Bar deleting the last remaining line item from a purchase order (ERR_CANNOT_DELETE_LAST_ITEM)."""
        self.client.force_authenticate(user=self.manager)
        init_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "pack_unit": "BOX",
                    "package_size": 10,
                    "ordered_packages": 5,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/orders/", init_payload, format="json")
        po_id = create_resp.data["po_id"]
        item_id = create_resp.data["items"][0]["item_id"]

        del_resp = self.client.delete(f"/api/v2/procurement/orders/{po_id}/items/{item_id}/")
        assert del_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert del_resp.data.get("error_code") == "ERR_CANNOT_DELETE_LAST_ITEM"

    def test_06_locked_po_guard_blocks_mutations(self):
        """Test 6: Locked PO guard: Mutating lines on non-DRAFT (APPROVED) PO rejected with ERR_PO_LOCKED."""
        self.client.force_authenticate(user=self.manager)
        init_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "pack_unit": "BOX",
                    "package_size": 10,
                    "ordered_packages": 5,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/orders/", init_payload, format="json")
        po_id = create_resp.data["po_id"]
        item_id = create_resp.data["items"][0]["item_id"]

        # Approve the PO
        appr_resp = self.client.post(f"/api/v2/procurement/orders/{po_id}/approve/", {"notes": "Approved for dispatch"}, format="json")
        assert appr_resp.status_code == status.HTTP_200_OK
        assert appr_resp.data["status"] == "APPROVED"

        # 1. Attempt to add line on APPROVED PO
        add_resp = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {
                "product_id": self.product_seclo.product_id,
                "ordered_quantity": 10,
                "unit_cost_price": "5.0000",
            },
            format="json",
        )
        assert add_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert add_resp.data.get("error_code") == "ERR_PO_LOCKED"

        # 2. Attempt to update line on APPROVED PO
        patch_resp = self.client.patch(
            f"/api/v2/procurement/orders/{po_id}/items/{item_id}/",
            {"ordered_quantity": 200},
            format="json",
        )
        assert patch_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert patch_resp.data.get("error_code") == "ERR_PO_LOCKED"

        # 3. Attempt to delete line on APPROVED PO
        del_resp = self.client.delete(f"/api/v2/procurement/orders/{po_id}/items/{item_id}/")
        assert del_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert del_resp.data.get("error_code") == "ERR_PO_LOCKED"

    def test_07_dgda_price_ceiling_guard(self):
        """Test 7: Quoted line unit_cost_price exceeding mrp_unit rejected with ERR_COST_EXCEEDS_MRP."""
        self.client.force_authenticate(user=self.manager)
        # 1. In create PO
        invalid_create_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "unit_cost_price": "4.0000",  # Exceeds MRP 3.00
                    "mrp_unit": "3.0000",
                    "ordered_quantity": 100,
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/orders/", invalid_create_payload, format="json")
        assert create_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert create_resp.data.get("error_code") == "ERR_COST_EXCEEDS_MRP"

        # 2. In add line item
        valid_po_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "ordered_quantity": 50,
                }
            ],
        }
        po_resp = self.client.post("/api/v2/procurement/orders/", valid_po_payload, format="json")
        po_id = po_resp.data["po_id"]

        invalid_add_payload = {
            "product_id": self.product_seclo.product_id,
            "unit_cost_price": "8.5000",  # Exceeds MRP 7.00
            "mrp_unit": "7.0000",
            "ordered_quantity": 50,
        }
        add_resp = self.client.post(f"/api/v2/procurement/orders/{po_id}/items/", invalid_add_payload, format="json")
        assert add_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert add_resp.data.get("error_code") == "ERR_COST_EXCEEDS_MRP"

    def test_08_invalid_line_item_parameters(self):
        """Test 8: Invalid quantity, cost, discount > 100%, tax > 100% rejected with HTTP 400."""
        self.client.force_authenticate(user=self.manager)
        valid_po_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "ordered_quantity": 50,
                }
            ],
        }
        po_resp = self.client.post("/api/v2/procurement/orders/", valid_po_payload, format="json")
        po_id = po_resp.data["po_id"]

        # Negative quantity
        r1 = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {"product_id": self.product_seclo.product_id, "ordered_quantity": -5, "unit_cost_price": "5.0000"},
            format="json",
        )
        assert r1.status_code == status.HTTP_400_BAD_REQUEST
        assert r1.data.get("error_code") == "ERR_INVALID_QUANTITY"

        # Negative cost price
        r2 = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {"product_id": self.product_seclo.product_id, "ordered_quantity": 10, "unit_cost_price": "-2.0000"},
            format="json",
        )
        assert r2.status_code == status.HTTP_400_BAD_REQUEST
        assert r2.data.get("error_code") == "ERR_INVALID_COST_PRICE"

        # Discount > 100%
        r3 = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {"product_id": self.product_seclo.product_id, "ordered_quantity": 10, "unit_cost_price": "5.0000", "discount_percentage": "120.00"},
            format="json",
        )
        assert r3.status_code == status.HTTP_400_BAD_REQUEST
        assert r3.data.get("error_code") in ["ERR_INVALID_DISCOUNT", "ERR_INVALID_DISCOUNT_PERCENTAGE"]

        # Tax > 100%
        r4 = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {"product_id": self.product_seclo.product_id, "ordered_quantity": 10, "unit_cost_price": "5.0000", "tax_percentage": "110.00"},
            format="json",
        )
        assert r4.status_code == status.HTTP_400_BAD_REQUEST
        assert r4.data.get("error_code") in ["ERR_INVALID_TAX", "ERR_INVALID_TAX_PERCENTAGE"]

    def test_09_cross_tenant_isolation(self):
        """Test 9: Foreign tenant strictly barred from viewing or mutating lines on another tenant's PO."""
        self.client.force_authenticate(user=self.manager)
        po_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "ordered_quantity": 50,
                }
            ],
        }
        po_resp = self.client.post("/api/v2/procurement/orders/", po_payload, format="json")
        po_id = po_resp.data["po_id"]
        item_id = po_resp.data["items"][0]["item_id"]

        # Switch to foreign manager
        self.client.force_authenticate(user=self.foreign_manager)

        # 1. Foreign tenant listing items
        list_resp = self.client.get(f"/api/v2/procurement/orders/{po_id}/items/")
        assert list_resp.status_code == status.HTTP_403_FORBIDDEN
        assert list_resp.data.get("error_code") == "ERR_CROSS_TENANT_ACCESS"

        # 2. Foreign tenant adding line
        add_resp = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {"product_id": self.product_seclo.product_id, "ordered_quantity": 10, "unit_cost_price": "5.0000"},
            format="json",
        )
        assert add_resp.status_code == status.HTTP_403_FORBIDDEN
        assert add_resp.data.get("error_code") == "ERR_CROSS_TENANT_ACCESS"

        # 3. Foreign tenant updating line
        patch_resp = self.client.patch(
            f"/api/v2/procurement/orders/{po_id}/items/{item_id}/",
            {"ordered_quantity": 200},
            format="json",
        )
        assert patch_resp.status_code == status.HTTP_403_FORBIDDEN
        assert patch_resp.data.get("error_code") == "ERR_CROSS_TENANT_ACCESS"

        # 4. Foreign tenant deleting line
        del_resp = self.client.delete(f"/api/v2/procurement/orders/{po_id}/items/{item_id}/")
        assert del_resp.status_code == status.HTTP_403_FORBIDDEN
        assert del_resp.data.get("error_code") == "ERR_CROSS_TENANT_ACCESS"

    def test_10_role_authorization_and_suspended_tenant(self):
        """Test 10: Cashier role blocked (ERR_STAFF_ROLE_DENIED) and suspended tenant blocked."""
        self.client.force_authenticate(user=self.manager)
        po_payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "unit_cost_price": "2.0000",
                    "mrp_unit": "3.0000",
                    "ordered_quantity": 50,
                }
            ],
        }
        po_resp = self.client.post("/api/v2/procurement/orders/", po_payload, format="json")
        po_id = po_resp.data["po_id"]
        item_id = po_resp.data["items"][0]["item_id"]

        # 1. Cashier role blocked
        self.client.force_authenticate(user=self.cashier)
        csh_add = self.client.post(
            f"/api/v2/procurement/orders/{po_id}/items/",
            {"product_id": self.product_seclo.product_id, "ordered_quantity": 10, "unit_cost_price": "5.0000"},
            format="json",
        )
        assert csh_add.status_code == status.HTTP_403_FORBIDDEN
        assert csh_add.data.get("error_code") == "ERR_STAFF_ROLE_DENIED"

        csh_del = self.client.delete(f"/api/v2/procurement/orders/{po_id}/items/{item_id}/")
        assert csh_del.status_code == status.HTTP_403_FORBIDDEN
        assert csh_del.data.get("error_code") == "ERR_STAFF_ROLE_DENIED"

        # 2. Suspended tenant blocked
        self.client.force_authenticate(user=self.suspended_manager)
        susp_resp = self.client.get(f"/api/v2/procurement/orders/{po_id}/items/")
        assert susp_resp.status_code == status.HTTP_403_FORBIDDEN
        assert susp_resp.data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]
