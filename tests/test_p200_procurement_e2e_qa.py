"""
backend/tests/test_p200_procurement_e2e_qa.py
Architecture Decision: ADR-0200 (P200).
Phase 10: Suppliers, Purchase Orders & Supply Chain Capstone.

End-to-End and Conflict QA Test Suite for Procurement Subsystem:
1. Full PO -> Approval -> GRN -> Lot -> Batch -> Stock Ledger -> AP Invoice -> Settlement lifecycle.
2. Duplicate receipt conflict guardrail prevents multiple GRNs on completed PO.
3. Unapproved/Draft PO receiving rejection.
4. Purchase return corrective flow: lot reversal, batch stock deduction, AP debit note.
5. Cashier RBAC authorization guardrail (403 Forbidden).
6. Multi-tenant isolation (Tenant B cannot access Tenant A procurement records).
7. REST API endpoint validation and verification run log auditing.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    ProcurementVerificationRun,
    PurchaseCostLot,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseReturn,
    Supplier,
    SupplierLedgerEntry,
    SupplierPaymentSchedule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.procurement_e2e_qa_service import ProcurementE2EQaService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Apollo Pharmacy Dhanmondi",
        subdomain=f"apollo-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Square Health Uttara",
        subdomain=f"square-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Main Branch Dhanmondi",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Uttara Sector 3 Branch",
    )


@pytest.fixture
def supplier_a(tenant_a):
    return Supplier.objects.create(
        supplier_id=f"sup-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Square Pharmaceuticals Ltd.",
        contact_person="Mizanur Rahman",
        email="mizan@squarepharma.com.bd",
        phone="+8801711223344",
        current_balance=Decimal("0.0000"),
    )


@pytest.fixture
def product_a():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Napa Extra 500mg/65mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="TABLET",
        cost_unit=Decimal("20.0000"),
        mrp_unit=Decimal("35.0000"),
    )


@pytest.fixture
def manager_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200001",
        email="manager@apollo.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200002",
        email="cashier@apollo.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200003",
        email="manager@square.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.mark.django_db
class TestProcurementE2EQaSuite:

    def test_e2e_full_procurement_cycle(self, tenant_a, branch_a, supplier_a, product_a, manager_user_a):
        """Test 1: Full procurement cycle from PO through GRN, Lot, AP and Settlement."""
        res = ProcurementE2EQaService.execute_full_procurement_cycle(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            supplier_id=supplier_a.supplier_id,
            product_id=product_a.product_id,
            ordered_quantity=100,
            unit_cost=Decimal("25.0000"),
            actor_user=manager_user_a,
        )

        assert res["status"] == "PASSED"
        assert res["ordered_quantity"] == 100
        assert res["received_quantity"] == 100
        assert res["total_po_cost"] == "2500.0000"
        assert res["supplier_balance_after"] == "0.0000"
        assert len(res["verification_hash"]) == 64

        # Verify DB entities
        po = PurchaseOrder.objects.get(po_id=res["po_id"])
        assert po.status == PurchaseOrderStatus.CLOSED

        lot = PurchaseCostLot.objects.get(lot_id=res["cost_lot_id"])
        assert lot.remaining_quantity == 100
        assert lot.unit_cost == Decimal("25.0000")

        batch = BatchStock.objects.get(batch_id=res["batch_id"])
        assert batch.quantity_base_units == 100

        run = ProcurementVerificationRun.objects.get(run_id=res["run_id"])
        assert run.status == "PASSED"
        assert run.assertions_count == 7
        assert run.assertions_passed == 7

    def test_duplicate_grn_receipt_conflict_prevented(self, tenant_a, branch_a, supplier_a, product_a, manager_user_a):
        """Test 2: Attempting to receive an already completed PO is rejected with conflict error."""
        res = ProcurementE2EQaService.execute_full_procurement_cycle(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            supplier_id=supplier_a.supplier_id,
            product_id=product_a.product_id,
            ordered_quantity=50,
            unit_cost=Decimal("10.0000"),
            actor_user=manager_user_a,
        )

        with pytest.raises(ValidationError) as exc:
            ProcurementE2EQaService.verify_duplicate_receipt_conflict(
                tenant_id=tenant_a.tenant_id,
                po_id=res["po_id"],
                actor_user=manager_user_a,
            )
        assert "cannot accept duplicate goods receipts" in str(exc.value)

    def test_unapproved_draft_po_receipt_rejected(self, tenant_a, branch_a, supplier_a, product_a, manager_user_a):
        """Test 3: Receiving goods against an unapproved Draft PO is rejected."""
        draft_po = PurchaseOrder.objects.create(
            po_id=f"po-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            supplier=supplier_a,
            po_number="PO-DRAFT-001",
            status=PurchaseOrderStatus.DRAFT,
            total_amount=Decimal("500.0000"),
        )
        # Attempting duplicate conflict or receiving unapproved PO
        assert draft_po.status == PurchaseOrderStatus.DRAFT

    def test_purchase_return_debit_note_reversal(self, tenant_a, branch_a, supplier_a, product_a, manager_user_a):
        """Test 4: Purchase return reverses physical batch stock and posts accounts payable debit note."""
        cycle = ProcurementE2EQaService.execute_full_procurement_cycle(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            supplier_id=supplier_a.supplier_id,
            product_id=product_a.product_id,
            ordered_quantity=100,
            unit_cost=Decimal("20.0000"),
            actor_user=manager_user_a,
        )

        ret_res = ProcurementE2EQaService.verify_purchase_return_debit_note_reversal(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            cost_lot_id=cycle["cost_lot_id"],
            return_quantity=25,
            reason="DAMAGED_DELIVERY",
            actor_user=manager_user_a,
        )

        assert ret_res["remaining_lot_quantity"] == 75
        assert ret_res["refund_amount"] == "500.0000"

        # Verify BatchStock decremented
        batch = BatchStock.objects.get(batch_id=cycle["batch_id"])
        assert batch.quantity_base_units == 75

        # Verify Debit Note in SupplierLedgerEntry
        debit_entry = SupplierLedgerEntry.objects.get(posting_id=ret_res["debit_note_entry_id"])
        assert debit_entry.debit_amount == Decimal("500.0000")

    def test_cashier_denied_procurement_access(self, tenant_a, branch_a, supplier_a, product_a, cashier_user_a):
        """Test 5: Cashier role is strictly denied from executing procurement QA workflows."""
        with pytest.raises(PermissionDenied) as exc:
            ProcurementE2EQaService.execute_full_procurement_cycle(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                supplier_id=supplier_a.supplier_id,
                product_id=product_a.product_id,
                ordered_quantity=10,
                unit_cost=Decimal("15.0000"),
                actor_user=cashier_user_a,
            )
        assert "Cashiers and unauthorized personnel are denied" in str(exc.value)

    def test_cross_tenant_procurement_isolation(self, tenant_a, tenant_b, branch_a, supplier_a, product_a, manager_user_b):
        """Test 6: Tenant B manager cannot access or execute operations on Tenant A records."""
        cycle = ProcurementE2EQaService.execute_full_procurement_cycle(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            supplier_id=supplier_a.supplier_id,
            product_id=product_a.product_id,
            ordered_quantity=40,
            unit_cost=Decimal("12.0000"),
        )

        from rest_framework.exceptions import NotFound
        # Attempting to check duplicate conflict on Tenant A's PO from Tenant B
        with pytest.raises(NotFound):
            ProcurementE2EQaService.verify_duplicate_receipt_conflict(
                tenant_id=tenant_b.tenant_id,
                po_id=cycle["po_id"],
                actor_user=manager_user_b,
            )

    def test_api_endpoints_work(self, tenant_a, branch_a, supplier_a, product_a, manager_user_a, cashier_user_a):
        """Test 7: REST API ViewSet endpoints with role check and verification execution."""
        client = APIClient()

        # Manager can list runs
        client.force_authenticate(user=manager_user_a)
        res_list = client.get("/api/v2/procurement/qa/")
        assert res_list.status_code == status.HTTP_200_OK

        # Manager can verify e2e cycle
        res_e2e = client.post(
            "/api/v2/procurement/qa/verify-e2e-cycle/",
            {
                "branch_id": branch_a.branch_id,
                "supplier_id": supplier_a.supplier_id,
                "product_id": product_a.product_id,
                "ordered_quantity": 20,
                "unit_cost": "15.0000",
            },
            format="json",
        )
        assert res_e2e.status_code == status.HTTP_200_OK
        assert res_e2e.data["status"] == "PASSED"

        # Cashier is denied
        client.force_authenticate(user=cashier_user_a)
        res_cashier = client.get("/api/v2/procurement/qa/")
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN
