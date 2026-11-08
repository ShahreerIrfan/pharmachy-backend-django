"""
backend/tests/test_p203_stock_inquiry.py
Architecture Decision: ADR-0203 (P203).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Tenant/Branch Authorized Stock Inquiry API:
1. Product & Batch Rollup: on-hand, available, reserved, quarantined, expired & near-expiry.
2. Mathematical stock conservation: available = on_hand - reserved (for sellable non-expired stock).
3. FEFO sorting (earliest expiry first).
4. Packaging breakdown calculation (Boxes, Strips, Pieces).
5. Decimal valuation precision (Cost & MRP).
6. Cashier branch isolation (cannot query other branches).
7. Cross-tenant isolation (Tenant B cannot view Tenant A stock).
8. Expiry status and cold chain filters.
9. Audit log generation & DRF REST API endpoints.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    StockInquiryAuditLog,
    StorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_inquiry_service import StockInquiryService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Lakeside Pharmacy Dhanmondi",
        subdomain=f"lakeside-{uid}",
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
def branch_a1(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Dhanmondi Main Branch",
    )


@pytest.fixture
def branch_a2(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Mirpur Branch 2",
    )


@pytest.fixture
def branch_b1(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Uttara Sector 7 Branch",
    )


@pytest.fixture
def manager_user_a(tenant_a, branch_a1):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700300001",
        email="manager@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a1.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a1(tenant_a, branch_a1):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700300002",
        email="cashier1@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a1.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b1):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700300003",
        email="manager@square.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b1.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def product_paracetamol():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Napa 500mg",
        brand_name_bn="নাপা ৫০০ মি.গ্রা.",
        generic_name="Paracetamol",
        generic_name_bn="প্যারাসিটামল",
        dosage_form="TABLET",
        strength="500mg",
        cost_unit=Decimal("1.2000"),
        mrp_unit=Decimal("1.5000"),
        barcode="89411001001",
    )


@pytest.fixture
def product_insulin():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Lantus Solostar 100IU/ml",
        brand_name_bn="ল্যান্টাস সলোস্টার",
        generic_name="Insulin Glargine",
        dosage_form="INJECTION",
        strength="100IU/ml",
        cost_unit=Decimal("820.0000"),
        mrp_unit=Decimal("950.0000"),
        requires_cold_chain=True,
    )


@pytest.mark.django_db
class TestStockInquirySuite:
    """Comprehensive Test Suite for P203: Stock inquiry API."""

    def test_stock_inquiry_full_rollup_and_fefo(
        self, tenant_a, branch_a1, manager_user_a, product_paracetamol
    ):
        """Test 1: Rollup calculation of available, reserved, quarantined, and expired stock."""
        today = timezone.now().date()

        # Batch 1: Active batch (100 units, 20 reserved -> 80 available, expires in 300 days)
        b1 = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-ACT-01",
            quantity_base_units=100,
            reserved_quantity_base_units=20,
            cost_per_unit=Decimal("1.2000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=300),
            storage_location="SH-A1",
        )

        # Batch 2: Near-expiry batch (40 units, 0 reserved, expires in 45 days)
        b2 = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-NEAR-02",
            quantity_base_units=40,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("1.2000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=45),
            storage_location="SH-A2",
        )

        # Batch 3: Expired batch (30 units, expired 10 days ago -> 0 available)
        b3 = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-EXP-03",
            quantity_base_units=30,
            reserved_quantity_base_units=0,
            status=BatchStatus.EXPIRED,
            cost_per_unit=Decimal("1.2000"),
            purchase_date=today,
            expiry_date=today - timezone.timedelta(days=10),
            storage_location="SH-DISP",
        )

        # Batch 4: Quarantined batch (25 units, is_quarantined=True -> 0 available)
        b4 = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-QUAR-04",
            quantity_base_units=25,
            reserved_quantity_base_units=0,
            is_quarantined=True,
            status=BatchStatus.QUARANTINED,
            cost_per_unit=Decimal("1.2000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=200),
            storage_location="QUAR-Z1",
        )

        res = StockInquiryService.query_stock(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a1.branch_id,
            product_id=product_paracetamol.product_id,
            actor_user=manager_user_a,
        )

        assert res["products_count"] == 1
        pdata = res["results"][0]

        # Invariants:
        # Total units = 100 + 40 + 30 + 25 = 195
        assert pdata["total_quantity_base_units"] == 195
        # Available units = (100 - 20) + 40 = 120
        assert pdata["available_quantity_base_units"] == 120
        assert pdata["reserved_quantity_base_units"] == 20
        assert pdata["expired_quantity_base_units"] == 30
        assert pdata["quarantined_quantity_base_units"] == 25
        assert pdata["near_expiry_quantity_base_units"] == 40

        # Packaging Summary: 120 tablets = 1 Box, 2 Strips
        pkg = pdata["packaging_summary"]
        assert pkg["boxes"] == 1
        assert pkg["strips"] == 2
        assert pkg["pieces"] == 0
        assert pkg["total_base_units"] == 120

        # FEFO Ordering check: Expired first, then Near-Expiry, then Active
        batches = pdata["batches"]
        assert len(batches) == 4
        assert batches[0]["batch_number"] == "BAT-EXP-03"
        assert batches[1]["batch_number"] == "BAT-NEAR-02"
        assert batches[2]["batch_number"] == "BAT-QUAR-04"
        assert batches[3]["batch_number"] == "BAT-ACT-01"

    def test_decimal_valuation_precision(
        self, tenant_a, branch_a1, manager_user_a, product_paracetamol
    ):
        """Test 2: Decimal cost and MRP valuation calculated with exact precision."""
        today = timezone.now().date()
        BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-VAL-01",
            quantity_base_units=150,
            cost_per_unit=Decimal("1.2500"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=180),
        )

        res = StockInquiryService.query_stock(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a1.branch_id,
            product_id=product_paracetamol.product_id,
            actor_user=manager_user_a,
        )
        pdata = res["results"][0]

        # 150 * 1.2500 = 187.5000
        assert Decimal(pdata["total_cost_valuation_bdt"]) == Decimal("187.5000")
        # 150 * 1.5000 = 225.0000
        assert Decimal(pdata["total_mrp_valuation_bdt"]) == Decimal("225.0000")

    def test_cashier_branch_access_restriction(
        self, tenant_a, branch_a1, branch_a2, cashier_user_a1, product_paracetamol
    ):
        """Test 3: Cashier assigned to Branch A1 is strictly denied from querying Branch A2."""
        with pytest.raises(PermissionDenied) as exc:
            StockInquiryService.query_stock(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a2.branch_id,
                product_id=product_paracetamol.product_id,
                actor_user=cashier_user_a1,
            )
        assert "Cashier is not authorized to query stock outside assigned branch" in str(exc.value)

    def test_cross_tenant_isolation(
        self, tenant_a, tenant_b, branch_a1, manager_user_b, product_paracetamol
    ):
        """Test 4: Tenant B manager cannot access Tenant A stock."""
        with pytest.raises(PermissionDenied) as exc:
            StockInquiryService.query_stock(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a1.branch_id,
                product_id=product_paracetamol.product_id,
                actor_user=manager_user_b,
            )
        assert "Cross-tenant stock access is strictly prohibited" in str(exc.value)

    def test_search_and_expiry_filters(
        self, tenant_a, branch_a1, manager_user_a, product_paracetamol, product_insulin
    ):
        """Test 5: Bangla keyword search and cold chain/expiry status filtering."""
        today = timezone.now().date()
        BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-NA-01",
            quantity_base_units=50,
            cost_per_unit=Decimal("1.20"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=120),
        )
        BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_insulin,
            batch_number="BAT-INS-COLD",
            quantity_base_units=10,
            cost_per_unit=Decimal("820.00"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=200),
            storage_condition=StorageCondition.COLD_CHAIN,
        )

        # 1. Search by Bengali brand name "নাপা"
        res_bn = StockInquiryService.query_stock(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a1.branch_id,
            search="নাপা",
            actor_user=manager_user_a,
        )
        assert res_bn["products_count"] == 1
        assert res_bn["results"][0]["brand_name"] == "Napa 500mg"

        # 2. Filter by storage_condition COLD_CHAIN
        res_cold = StockInquiryService.query_stock(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a1.branch_id,
            storage_condition=StorageCondition.COLD_CHAIN,
            actor_user=manager_user_a,
        )
        assert res_cold["products_count"] == 1
        assert res_cold["results"][0]["brand_name"] == "Lantus Solostar 100IU/ml"

    def test_audit_log_and_drf_api(
        self, tenant_a, branch_a1, manager_user_a, product_paracetamol
    ):
        """Test 6: Audit log creation and DRF viewset endpoints."""
        today = timezone.now().date()
        BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a1,
            product=product_paracetamol,
            batch_number="BAT-API-10",
            quantity_base_units=80,
            reserved_quantity_base_units=10,
            cost_per_unit=Decimal("1.2000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=150),
        )

        client = APIClient()
        client.force_authenticate(user=manager_user_a)

        # 1. Query stock via API
        resp = client.get(
            f"/api/v2/inventory/stock-inquiry/?branch={branch_a1.branch_id}&search=Napa"
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["products_count"] == 1
        assert resp.data["total_available_units"] == 70

        # 2. Verify audit log entry was created
        log_entry = StockInquiryAuditLog.objects.filter(
            tenant=tenant_a, branch=branch_a1
        ).first()
        assert log_entry is not None
        assert log_entry.search_query == "Napa"
        assert log_entry.total_available_units == 70

        # 3. Retrieve audit logs via API
        resp_logs = client.get(
            f"/api/v2/inventory/stock-inquiry/audit-logs/?branch={branch_a1.branch_id}"
        )
        assert resp_logs.status_code == status.HTTP_200_OK
        assert len(resp_logs.data) >= 1

