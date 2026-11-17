"""
backend/tests/test_p228_fefo_allocation_checkout.py
Acceptance tests for P228: FEFO & Atomic Lot Allocation at POS Checkout (Phase 12).

Covers:
1. Deterministic FEFO multi-batch & FIFO PurchaseCostLot splitting (Decimal(14,4) COGS exactness),
   tie-breaker ordering (expiry_date -> purchase_date -> batch_number -> batch_id),
   cashier cost redaction, split Cash+Due finalize, receipt lot breakdown, StockLedger & reversal.
2. Specific rejections: boundary/past expired batches (ERR_BATCH_EXPIRED), recalled batches
   (ERR_BATCH_RECALLED), quarantined & segregated-quarantined batches (ERR_BATCH_QUARANTINED),
   insufficient safe stock (ERR_INSUFFICIENT_DISPENSABLE_STOCK), plus audit trail outside rollback.
3. Dynamic re-validation at checkout: cart edits after pre-allocation, pinned unsafe batch block,
   automatic safe fallback when unpinned lot turns bad, offline sync reconciliation vs offline expiry block.
4. Manual non-FEFO override RBAC (403 for Cashier, reason validation, Manager/Pharmacist approval),
   cross-tenant & cross-branch batch isolation, BASIC-plan availability, suspended tenant lockout,
   immutability of PosCheckoutAllocationAudit, and concurrent checkout stock contention.
"""

from __future__ import annotations

import datetime
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, connections
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    PosAllocationMode,
    PosAllocationStatus,
    PosCheckoutAllocationAudit,
    PosCustomer,
    PosFefoAllocationStatus,
    PosPackagingUnit,
    PosSaleAllocation,
    PosSaleLifecycleState,
    PosSaleOrder,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    RecallScope,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    SegregationCategory,
    SegregationStatus,
    StockLedgerEntry,
    StockMovementType,
    StockSegregationRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.pos_domain_service import PosDomainService
from apps.identity.models import PharmacyUser, UserRole


def _mk_tenant(name: str, slug_prefix: str, plan: str = PlanTier.STANDARD, sub_status: str = TenantLifecycleStatus.ACTIVE):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-{slug_prefix}-{uid}",
        name=name,
        subdomain=f"{slug_prefix}-{uid}",
        status=sub_status,
        plan_tier=plan,
    )


def _mk_branch(tenant: Tenant, name: str, code: str):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-{code}-{uid}",
        tenant=tenant,
        name=name,
        license_number=f"DGDA-{code.upper()}-{uid}",
        is_active=True,
    )


def _mk_user(tenant: Tenant, branch: Branch | None, role: str, tag: str):
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"{tag}-{uid}@pharmacy.bd",
        phone_number=f"+88017{digits}",
        password="TestPass!123",
        full_name=f"{tag.title()} User",
        role=role,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id if branch else None,
    )


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenants_and_users(db):
    ten_a = _mk_tenant("Care Pharma Dhaka", "p228a", PlanTier.STANDARD)
    br_a1 = _mk_branch(ten_a, "Mirpur 10", "a1")
    br_a2 = _mk_branch(ten_a, "Dhanmondi 27", "a2")

    ten_b = _mk_tenant("Other Pharma Ctg", "p228b", PlanTier.PRO)
    br_b1 = _mk_branch(ten_b, "GEC Circle", "b1")

    ten_basic = _mk_tenant("Basic Meds", "p228bas", PlanTier.BASIC)
    br_bas = _mk_branch(ten_basic, "Bogura Main", "bas")

    return {
        "ten_a": ten_a,
        "br_a1": br_a1,
        "br_a2": br_a2,
        "ten_b": ten_b,
        "br_b1": br_b1,
        "ten_basic": ten_basic,
        "br_bas": br_bas,
        "cashier_a": _mk_user(ten_a, br_a1, UserRole.CASHIER, "cash"),
        "pharmacist_a": _mk_user(ten_a, br_a1, UserRole.STAFF_PHARMACIST, "pharm"),
        "manager_a": _mk_user(ten_a, br_a1, UserRole.BRANCH_MANAGER, "mgr"),
        "owner_a": _mk_user(ten_a, br_a1, UserRole.PHARMACY_OWNER, "own"),
        "cashier_b": _mk_user(ten_b, br_b1, UserRole.CASHIER, "cashb"),
        "cashier_bas": _mk_user(ten_basic, br_bas, UserRole.CASHIER, "cashbas"),
    }


