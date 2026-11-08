"""
backend/tests/test_p194_auto_reorder_planner.py
Architecture Decision: ADR-0194 (P194).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P194 Automatic Reorder Planner:
1. Generate draft reorder proposal -> status is DRAFT_PROPOSAL, transparency metrics calculated.
2. Never auto-order silently guard -> converting unapproved draft to PO raises HTTP 400 ERR_PLAN_NOT_APPROVED.
3. Managerial approval with adjustments -> updates approved_order_units, status becomes APPROVED.
4. Convert approved plan to Purchase Orders -> creates real PurchaseOrder and items, status becomes CONVERTED_TO_PO.
5. FEFO exposure deduction -> near-expiry stock is deducted from effective stock, correctly calculated.
6. Dismiss proposal with reason -> status becomes DISMISSED.
7. Cannot dismiss already converted proposal -> raises HTTP 400 ERR_CANNOT_DISMISS_CONVERTED.
8. Cannot approve invalid status -> raises HTTP 400 ERR_INVALID_STATUS.
9. List and filter proposals by status and branch.
10. RBAC and cross-tenant isolation -> Cashier blocked (403), foreign tenant blocked (404).
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderStatus,
    ReorderPlanStatus,
    ReorderProposal,
    ReorderProposalItem,
    ReorderUrgency,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestAutoReorderPlannerIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p194-{self.suffix}",
            name="Square Health Care",
            subdomain=f"square-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-sq-{self.suffix}",
            tenant=self.tenant,
            name="Main Dispensary",
        )

        # 2. Foreign Tenant & Branch
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-foreign-{self.suffix}",
            name="Popular Pharma Ltd",
            subdomain=f"popular-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-pop-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Popular Central",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Front Desk Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-sq-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals Ltd",
            contact_person="M. Rahman",
            phone="+8801700000000",
            email="supply@squarepharma.com.bd",
            is_active=True,
        )

        # 5. Products: Product A (Critically low stock), Product B (Normal stock)
        self.product_low = MedicineProduct.objects.create(
            product_id=f"med-low-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            strength="500mg/65mg",
            dosage_form="Tablet",
            manufacturer="Beximco Pharmaceuticals Ltd",
            cost_unit=Decimal("2.5000"),
            mrp_unit=Decimal("3.5000"),
        )

        # Batch for Product Low: only 5 units in stock (below ROP of 15 units)
        self.batch_low = BatchStock.objects.create(
            batch_id=f"bat-low-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product_low,
            batch_number=f"BAT-LOW-{self.suffix.upper()}",
            expiry_date=datetime.date.today() + datetime.timedelta(days=365),
            purchase_date=datetime.date.today(),
            quantity_base_units=5,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("2.5000"),
            status=BatchStatus.ACTIVE,
        )

    def test_01_generate_reorder_plan(self):
        """Generates a draft reorder proposal calculating lead time, safety stock, and ROP."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "planning_horizon_days": 14,
            "notes": "Automated replenishment proposal",
        }

        resp = self.client.post("/api/v2/procurement/reorder-plans/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["status"] == "DRAFT_PROPOSAL"
        assert data["total_recommended_skus"] >= 1
        assert Decimal(data["total_estimated_cost"]) > Decimal("0.0000")

        # Find Product Low recommendation
        item = next((i for i in data["items"] if i["product_id"] == self.product_low.product_id), None)
        assert item is not None
        assert item["current_stock_units"] == 5
        assert item["effective_stock_units"] == 5
        assert item["lead_time_days"] == 2
        assert item["safety_stock_units"] == 5
        assert item["reorder_point_units"] == 15
        assert item["raw_recommended_units"] > 0
        assert item["urgency"] in ("CRITICAL", "HIGH", "MEDIUM")

    def test_02_never_auto_order_silently_guard(self):
        """Converting an unapproved DRAFT_PROPOSAL directly to PO fails with ERR_PLAN_NOT_APPROVED."""
        self.client.force_authenticate(user=self.manager)
        # 1. Generate plan
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_id = gen_resp.json()["proposal_id"]

        # 2. Attempt direct conversion to PO without managerial approval
        po_resp = self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/convert-to-po/")
        assert po_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert po_resp.json()["error_code"] == "ERR_PLAN_NOT_APPROVED"

    def test_03_managerial_approval_with_adjustments(self):
        """Manager reviews proposal, adjusts quantities, and approves."""
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_data = gen_resp.json()
        plan_id = plan_data["proposal_id"]
        item_id = plan_data["items"][0]["item_id"]

        # Approve with adjusted quantity
        app_resp = self.client.post(
            f"/api/v2/procurement/reorder-plans/{plan_id}/approve/",
            data={
                "review_notes": "Increased order quantity by 50 units for promotional festival",
                "item_adjustments": [{"item_id": item_id, "approved_order_units": 150}],
            },
            format="json",
        )
        assert app_resp.status_code == status.HTTP_200_OK
        app_data = app_resp.json()

        assert app_data["status"] == "APPROVED"
        assert app_data["reviewed_by"] is not None
        assert "promotional festival" in app_data["review_notes"]

        approved_item = next(i for i in app_data["items"] if i["item_id"] == item_id)
        assert approved_item["approved_order_units"] == 150

    def test_04_convert_approved_plan_to_purchase_orders(self):
        """Converting an approved reorder plan creates real Purchase Orders and links line items."""
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_id = gen_resp.json()["proposal_id"]

        # Approve
        self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/approve/", data={"review_notes": "Approved"}, format="json")

        # Convert to PO
        conv_resp = self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/convert-to-po/")
        assert conv_resp.status_code == status.HTTP_200_OK
        conv_data = conv_resp.json()

        assert conv_data["status"] == "CONVERTED_TO_PO"
        assert len(conv_data["generated_purchase_orders"]) >= 1

        created_po_id = conv_data["generated_purchase_orders"][0]["po_id"]
        po = PurchaseOrder.objects.filter(po_id=created_po_id, tenant=self.tenant).first()
        assert po is not None
        assert po.status == PurchaseOrderStatus.DRAFT
        assert po.items.count() >= 1

    def test_05_fefo_exposure_deduction(self):
        """Near-expiry stock within threshold is deducted from effective stock."""
        # Create a batch expiring in 30 days (within 60-day threshold)
        BatchStock.objects.create(
            batch_id=f"bat-near-exp-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product_low,
            batch_number=f"BAT-EXP30-{self.suffix.upper()}",
            expiry_date=datetime.date.today() + datetime.timedelta(days=30),
            purchase_date=datetime.date.today(),
            quantity_base_units=20,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("2.5000"),
            status=BatchStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id, "expiry_threshold_days": 60},
            format="json",
        )
        assert gen_resp.status_code == status.HTTP_201_CREATED
        plan_data = gen_resp.json()

        item = next(i for i in plan_data["items"] if i["product_id"] == self.product_low.product_id)
        # Current stock: 5 + 20 = 25
        # Expiring soon: 20
        # Effective stock: 25 - 20 = 5 units
        assert item["current_stock_units"] == 25
        assert item["expiring_soon_units"] == 20
        assert item["effective_stock_units"] == 5

    def test_06_dismiss_reorder_proposal(self):
        """Manager dismisses proposal with reason."""
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_id = gen_resp.json()["proposal_id"]

        dismiss_resp = self.client.post(
            f"/api/v2/procurement/reorder-plans/{plan_id}/dismiss/",
            data={"reason": "Sufficient safety stock present in sister warehouse"},
            format="json",
        )
        assert dismiss_resp.status_code == status.HTTP_200_OK
        assert dismiss_resp.json()["status"] == "DISMISSED"

    def test_07_cannot_dismiss_already_converted_plan(self):
        """Attempting to dismiss a converted proposal raises ERR_CANNOT_DISMISS_CONVERTED."""
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_id = gen_resp.json()["proposal_id"]

        self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/approve/", data={"review_notes": "OK"}, format="json")
        self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/convert-to-po/")

        dis_resp = self.client.post(
            f"/api/v2/procurement/reorder-plans/{plan_id}/dismiss/",
            data={"reason": "Should fail"},
            format="json",
        )
        assert dis_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert dis_resp.json()["error_code"] == "ERR_CANNOT_DISMISS_CONVERTED"

    def test_08_cannot_approve_dismissed_or_converted_plan(self):
        """Attempting to approve a dismissed proposal raises ERR_INVALID_STATUS."""
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_id = gen_resp.json()["proposal_id"]

        self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/dismiss/", data={"reason": "Dismissed"}, format="json")

        app_resp = self.client.post(f"/api/v2/procurement/reorder-plans/{plan_id}/approve/", data={"review_notes": "Try"}, format="json")
        assert app_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert app_resp.json()["error_code"] == "ERR_INVALID_STATUS"

    def test_09_list_and_filter_reorder_plans(self):
        """Lists reorder plans with filtering by status and branch."""
        self.client.force_authenticate(user=self.manager)

        # Plan 1 (Draft)
        self.client.post("/api/v2/procurement/reorder-plans/", data={"branch_id": self.branch.branch_id}, format="json")

        # Plan 2 (Approved)
        p2 = self.client.post("/api/v2/procurement/reorder-plans/", data={"branch_id": self.branch.branch_id}, format="json").json()
        self.client.post(f"/api/v2/procurement/reorder-plans/{p2['proposal_id']}/approve/", data={"review_notes": "OK"}, format="json")

        # List all
        resp = self.client.get("/api/v2/procurement/reorder-plans/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["count"] == 2

        # Filter by status=APPROVED
        resp_app = self.client.get("/api/v2/procurement/reorder-plans/?status=APPROVED")
        assert resp_app.status_code == status.HTTP_200_OK
        assert resp_app.json()["count"] == 1
        assert resp_app.json()["results"][0]["status"] == "APPROVED"

    def test_10_rbac_and_cross_tenant_isolation(self):
        """Cashier role is blocked from reorder planning, and foreign tenants cannot access plans."""
        # 1. Cashier attempts to generate plan -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        resp_csh = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        assert resp_csh.status_code == status.HTTP_403_FORBIDDEN
        assert resp_csh.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Manager creates plan
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/reorder-plans/",
            data={"branch_id": self.branch.branch_id},
            format="json",
        )
        plan_id = gen_resp.json()["proposal_id"]

        # 3. Foreign tenant attempts to access plan -> 404 Not Found
        self.client.force_authenticate(user=self.foreign_manager)
        resp_foreign = self.client.get(f"/api/v2/procurement/reorder-plans/{plan_id}/")
        assert resp_foreign.status_code == status.HTTP_404_NOT_FOUND
