"""
backend/tests/test_p185_purchase_order_lifecycle.py
Architecture Decision: ADR-0185 (P185).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P185 Purchase Order Lifecycle:
1. Valid purchase order creation with line items, trade discounts, order tax, and audit trail.
2. DGDA MRP ceiling guard: Quoted cost exceeding MRP rejected with ERR_COST_EXCEEDS_MRP (HTTP 400).
3. Approval and dispatch lifecycle: DRAFT -> APPROVED -> ORDERED with timestamps.
4. Progressive goods receipt transitions: ORDERED -> PARTIALLY_RECEIVED -> CLOSED.
5. Guarded cancellation allowed before goods receipt with mandatory cancellation_reason.
6. Guarded cancellation barred after goods received (ERR_CANNOT_CANCEL_RECEIVED_PO).
7. Immutability: Mutating approved or ordered POs rejected with ERR_PO_LOCKED.
8. Cross-tenant isolation strictly enforced with HTTP 403.
9. RBAC: Cashier role blocked, and staff pharmacist barred from approving.
10. Suspended tenant blocked with HTTP 403.
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
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPurchaseOrderLifecycleIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p185-{self.suffix}",
            name="Evercare Central Hospital",
            subdomain=f"evercare-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Main Hospital Branch A",
        )

        # Foreign Tenant & Branch
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.suffix}",
            name="Foreign Medico Ltd.",
            subdomain=f"foreign-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-for-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign North Branch",
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
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@evercare.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"csh_{self.suffix}@evercare.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@medico.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
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
        self.foreign_supplier = Supplier.objects.create(
            supplier_id=f"sup-for-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Supplier Corp",
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

    def test_01_create_valid_purchase_order_with_items_and_audit(self):
        """Test 1: Valid purchase order creation with line items, trade discounts, order tax, and audit."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "payment_terms_days": 30,
            "discount_amount": "20.0000",
            "tax_amount": "10.0000",
            "expected_delivery_date": "2026-11-25",
            "notes": "Bulk depot replenishment order",
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "package_type": "BOX",
                    "package_size": 100,
                    "ordered_quantity": 100,
                    "unit_cost_price": "2.5000",
                    "discount_percentage": "10.00",  # net = 2.2500 -> total = 225.0000
                },
                {
                    "product_id": self.product_seclo.product_id,
                    "package_type": "STRIP",
                    "package_size": 10,
                    "ordered_quantity": 50,
                    "unit_cost_price": "5.0000",
                    "discount_percentage": "0.00",  # net = 5.0000 -> total = 250.0000
                },
            ],
        }

        # Subtotal = 225.00 + 250.00 = 475.0000
        # Total = 475.00 - 20.00 + 10.00 = 465.0000
        resp = self.client.post("/api/v2/procurement/orders/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert data["po_id"].startswith("po-")
        assert data["po_number"].startswith("PO-")
        assert data["tenant_id"] == str(self.tenant.tenant_id)
        assert data["supplier_id"] == self.supplier_square.supplier_id
        assert data["status"] == "DRAFT"
        assert data["subtotal_amount"] == "475.0000"
        assert data["discount_amount"] == "20.0000"
        assert data["tax_amount"] == "10.0000"
        assert data["total_amount"] == "465.0000"
        assert len(data["items"]) == 2

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            resource_id=data["po_id"],
            action="PURCHASE_ORDER_CREATED",
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS
        assert audit.metadata_json["total_amount"] == "465.0000"

    def test_02_cost_exceeds_mrp_rejected(self):
        """Test 2: DGDA MRP ceiling guard: Quoted cost exceeding MRP rejected with ERR_COST_EXCEEDS_MRP."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch_a.branch_id,
            "supplier_id": self.supplier_square.supplier_id,
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "ordered_quantity": 50,
                    "unit_cost_price": "3.5000",  # Product MRP is 3.0000 -> Exceeds MRP!
                }
            ],
        }

        resp = self.client.post("/api/v2/procurement/orders/", data=payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error_code"] == "ERR_COST_EXCEEDS_MRP"

    def test_03_approval_and_dispatch_lifecycle(self):
        """Test 3: Approval and dispatch lifecycle: DRAFT -> APPROVED -> ORDERED with timestamps."""
        self.client.force_authenticate(user=self.manager)
        po = PurchaseOrder.objects.create(
            po_id=f"po-app-{self.suffix}",
            po_number=f"PO-APP-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.DRAFT,
            total_amount=Decimal("500.0000"),
        )
        PurchaseOrderItem.objects.create(
            item_id=f"poi-app-{self.suffix}",
            purchase_order=po,
            product=self.product_napa,
            ordered_quantity=100,
            unit_cost_price=Decimal("2.0000"),
            net_unit_cost=Decimal("2.0000"),
            line_total=Decimal("200.0000"),
        )

        # 1. Approve
        resp_app = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/approve/",
            data={"approval_notes": "Commercially vetted"},
            format="json",
        )
        assert resp_app.status_code == status.HTTP_200_OK
        assert resp_app.data["status"] == "APPROVED"
        assert resp_app.data["approved_by_id"] == str(self.manager.user_id)
        assert resp_app.data["approval_notes"] == "Commercially vetted"

        # 2. Dispatch
        resp_ord = self.client.post(f"/api/v2/procurement/orders/{po.po_id}/order/", data={}, format="json")
        assert resp_ord.status_code == status.HTTP_200_OK
        assert resp_ord.data["status"] == "ORDERED"
        assert resp_ord.data["ordered_at"] is not None

        # Verify audit entries
        assert AuditLogEntry.objects.filter(resource_id=po.po_id, action="PURCHASE_ORDER_APPROVED").exists()
        assert AuditLogEntry.objects.filter(resource_id=po.po_id, action="PURCHASE_ORDER_DISPATCHED").exists()

    def test_04_progressive_goods_receipt_transitions_to_partially_received_and_closed(self):
        """Test 4: Progressive goods receipt transitions: ORDERED -> PARTIALLY_RECEIVED -> CLOSED."""
        self.client.force_authenticate(user=self.manager)
        po = PurchaseOrder.objects.create(
            po_id=f"po-rec-{self.suffix}",
            po_number=f"PO-REC-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.ORDERED,
            ordered_at=datetime.datetime.now(datetime.timezone.utc),
        )
        item1 = PurchaseOrderItem.objects.create(
            item_id=f"poi-r1-{self.suffix}",
            purchase_order=po,
            product=self.product_napa,
            ordered_quantity=100,
            received_quantity=0,
            unit_cost_price=Decimal("2.0000"),
            net_unit_cost=Decimal("2.0000"),
            line_total=Decimal("200.0000"),
        )
        item2 = PurchaseOrderItem.objects.create(
            item_id=f"poi-r2-{self.suffix}",
            purchase_order=po,
            product=self.product_seclo,
            ordered_quantity=50,
            received_quantity=0,
            unit_cost_price=Decimal("5.0000"),
            net_unit_cost=Decimal("5.0000"),
            line_total=Decimal("250.0000"),
        )

        # 1. Partial receipt: Receive 50 out of 100 for item1, 0 for item2
        resp_part = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/receive/",
            data={"receipts": [{"item_id": item1.item_id, "quantity": 50}]},
            format="json",
        )
        assert resp_part.status_code == status.HTTP_200_OK
        assert resp_part.data["status"] == "PARTIALLY_RECEIVED"
        assert resp_part.data["items"][0]["received_quantity"] == 50

        # 2. Complete receipt: Receive remaining 50 for item1 and 50 for item2
        resp_full = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/receive/",
            data={
                "receipts": [
                    {"item_id": item1.item_id, "quantity": 50},
                    {"item_id": item2.item_id, "quantity": 50},
                ]
            },
            format="json",
        )
        assert resp_full.status_code == status.HTTP_200_OK
        assert resp_full.data["status"] == "CLOSED"
        assert resp_full.data["closed_at"] is not None

    def test_05_guarded_cancellation_allowed_before_receipt(self):
        """Test 5: Guarded cancellation allowed before goods receipt with mandatory cancellation_reason."""
        self.client.force_authenticate(user=self.manager)
        po = PurchaseOrder.objects.create(
            po_id=f"po-can-{self.suffix}",
            po_number=f"PO-CAN-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.ORDERED,
        )
        PurchaseOrderItem.objects.create(
            item_id=f"poi-can-{self.suffix}",
            purchase_order=po,
            product=self.product_napa,
            ordered_quantity=50,
            received_quantity=0,
            unit_cost_price=Decimal("2.0000"),
        )

        # Missing reason rejected
        fail_resp = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel/",
            data={"cancellation_reason": "   "},
            format="json",
        )
        assert fail_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert fail_resp.data["error_code"] == "ERR_MISSING_CANCELLATION_REASON"

        # Cancel with reason succeeds
        ok_resp = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel/",
            data={"cancellation_reason": "Distributor announced manufacturing delay"},
            format="json",
        )
        assert ok_resp.status_code == status.HTTP_200_OK
        assert ok_resp.data["status"] == "CANCELLED"
        assert ok_resp.data["cancellation_reason"] == "Distributor announced manufacturing delay"

    def test_06_guarded_cancellation_barred_after_goods_received(self):
        """Test 6: Guarded cancellation barred after goods received (ERR_CANNOT_CANCEL_RECEIVED_PO)."""
        self.client.force_authenticate(user=self.manager)
        po = PurchaseOrder.objects.create(
            po_id=f"po-bar-{self.suffix}",
            po_number=f"PO-BAR-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.PARTIALLY_RECEIVED,
        )
        PurchaseOrderItem.objects.create(
            item_id=f"poi-bar-{self.suffix}",
            purchase_order=po,
            product=self.product_napa,
            ordered_quantity=50,
            received_quantity=10,  # 10 units already received
            unit_cost_price=Decimal("2.0000"),
        )

        resp = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel/",
            data={"cancellation_reason": "Attempting cancellation on received stock"},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error_code"] == "ERR_CANNOT_CANCEL_RECEIVED_PO"

    def test_07_locked_state_prevents_item_mutation_after_approval(self):
        """Test 7: Immutability: Mutating approved or ordered POs rejected with ERR_PO_LOCKED."""
        self.client.force_authenticate(user=self.manager)
        po = PurchaseOrder.objects.create(
            po_id=f"po-lck-{self.suffix}",
            po_number=f"PO-LCK-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.APPROVED,
        )

        patch_resp = self.client.patch(
            f"/api/v2/procurement/orders/{po.po_id}/",
            data={"notes": "Modifying approved PO"},
            format="json",
        )
        assert patch_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert patch_resp.data["error_code"] == "ERR_PO_LOCKED"

    def test_08_cross_tenant_isolation_enforced(self):
        """Test 8: Cross-tenant isolation strictly enforced with HTTP 403."""
        self.client.force_authenticate(user=self.foreign_manager)

        # 1. Foreign manager cannot create PO for tenant-alpha's supplier
        resp_post = self.client.post(
            "/api/v2/procurement/orders/",
            data={
                "tenant_id": self.tenant.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "supplier_id": self.supplier_square.supplier_id,
                "items": [{"product_id": self.product_napa.product_id, "ordered_quantity": 10}],
            },
            format="json",
        )
        assert resp_post.status_code == status.HTTP_403_FORBIDDEN
        assert resp_post.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        # 2. Foreign manager cannot access tenant-alpha's PO
        po = PurchaseOrder.objects.create(
            po_id=f"po-iso-{self.suffix}",
            po_number=f"PO-ISO-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.DRAFT,
        )
        resp_get = self.client.get(f"/api/v2/procurement/orders/{po.po_id}/")
        assert resp_get.status_code == status.HTTP_403_FORBIDDEN
        assert resp_get.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_09_cashier_role_blocked_and_pharmacist_approval_blocked(self):
        """Test 9: RBAC: Cashier role blocked, and staff pharmacist barred from approving."""
        # Cashier blocked from creating
        self.client.force_authenticate(user=self.cashier)
        resp_csh = self.client.post(
            "/api/v2/procurement/orders/",
            data={"branch_id": self.branch_a.branch_id, "supplier_id": self.supplier_square.supplier_id, "items": []},
            format="json",
        )
        assert resp_csh.status_code == status.HTTP_403_FORBIDDEN
        assert resp_csh.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # Pharmacist creates DRAFT PO, but cannot approve
        self.client.force_authenticate(user=self.pharmacist)
        po = PurchaseOrder.objects.create(
            po_id=f"po-rbc-{self.suffix}",
            po_number=f"PO-RBC-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            supplier=self.supplier_square,
            status=PurchaseOrderStatus.DRAFT,
        )
        resp_app = self.client.post(f"/api/v2/procurement/orders/{po.po_id}/approve/", data={}, format="json")
        assert resp_app.status_code == status.HTTP_403_FORBIDDEN
        assert resp_app.data["error_code"] == "ERR_APPROVAL_ROLE_REQUIRED"

    def test_10_suspended_tenant_blocked(self):
        """Test 10: Suspended tenant blocked with HTTP 403."""
        self.client.force_authenticate(user=self.suspended_manager)
        resp = self.client.get("/api/v2/procurement/orders/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]