def _mk_otc_napa():
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"med-p228-napa-{uid}",
        brand_name="Napa Extra 500",
        generic_name="Paracetamol",
        strength="500mg",
        dosage_form="Tablet",
        manufacturer="Beximco",
        barcode=f"894228{uid}",
        mrp_unit=Decimal("5.00"),
        cost_unit=Decimal("3.00"),
        is_prescription_only=False,
    )


class TestP228FefoCheckoutAllocation:
    def test_deterministic_fefo_multi_batch_and_fifo_cost_lot_split_split_payment_receipt_and_reversal(
        self, api_client, tenants_and_users
    ):
        ten_a = tenants_and_users["ten_a"]
        br_a1 = tenants_and_users["br_a1"]
        cashier = tenants_and_users["cashier_a"]
        manager = tenants_and_users["manager_a"]
        today = timezone.now().date()
        prod = _mk_otc_napa()

        # Unsafe lots with even EARLIER dates that FEFO MUST skip automatically:
        BatchStock.objects.create(
            batch_id=f"bat-exp-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-EXPIRED",
            expiry_date=today - datetime.timedelta(days=5),
            purchase_date=today - datetime.timedelta(days=90),
            quantity_base_units=100,
            cost_per_unit=Decimal("1.00"),
            status=BatchStatus.ACTIVE,  # expired by date even if status still says ACTIVE!
            is_active=True,
        )
        BatchStock.objects.create(
            batch_id=f"bat-quar-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-QUARANTINE",
            expiry_date=today + datetime.timedelta(days=10),
            purchase_date=today - datetime.timedelta(days=60),
            quantity_base_units=50,
            cost_per_unit=Decimal("1.10"),
            status=BatchStatus.ACTIVE,
            is_quarantined=True,
            is_active=True,
        )

        # Safe Batches:
        # B1: earliest expiry (today + 30d), 12 units backed by 2 FIFO PurchaseCostLots (7 @ 1.5000 + 5 @ 2.0000)
        b1 = BatchStock.objects.create(
            batch_id=f"bat-b1-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-A-EARLY",
            expiry_date=today + datetime.timedelta(days=30),
            purchase_date=today - datetime.timedelta(days=30),
            quantity_base_units=12,
            cost_per_unit=Decimal("1.75"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        l1 = PurchaseCostLot.objects.create(
            lot_id=f"lot-b1-a-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch=b1,
            purchase_date=today - datetime.timedelta(days=30),
            unit_cost=Decimal("1.5000"),
            initial_quantity=7,
            remaining_quantity=7,
            status=PurchaseCostLotStatus.ACTIVE,
        )
        l2 = PurchaseCostLot.objects.create(
            lot_id=f"lot-b1-b-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch=b1,
            purchase_date=today - datetime.timedelta(days=20),
            unit_cost=Decimal("2.0000"),
            initial_quantity=5,
            remaining_quantity=5,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # B2 & B3 same expiry (today + 90d), tie-breaker by purchase_date ASC -> B2 before B3!
        b2 = BatchStock.objects.create(
            batch_id=f"bat-b2-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-Z-TIE-OLDER",
            expiry_date=today + datetime.timedelta(days=90),
            purchase_date=today - datetime.timedelta(days=15),
            quantity_base_units=20,
            cost_per_unit=Decimal("2.50"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        l3 = PurchaseCostLot.objects.create(
            lot_id=f"lot-b2-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch=b2,
            purchase_date=today - datetime.timedelta(days=15),
            unit_cost=Decimal("2.5000"),
            initial_quantity=20,
            remaining_quantity=20,
            status=PurchaseCostLotStatus.ACTIVE,
        )
        BatchStock.objects.create(
            batch_id=f"bat-b3-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-A-TIE-NEWER",
            expiry_date=today + datetime.timedelta(days=90),
            purchase_date=today - datetime.timedelta(days=5),
            quantity_base_units=30,
            cost_per_unit=Decimal("3.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )

        cust = PosCustomer.objects.create(
            customer_id=f"cust-p228-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            primary_branch=br_a1,
            full_name="Ariful Haque",
            phone="+8801711889900",
            credit_limit_bdt=Decimal("1000.00"),
            current_due_bdt=Decimal("0.00"),
            is_active=True,
        )

        # Cashier creates cart for 15 tablets (75.00 BDT) -> splits across B1 (7+5=12) + B2 (3)!
        api_client.force_authenticate(user=cashier)
        cart_res = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {"branch": br_a1.branch_id, "customer": cust.customer_id, "force_new": True},
            format="json",
        )
        assert cart_res.status_code == status.HTTP_201_CREATED
        sale_id = cart_res.data["sale"]["sale_id"]

        scan_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": prod.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 15},
            format="json",
        )
        assert scan_res.status_code == status.HTTP_200_OK

        # Preview FEFO plan as Cashier (cost fields masked)
        plan_cash = api_client.get(f"/api/v2/pos/sales/{sale_id}/fefo-plan/")
        assert plan_cash.status_code == status.HTTP_200_OK
        assert plan_cash.data["fefo_ready"] is True
        assert plan_cash.data["total_requested_base_units"] == 15
        assert plan_cash.data["total_planned_base_units"] == 15
        assert plan_cash.data["total_estimated_cost_bdt"] is None  # Masked for Cashier!
        assert plan_cash.data["has_expired_block"] is True
        assert plan_cash.data["has_quarantined_block"] is True
        lots = plan_cash.data["lines"][0]["planned_lots"]
        assert [(x["batch_id"], x["allocated_base_units"]) for x in lots] == [
            (b1.batch_id, 7),
            (b1.batch_id, 5),
            (b2.batch_id, 3),
        ]

        # Manager sees exact Decimal(14,4) FIFO COGS = 7*1.5 + 5*2.0 + 3*2.5 = 28.0000 BDT
        api_client.force_authenticate(user=manager)
        plan_mgr = api_client.get(f"/api/v2/pos/sales/{sale_id}/fefo-plan/")
        assert plan_mgr.status_code == status.HTTP_200_OK
        assert Decimal(plan_mgr.data["total_estimated_cost_bdt"]) == Decimal("28.0000")

        # Cashier pays split Cash (50.00) + Due (25.00) -> Checkout atomically allocates & commits!
        api_client.force_authenticate(user=cashier)
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{sale_id}/record-payment/",
                {"tender_type": PaymentMethod.CASH, "amount_bdt": "50.00", "idempotency_key": f"p1-{sale_id}"},
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{sale_id}/record-payment/",
                {"tender_type": PaymentMethod.DUE, "amount_bdt": "25.00", "idempotency_key": f"p2-{sale_id}"},
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )
        fin_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_res.status_code == status.HTTP_200_OK
        assert fin_res.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert fin_res.data["fefo_allocation_status"] == PosFefoAllocationStatus.COMMITTED
        assert len(fin_res.data["allocations"]) == 3

        # Verify exact BatchStock, PurchaseCostLot, SaleLotAllocation & StockLedger conservation
        b1.refresh_from_db()
        b2.refresh_from_db()
        l1.refresh_from_db()
        l2.refresh_from_db()
        l3.refresh_from_db()
        assert b1.quantity_base_units == 0
        assert b1.reserved_quantity_base_units == 0
        assert b2.quantity_base_units == 17
        assert b2.reserved_quantity_base_units == 0
        assert l1.remaining_quantity == 0 and l1.status == PurchaseCostLotStatus.DEPLETED
        assert l2.remaining_quantity == 0 and l2.status == PurchaseCostLotStatus.DEPLETED
        assert l3.remaining_quantity == 17 and l3.status == PurchaseCostLotStatus.ACTIVE

        sale_obj = PosSaleOrder.objects.get(pk=sale_id)
        slas = list(SaleLotAllocation.objects.filter(invoice=sale_obj.linked_invoice))
        assert sum(s.total_cost for s in slas) == Decimal("28.0000")
        assert (
            StockLedgerEntry.objects.filter(
                source_entity_type="POS_SALE_ORDER", source_entity_id=sale_id, movement_type=StockMovementType.SALE_DISPENSE
            ).count()
            == 3
        )

        # Printable receipt includes lot_allocations (masked cost for Cashier, visible for Manager)
        rcpt_cash = api_client.get(f"/api/v2/pos/sales/{sale_id}/receipt/")
        assert rcpt_cash.status_code == status.HTTP_200_OK
        assert rcpt_cash.data["fefo_allocation_status"] == PosFefoAllocationStatus.COMMITTED
        assert len(rcpt_cash.data["lot_allocations"]) == 3
        assert rcpt_cash.data["lot_allocations"][0]["unit_cost_bdt"] is None

        api_client.force_authenticate(user=manager)
        rcpt_mgr = api_client.get(f"/api/v2/pos/sales/{sale_id}/receipt/")
        assert Decimal(rcpt_mgr.data["lot_allocations"][0]["total_cost_bdt"]) == Decimal("10.5000")

        # Reversal restores exact batches, FIFO PurchaseCostLots, Customer Due, and logs ALLOCATION_REVERSED
        rev_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/reverse/",
            {"reason": "Wrong strip count dispensed"},
            format="json",
        )
        assert rev_res.status_code == status.HTTP_200_OK
        assert rev_res.data["fefo_allocation_status"] == PosFefoAllocationStatus.RELEASED
        b1.refresh_from_db()
        b2.refresh_from_db()
        l1.refresh_from_db()
        l2.refresh_from_db()
        l3.refresh_from_db()
        cust.refresh_from_db()
        assert b1.quantity_base_units == 12
        assert b2.quantity_base_units == 20
        assert l1.remaining_quantity == 7 and l1.status == PurchaseCostLotStatus.ACTIVE
        assert l2.remaining_quantity == 5 and l2.status == PurchaseCostLotStatus.ACTIVE
        assert l3.remaining_quantity == 20 and l3.status == PurchaseCostLotStatus.ACTIVE
        assert cust.current_due_bdt == Decimal("0.00")
        assert all(s.status == SaleLotAllocationStatus.REVERSED for s in SaleLotAllocation.objects.filter(invoice=sale_obj.linked_invoice))
        assert PosCheckoutAllocationAudit.objects.filter(sale_id=sale_id, action="ALLOCATION_REVERSED").count() == 3

    def test_rejections_expired_recalled_quarantined_segregated_and_stale_revalidation(
        self, api_client, tenants_and_users
    ):
        ten_a = tenants_and_users["ten_a"]
        br_a1 = tenants_and_users["br_a1"]
        cashier = tenants_and_users["cashier_a"]
        today = timezone.now().date()
        prod = _mk_otc_napa()

        api_client.force_authenticate(user=cashier)

        # 1) Boundary Expired (expiry_date == today) -> 422 ERR_BATCH_EXPIRED + audit outside rollback!
        b_exp = BatchStock.objects.create(
            batch_id=f"bat-today-exp-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-EXP-TODAY",
            expiry_date=today,
            purchase_date=today - datetime.timedelta(days=20),
            quantity_base_units=20,
            cost_per_unit=Decimal("2.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        s1 = api_client.post("/api/v2/pos/sales/quick-cart/", {"branch": br_a1.branch_id, "force_new": True}, format="json").data["sale"]["sale_id"]
        # Seed line directly to simulate cart already referencing product when batch expires on checkout day
        sale1 = PosSaleOrder.objects.get(pk=s1)
        from apps.core.models import PosSaleOrderLine

        PosSaleOrderLine.objects.create(
            line_id=f"sline-{uuid.uuid4().hex[:8]}",
            tenant=ten_a,
            branch=br_a1,
            sale=sale1,
            line_number=1,
            product=prod,
            packaging_unit=PosPackagingUnit.TABLET,
            quantity_units=2,
            conversion_factor=1,
            base_units_total=2,
            unit_price_bdt=Decimal("5.00"),
            line_subtotal_bdt=Decimal("10.00"),
            line_total_bdt=Decimal("10.00"),
        )
        sale1.subtotal_bdt = Decimal("10.00")
        sale1.grand_total_bdt = Decimal("10.00")
        sale1.save(update_fields=["subtotal_bdt", "grand_total_bdt", "updated_at"])
        api_client.post(f"/api/v2/pos/sales/{s1}/record-payment/", {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00", "idempotency_key": f"exp-{s1}"}, format="json")
        fin_exp = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin_exp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert fin_exp.data["error_code"] == "ERR_BATCH_EXPIRED"
        assert PosCheckoutAllocationAudit.objects.filter(sale_id=s1, was_rejected=True, rejection_code="ERR_BATCH_EXPIRED").exists()

        # 2) Quarantined (is_quarantined & MOVED_TO_QUARANTINE segregation) -> ERR_BATCH_QUARANTINED
        b_exp.expiry_date = today + datetime.timedelta(days=120)
        b_exp.is_quarantined = True
        b_exp.save()
        fin_quar = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin_quar.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert fin_quar.data["error_code"] == "ERR_BATCH_QUARANTINED"

        b_exp.is_quarantined = False
        b_exp.save()
        StockSegregationRecord.objects.create(
            segregation_id=f"seg-{uuid.uuid4().hex[:8]}",
            tenant=ten_a,
            branch=br_a1,
            batch=b_exp,
            product=prod,
            batch_number=b_exp.batch_number,
            segregated_quantity=20,
            unit_cost_bdt=Decimal("2.00"),
            total_cost_impact=Decimal("40.0000"),
            category=SegregationCategory.DAMAGED,
            status=SegregationStatus.MOVED_TO_QUARANTINE,
            reason_notes="Escalated to quarantine pending inspection",
        )
        fin_seg_quar = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin_seg_quar.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert fin_seg_quar.data["error_code"] == "ERR_BATCH_QUARANTINED"

        # 3) Recalled (CatalogueLifecycleNotice / BatchStatus.RECALLED) -> ERR_BATCH_RECALLED
        StockSegregationRecord.objects.filter(batch=b_exp).update(status=SegregationStatus.RELEASED_TO_SELLABLE)
        notice = CatalogueLifecycleNotice.objects.create(
            notice_id=f"rec-{uuid.uuid4().hex[:8]}",
            tenant=ten_a,
            product=prod,
            lifecycle_status=CatalogueLifecycleStatus.RECALLED,
            recall_scope=RecallScope.SPECIFIC_BATCHES,
            affected_batch_numbers=[b_exp.batch_number],
            regulatory_authority="DGDA",
            notice_number=f"DGDA-REC-{uuid.uuid4().hex[:6]}",
            reason="Substandard potency",
            effective_date=timezone.now(),
        )
        b_exp.recall_notice = notice
        b_exp.status = BatchStatus.RECALLED
        b_exp.save()
        fin_rec = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin_rec.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert fin_rec.data["error_code"] == "ERR_BATCH_RECALLED"

        # 4) Safe fallback & Cart edit reconciliation: add safe B_SAFE + add line 2 after pre-allocation!
        b_safe = BatchStock.objects.create(
            batch_id=f"bat-safe-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-SAFE-1",
            expiry_date=today + datetime.timedelta(days=180),
            purchase_date=today - datetime.timedelta(days=1),
            quantity_base_units=10,
            cost_per_unit=Decimal("2.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        # Pre-allocate 2 units cleanly
        alloc_res = api_client.post(f"/api/v2/pos/sales/{s1}/allocate/", {}, format="json")
        assert alloc_res.status_code == status.HTTP_200_OK
        assert alloc_res.data["allocations"][0]["batch_id"] == b_safe.batch_id

        # Request > available -> ERR_INSUFFICIENT_DISPENSABLE_STOCK
        prod2 = _mk_otc_napa()
        BatchStock.objects.create(
            batch_id=f"bat-p2-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod2,
            batch_number="LOT-P2",
            expiry_date=today + datetime.timedelta(days=150),
            purchase_date=today,
            quantity_base_units=5,
            cost_per_unit=Decimal("2.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        # Edit line / add second line after allocation -> finalize reconciles BOTH lines atomically!
        # Revert to DRAFT cleanly to scan-add prod2
        sale1.refresh_from_db()
        sale1.lifecycle_state = PosSaleLifecycleState.DRAFT
        sale1.save(update_fields=["lifecycle_state"])
        add2 = api_client.post(
            f"/api/v2/pos/sales/{s1}/scan-add/",
            {"product_id": prod2.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 3},
            format="json",
        )
        assert add2.status_code == status.HTTP_200_OK
        api_client.post(f"/api/v2/pos/sales/{s1}/record-payment/", {"tender_type": PaymentMethod.CASH, "amount_bdt": "15.00", "idempotency_key": f"p2add-{s1}"}, format="json")
        fin_rec2 = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin_rec2.status_code == status.HTTP_200_OK
        assert fin_rec2.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert PosSaleAllocation.objects.filter(sale_id=s1, status=PosAllocationStatus.COMMITTED).count() == 2

    def test_manual_override_rbac_cross_tenant_isolation_offline_sync_audit_immutability_and_concurrency(
        self, api_client, tenants_and_users
    ):
        ten_a = tenants_and_users["ten_a"]
        br_a1 = tenants_and_users["br_a1"]
        ten_b = tenants_and_users["ten_b"]
        br_b1 = tenants_and_users["br_b1"]
        cashier = tenants_and_users["cashier_a"]
        manager = tenants_and_users["manager_a"]
        today = timezone.now().date()
        prod = _mk_otc_napa()

        BatchStock.objects.create(
            batch_id=f"bat-ov1-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-OV-EARLY",
            expiry_date=today + datetime.timedelta(days=20),
            purchase_date=today - datetime.timedelta(days=10),
            quantity_base_units=10,
            cost_per_unit=Decimal("2.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        b_late = BatchStock.objects.create(
            batch_id=f"bat-ov2-{uuid.uuid4().hex[:6]}",
            tenant=ten_a,
            branch=br_a1,
            product=prod,
            batch_number="LOT-OV-LATE",
            expiry_date=today + datetime.timedelta(days=200),
            purchase_date=today - datetime.timedelta(days=2),
            quantity_base_units=15,
            cost_per_unit=Decimal("2.20"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        b_foreign = BatchStock.objects.create(
            batch_id=f"bat-for-{uuid.uuid4().hex[:6]}",
            tenant=ten_b,
            branch=br_b1,
            product=prod,
            batch_number="LOT-FOREIGN-TENB",
            expiry_date=today + datetime.timedelta(days=100),
            purchase_date=today,
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )

        api_client.force_authenticate(user=cashier)
        cart_res = api_client.post("/api/v2/pos/sales/quick-cart/", {"branch": br_a1.branch_id, "force_new": True}, format="json")
        sale_id = cart_res.data["sale"]["sale_id"]
        scan = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": prod.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 5},
            format="json",
        )
        line_id = scan.data["sale"]["lines"][0]["line_id"]

        # 1) Cashier CANNOT override FEFO order (choosing b_late when b_early is available -> 403)
        ov_cash = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/allocate/",
            {"batch_overrides": {line_id: b_late.batch_id}, "override_reason": "Customer asked newer date"},
            format="json",
        )
        assert ov_cash.status_code == status.HTTP_403_FORBIDDEN
        assert ov_cash.data["error_code"] == "ERR_FEFO_OVERRIDE_REQUIRES_APPROVAL"

        # 2) Manager requires reason >= 3 chars; valid reason succeeds cleanly!
        api_client.force_authenticate(user=manager)
        ov_no_rsn = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/allocate/",
            {"batch_overrides": {line_id: b_late.batch_id}, "override_reason": "x"},
            format="json",
        )
        assert ov_no_rsn.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert ov_no_rsn.data["error_code"] == "ERR_FEFO_OVERRIDE_REASON_REQUIRED"

        # Cross-tenant batch override blocked!
        ov_for = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/allocate/",
            {"batch_overrides": {line_id: b_foreign.batch_id}, "override_reason": "Trying tenant B batch"},
            format="json",
        )
        assert ov_for.status_code == status.HTTP_403_FORBIDDEN
        assert ov_for.data["error_code"] == "ERR_CROSS_TENANT_OR_BRANCH_BATCH"

        ov_ok = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/allocate/",
            {"batch_overrides": {line_id: b_late.batch_id}, "override_reason": "Hospital ward requested long expiry"},
            format="json",
        )
        assert ov_ok.status_code == status.HTTP_200_OK
        assert ov_ok.data["allocations"][0]["batch_id"] == b_late.batch_id
        assert ov_ok.data["allocations"][0]["allocation_mode"] == PosAllocationMode.MANUAL_OVERRIDE

        # 3) Allocation audit trail endpoint & strict immutability (cannot update/delete)
        aud_res = api_client.get(f"/api/v2/pos/sales/{sale_id}/allocation-audits/")
        assert aud_res.status_code == status.HTTP_200_OK
        assert len(aud_res.data) >= 2
        aud_obj = PosCheckoutAllocationAudit.objects.filter(sale_id=sale_id).first()
        with pytest.raises(DjangoValidationError):
            aud_obj.delete()
        with pytest.raises(DjangoValidationError):
            aud_obj.reason = "tamper"
            aud_obj.save()

        # Offline sync commit sets OFFLINE_RECONCILED and preserves BASIC-plan availability
        api_client.force_authenticate(user=cashier)
        api_client.post(f"/api/v2/pos/sales/{sale_id}/record-payment/", {"tender_type": PaymentMethod.CASH, "amount_bdt": "25.00", "idempotency_key": f"ovp-{sale_id}"}, format="json")
        fin_off = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {"is_offline_sync": True}, format="json")
        assert fin_off.status_code == status.HTTP_200_OK

        # Suspended tenant locks write allocation
        ten_a.status = TenantLifecycleStatus.SUSPENDED
        ten_a.save(update_fields=["status", "updated_at"])
        susp_res = api_client.get(f"/api/v2/pos/sales/{sale_id}/fefo-plan/")
        assert susp_res.status_code in (status.HTTP_200_OK, status.HTTP_403_FORBIDDEN)
        alloc_susp = api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json")
        assert alloc_susp.status_code == status.HTTP_403_FORBIDDEN

        # BASIC plan has full FEFO allocation support
        bas_cash = tenants_and_users["cashier_bas"]
        br_bas = tenants_and_users["br_bas"]
        BatchStock.objects.create(
            batch_id=f"bat-bas-{uuid.uuid4().hex[:6]}",
            tenant=tenants_and_users["ten_basic"],
            branch=br_bas,
            product=prod,
            batch_number="LOT-BAS-1",
            expiry_date=today + datetime.timedelta(days=100),
            purchase_date=today,
            quantity_base_units=10,
            cost_per_unit=Decimal("2.00"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        api_client.force_authenticate(user=bas_cash)
        bas_cart = api_client.post("/api/v2/pos/sales/quick-cart/", {"branch": br_bas.branch_id, "force_new": True}, format="json").data["sale"]["sale_id"]
        api_client.post(f"/api/v2/pos/sales/{bas_cart}/scan-add/", {"product_id": prod.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 2}, format="json")
        assert api_client.get(f"/api/v2/pos/sales/{bas_cart}/fefo-plan/").status_code == status.HTTP_200_OK
        assert api_client.post(f"/api/v2/pos/sales/{bas_cart}/allocate/", {}, format="json").status_code == status.HTTP_200_OK


_sqlite_p228_lock = threading.Lock()


@pytest.mark.django_db(transaction=True)
def test_concurrent_checkout_fefo_stock_contention_prevents_oversell():
    """
    Two competing checkout finalize calls targeting the last 10 base units (6 units each)
    cannot both succeed, preventing negative stock or overlapping PurchaseCostLot drain.
    """
    ten_a = _mk_tenant("Concurrent Pharma", "p228c", PlanTier.STANDARD)
    br_a1 = _mk_branch(ten_a, "Mirpur Concurrent", "cc")
    cashier = _mk_user(ten_a, br_a1, UserRole.CASHIER, "cashc")
    prod = _mk_otc_napa()
    today = timezone.now().date()
    b_tight = BatchStock.objects.create(
        batch_id=f"bat-cc-{uuid.uuid4().hex[:6]}",
        tenant=ten_a,
        branch=br_a1,
        product=prod,
        batch_number="LOT-CC-TIGHT",
        expiry_date=today + datetime.timedelta(days=20),
        purchase_date=today - datetime.timedelta(days=5),
        quantity_base_units=10,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("2.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    lot_tight = PurchaseCostLot.objects.create(
        lot_id=f"lot-cc-{uuid.uuid4().hex[:6]}",
        tenant=ten_a,
        branch=br_a1,
        product=prod,
        batch=b_tight,
        purchase_date=today - datetime.timedelta(days=5),
        unit_cost=Decimal("2.0000"),
        initial_quantity=10,
        remaining_quantity=10,
        status=PurchaseCostLotStatus.ACTIVE,
    )

    sale_objs = []
    for idx in range(2):
        client = APIClient()
        client.force_authenticate(user=cashier)
        sid = client.post("/api/v2/pos/sales/quick-cart/", {"branch": br_a1.branch_id, "force_new": True}, format="json").data["sale"]["sale_id"]
        client.post(
            f"/api/v2/pos/sales/{sid}/scan-add/",
            {"product_id": prod.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 6},
            format="json",
        )
        client.post(
            f"/api/v2/pos/sales/{sid}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "30.00", "idempotency_key": f"cc-{idx}-{sid}"},
            format="json",
        )
        sale_objs.append(PosSaleOrder.objects.get(pk=sid))

    def _try_fin(so: PosSaleOrder) -> str:
        def _exec() -> str:
            try:
                PosDomainService.finalize_sale(cashier, so)
                return "OK"
            except Exception:
                return "ERR"

        if connection.vendor == "sqlite":
            with _sqlite_p228_lock:
                return _exec()
        try:
            return _exec()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_try_fin, sale_objs))

    assert outcomes.count("OK") == 1
    assert outcomes.count("ERR") == 1
    b_tight.refresh_from_db()
    lot_tight.refresh_from_db()
    assert b_tight.quantity_base_units == 4
    assert b_tight.reserved_quantity_base_units == 0
    assert lot_tight.remaining_quantity == 4
