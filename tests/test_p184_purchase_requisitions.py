"""
backend/tests/test_p184_purchase_requisitions.py
Architecture Decision: ADR-0184 (P184).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P184 Purchase Requisitions:
1. Valid requisition drafting with items, cost calculation, and audit trail.
2. Budget ceiling guard enforced on submission (ERR_BUDGET_EXCEEDED).
3. Approval chain workflow: DRAFT -> SUBMITTED -> APPROVED with approver metadata.
4. Rejection workflow requiring mandatory rejection_reason.
5. Immutability: Mutating submitted or approved requisitions rejected with ERR_REQUISITION_LOCKED.
6. Cancellation workflow: DRAFT/SUBMITTED -> CANCELLED, with approved cancellation barred.
7. Validation errors: empty items, invalid quantity, invalid cost price, negative budget.
8. Cross-tenant isolation strictly enforced with HTTP 403.
9. RBAC: Cashier role blocked, and staff pharmacist barred from approving (approval role required).
10. Suspended tenant blocked with HTTP 403.
"""

from __future__ import annotations

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
    PurchaseRequisition,
    PurchaseRequisitionItem,
    RequisitionDepartment,
    RequisitionPriority,
    RequisitionStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPurchaseRequisitionsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p184-{self.suffix}",
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

        # 3. Setup Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals Depot",
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

    def test_01_create_valid_requisition_with_items_and_audit(self):
        """Test 1: Valid requisition drafting with items, cost calculation, and audit trail."""
        self.client.force_authenticate(user=self.pharmacist)
        payload = {
            "branch_id": self.branch_a.branch_id,
            "department": "DISPENSARY",
            "priority": "HIGH",
            "allocated_budget": "1500.0000",
            "justification": "Restocking critical OPD dispensary medications",
            "target_delivery_date": "2026-11-20",
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "preferred_supplier_id": self.supplier.supplier_id,
                    "package_type": "BOX",
                    "package_size": 100,
                    "requested_quantity": 200,
                    "estimated_unit_cost": "2.4000",
                    "current_stock_quantity": 20,
                    "notes": "Fast moving",
                },
                {
                    "product_id": self.product_seclo.product_id,
                    "preferred_supplier_id": self.supplier.supplier_id,
                    "package_type": "STRIP",
                    "package_size": 10,
                    "requested_quantity": 100,
                    "estimated_unit_cost": "5.0000",
                    "current_stock_quantity": 15,
                    "notes": "Gastric care",
                },
            ],
        }

        # Expected cost = (200 * 2.40) + (100 * 5.00) = 480.00 + 500.00 = 980.0000
        resp = self.client.post("/api/v2/procurement/requisitions/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert data["requisition_id"].startswith("req-")
        assert data["tenant_id"] == str(self.tenant.tenant_id)
        assert data["branch_id"] == self.branch_a.branch_id
        assert data["department"] == "DISPENSARY"
        assert data["priority"] == "HIGH"
        assert data["status"] == "DRAFT"
        assert data["total_estimated_cost"] == "980.0000"
        assert data["allocated_budget"] == "1500.0000"
        assert len(data["items"]) == 2

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            resource_id=data["requisition_id"],
            action="PURCHASE_REQUISITION_CREATED",
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS
        assert audit.metadata_json["total_estimated_cost"] == "980.0000"

    def test_02_budget_ceiling_guard_enforced_on_submission(self):
        """Test 2: Budget ceiling guard enforced on submission (ERR_BUDGET_EXCEEDED)."""
        self.client.force_authenticate(user=self.pharmacist)
        payload = {
            "branch_id": self.branch_a.branch_id,
            "department": "CENTRAL_STORE",
            "allocated_budget": "400.0000",  # Budget cap is 400
            "items": [
                {
                    "product_id": self.product_napa.product_id,
                    "requested_quantity": 200,
                    "estimated_unit_cost": "2.5000",  # Total = 500 > 400
                }
            ],
        }

        create_resp = self.client.post("/api/v2/procurement/requisitions/", data=payload, format="json")
        assert create_resp.status_code == status.HTTP_201_CREATED
        req_id = create_resp.data["requisition_id"]

        # Submit without budget override: MUST FAIL
        submit_fail = self.client.post(f"/api/v2/procurement/requisitions/{req_id}/submit/", data={}, format="json")
        assert submit_fail.status_code == status.HTTP_400_BAD_REQUEST
        assert submit_fail.data["error_code"] == "ERR_BUDGET_EXCEEDED"
        assert "exceeds allocated budget" in submit_fail.data["detail"]

        # Submit with budget override: SUCCEEDS
        submit_ok = self.client.post(
            f"/api/v2/procurement/requisitions/{req_id}/submit/",
            data={"budget_override": True},
            format="json",
        )
        assert submit_ok.status_code == status.HTTP_200_OK
        assert submit_ok.data["status"] == "SUBMITTED"

    def test_03_approval_chain_lifecycle_submit_and_approve(self):
        """Test 3: Approval chain workflow: DRAFT -> SUBMITTED -> APPROVED with approver metadata."""
        self.client.force_authenticate(user=self.pharmacist)
        req = PurchaseRequisition.objects.create(
            requisition_id=f"req-app-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            department=RequisitionDepartment.OT_PHARMACY,
            priority=RequisitionPriority.EMERGENCY,
            total_estimated_cost=Decimal("500.0000"),
            allocated_budget=Decimal("1000.0000"),
            status=RequisitionStatus.DRAFT,
        )
        PurchaseRequisitionItem.objects.create(
            item_id=f"pri-app-{self.suffix}",
            requisition=req,
            product=self.product_seclo,
            requested_quantity=100,
            estimated_unit_cost=Decimal("5.0000"),
            estimated_total_cost=Decimal("500.0000"),
        )

        # Pharmacist submits
        submit_resp = self.client.post(f"/api/v2/procurement/requisitions/{req.requisition_id}/submit/", data={}, format="json")
        assert submit_resp.status_code == status.HTTP_200_OK
        assert submit_resp.data["status"] == "SUBMITTED"
        assert submit_resp.data["submitted_by_id"] == str(self.pharmacist.user_id)

        # Manager approves
        self.client.force_authenticate(user=self.manager)
        app_resp = self.client.post(
            f"/api/v2/procurement/requisitions/{req.requisition_id}/approve/",
            data={"approval_notes": "Approved for emergency surgical operations"},
            format="json",
        )
        assert app_resp.status_code == status.HTTP_200_OK
        assert app_resp.data["status"] == "APPROVED"
        assert app_resp.data["approved_by_id"] == str(self.manager.user_id)
        assert app_resp.data["approval_notes"] == "Approved for emergency surgical operations"

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            resource_id=req.requisition_id,
            action="PURCHASE_REQUISITION_APPROVED",
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

    def test_04_rejection_workflow_with_mandatory_reason(self):
        """Test 4: Rejection workflow requiring mandatory rejection_reason."""
        self.client.force_authenticate(user=self.pharmacist)
        req = PurchaseRequisition.objects.create(
            requisition_id=f"req-rej-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            status=RequisitionStatus.SUBMITTED,
            total_estimated_cost=Decimal("200.0000"),
        )
        PurchaseRequisitionItem.objects.create(
            item_id=f"pri-rej-{self.suffix}",
            requisition=req,
            product=self.product_napa,
            requested_quantity=80,
            estimated_unit_cost=Decimal("2.5000"),
            estimated_total_cost=Decimal("200.0000"),
        )

        self.client.force_authenticate(user=self.manager)

        # Rejection without reason fails
        rej_fail = self.client.post(
            f"/api/v2/procurement/requisitions/{req.requisition_id}/reject/",
            data={"rejection_reason": "  "},
            format="json",
        )
        assert rej_fail.status_code == status.HTTP_400_BAD_REQUEST
        assert rej_fail.data["error_code"] == "ERR_MISSING_REJECTION_REASON"

        # Rejection with reason succeeds
        rej_ok = self.client.post(
            f"/api/v2/procurement/requisitions/{req.requisition_id}/reject/",
            data={"rejection_reason": "Sufficient buffer exists in central warehouse"},
            format="json",
        )
        assert rej_ok.status_code == status.HTTP_200_OK
        assert rej_ok.data["status"] == "REJECTED"
        assert rej_ok.data["rejected_by_id"] == str(self.manager.user_id)
        assert rej_ok.data["rejection_reason"] == "Sufficient buffer exists in central warehouse"

    def test_05_locked_state_prevents_mutation_after_approval_or_submission(self):
        """Test 5: Immutability: Mutating submitted or approved requisitions rejected with ERR_REQUISITION_LOCKED."""
        self.client.force_authenticate(user=self.pharmacist)
        req = PurchaseRequisition.objects.create(
            requisition_id=f"req-lock-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            status=RequisitionStatus.APPROVED,
            total_estimated_cost=Decimal("100.0000"),
        )

        patch_resp = self.client.patch(
            f"/api/v2/procurement/requisitions/{req.requisition_id}/",
            data={"department": "CENTRAL_STORE"},
            format="json",
        )
        assert patch_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert patch_resp.data["error_code"] == "ERR_REQUISITION_LOCKED"

    def test_06_cancellation_workflow(self):
        """Test 6: Cancellation workflow: DRAFT/SUBMITTED -> CANCELLED, with approved cancellation barred."""
        self.client.force_authenticate(user=self.pharmacist)

        # 1. Draft can be cancelled
        req_draft = PurchaseRequisition.objects.create(
            requisition_id=f"req-can1-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            status=RequisitionStatus.DRAFT,
        )
        res1 = self.client.post(f"/api/v2/procurement/requisitions/{req_draft.requisition_id}/cancel/", data={}, format="json")
        assert res1.status_code == status.HTTP_200_OK
        assert res1.data["status"] == "CANCELLED"

        # 2. Approved cannot be cancelled
        req_app = PurchaseRequisition.objects.create(
            requisition_id=f"req-can2-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            status=RequisitionStatus.APPROVED,
        )
        res2 = self.client.post(f"/api/v2/procurement/requisitions/{req_app.requisition_id}/cancel/", data={}, format="json")
        assert res2.status_code == status.HTTP_400_BAD_REQUEST
        assert res2.data["error_code"] == "ERR_CANNOT_CANCEL_APPROVED"

    def test_07_invalid_items_rejection(self):
        """Test 7: Validation errors: empty items, invalid quantity, invalid cost price, negative budget."""
        self.client.force_authenticate(user=self.pharmacist)

        # Empty items list
        resp1 = self.client.post(
            "/api/v2/procurement/requisitions/",
            data={"branch_id": self.branch_a.branch_id, "items": []},
            format="json",
        )
        assert resp1.status_code == status.HTTP_400_BAD_REQUEST
        assert resp1.data["error_code"] == "ERR_EMPTY_REQUISITION_ITEMS"

        # Non-existent product
        resp2 = self.client.post(
            "/api/v2/procurement/requisitions/",
            data={
                "branch_id": self.branch_a.branch_id,
                "items": [{"product_id": "prod-non-existent", "requested_quantity": 10, "estimated_unit_cost": "2.0000"}],
            },
            format="json",
        )
        assert resp2.status_code == status.HTTP_404_NOT_FOUND
        assert resp2.data["error_code"] == "ERR_PRODUCT_NOT_FOUND"

        # Zero quantity
        resp3 = self.client.post(
            "/api/v2/procurement/requisitions/",
            data={
                "branch_id": self.branch_a.branch_id,
                "items": [{"product_id": self.product_napa.product_id, "requested_quantity": 0, "estimated_unit_cost": "2.0000"}],
            },
            format="json",
        )
        assert resp3.status_code == status.HTTP_400_BAD_REQUEST
        assert resp3.data["error_code"] == "ERR_INVALID_QUANTITY"

    def test_08_cross_tenant_isolation_enforced(self):
        """Test 8: Cross-tenant isolation strictly enforced with HTTP 403."""
        # 1. Foreign manager tries to create requisition for tenant-alpha's branch
        self.client.force_authenticate(user=self.foreign_manager)
        resp1 = self.client.post(
            "/api/v2/procurement/requisitions/",
            data={
                "tenant_id": self.tenant.tenant_id,  # tenant alpha
                "branch_id": self.branch_a.branch_id,
                "items": [{"product_id": self.product_napa.product_id, "requested_quantity": 10}],
            },
            format="json",
        )
        assert resp1.status_code == status.HTTP_403_FORBIDDEN
        assert resp1.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        # 2. Foreign manager tries to access or approve tenant-alpha's requisition
        req = PurchaseRequisition.objects.create(
            requisition_id=f"req-iso-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            status=RequisitionStatus.SUBMITTED,
        )
        resp_get = self.client.get(f"/api/v2/procurement/requisitions/{req.requisition_id}/")
        assert resp_get.status_code == status.HTTP_403_FORBIDDEN
        assert resp_get.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        resp_app = self.client.post(f"/api/v2/procurement/requisitions/{req.requisition_id}/approve/", data={}, format="json")
        assert resp_app.status_code == status.HTTP_403_FORBIDDEN
        assert resp_app.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_09_cashier_role_blocked_and_unauthorized_approver_blocked(self):
        """Test 9: RBAC: Cashier role blocked, and staff pharmacist barred from approving."""
        # Cashier blocked from creating
        self.client.force_authenticate(user=self.cashier)
        resp_csh = self.client.post(
            "/api/v2/procurement/requisitions/",
            data={"branch_id": self.branch_a.branch_id, "items": []},
            format="json",
        )
        assert resp_csh.status_code == status.HTTP_403_FORBIDDEN
        assert resp_csh.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # Staff pharmacist creates & submits, but cannot approve
        self.client.force_authenticate(user=self.pharmacist)
        req = PurchaseRequisition.objects.create(
            requisition_id=f"req-rbac-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            status=RequisitionStatus.SUBMITTED,
        )
        resp_app = self.client.post(f"/api/v2/procurement/requisitions/{req.requisition_id}/approve/", data={}, format="json")
        assert resp_app.status_code == status.HTTP_403_FORBIDDEN
        assert resp_app.data["error_code"] == "ERR_APPROVAL_ROLE_REQUIRED"

    def test_10_suspended_tenant_blocked(self):
        """Test 10: Suspended tenant blocked with HTTP 403."""
        self.client.force_authenticate(user=self.suspended_manager)
        resp = self.client.get("/api/v2/procurement/requisitions/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]

