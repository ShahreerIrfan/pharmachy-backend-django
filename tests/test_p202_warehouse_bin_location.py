"""
backend/tests/test_p202_warehouse_bin_location.py
Architecture Decision: ADR-0202 (P202).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Warehouse, Bin Locations, Cold-Chain Fridges,
Quarantine Segregation, Capacity Limits, and Branch Controlled-Location Access Rules.
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
    BatchLocationAssignment,
    BatchStatus,
    BatchStock,
    Branch,
    ControlledLocationAccessRule,
    MedicineProduct,
    StorageCondition,
    StorageLocation,
    StorageLocationType,
    Tenant,
    TenantLifecycleStatus,
    Warehouse,
    WarehouseType,
)
from apps.core.warehouse_location_service import WarehouseLocationService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Lakeside Pharmacy Banani",
        subdomain=f"lakeside-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Square Health Gulshan",
        subdomain=f"square-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Main Dispensary Banani",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Gulshan Branch 2",
    )


@pytest.fixture
def manager_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200010",
        email="manager@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def pharmacist_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200011",
        email="pharmacist@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200012",
        email="cashier@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700200013",
        email="manager@square.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def product_ambient():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Paracetamol 500mg",
        generic_name="Paracetamol",
        dosage_form="TABLET",
        cost_unit=Decimal("10.0000"),
        mrp_unit=Decimal("15.0000"),
    )


@pytest.fixture
def product_cold_chain():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Insulin Glargine 100IU/ml",
        generic_name="Insulin Glargine",
        dosage_form="INJECTION",
        cost_unit=Decimal("450.0000"),
        mrp_unit=Decimal("520.0000"),
    )


@pytest.fixture
def product_narcotic():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Morphine Sulfate 15mg",
        generic_name="Morphine Sulfate",
        dosage_form="TABLET",
        cost_unit=Decimal("80.0000"),
        mrp_unit=Decimal("110.0000"),
    )


@pytest.mark.django_db
class TestWarehouseLocationSuite:
    """End-to-End Test Suite for P202: Warehouse, Bins, Cold Chain & Access Rules."""

    def test_create_warehouse_and_storage_hierarchy(self, tenant_a, branch_a, manager_user_a):
        """Test 1: Valid creation of warehouse and hierarchy of Shelf, Bin, Fridge, and Vault."""
        wh = WarehouseLocationService.create_warehouse(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            code="WH-MAIN",
            name="Main Branch Backroom Dispensary",
            name_bn="মূল ডিসপেনসারি গুদাম",
            warehouse_type=WarehouseType.MAIN_DISPENSARY,
            is_cold_storage=True,
            actor_user=manager_user_a,
        )
        assert wh.code == "WH-MAIN"
        assert wh.is_active is True

        shelf = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="SH-A1",
            name="Shelf A - Row 1",
            name_bn="তাক এ - সারি ১",
            location_type=StorageLocationType.SHELF,
            capacity_pack_limit=200,
            actor_user=manager_user_a,
        )
        assert shelf.code == "SH-A1"
        assert shelf.capacity_pack_limit == 200

        bin_loc = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="BIN-A1-01",
            name="Bin A1-01 (Tablets)",
            name_bn="বিন এ১-০১ (ট্যাবলেট)",
            location_type=StorageLocationType.BIN,
            parent_location_id=shelf.location_id,
            capacity_pack_limit=50,
            actor_user=manager_user_a,
        )
        assert bin_loc.parent_location == shelf

        fridge = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="FRIDGE-01",
            name="Cold Chain Vaccine Refrigerator",
            name_bn="টিকা রেফ্রিজারেটর",
            location_type=StorageLocationType.FRIDGE,
            actor_user=manager_user_a,
        )
        assert fridge.temperature_monitored is True
        assert fridge.min_temp_celsius == Decimal("2.00")
        assert fridge.max_temp_celsius == Decimal("8.00")

    def test_invalid_input_and_capacity_validation(self, tenant_a, branch_a, manager_user_a):
        """Test 2: Rejection of negative capacity, min temp > max temp, and duplicate codes."""
        wh = WarehouseLocationService.create_warehouse(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            code="WH-SEC",
            name="Secondary Warehouse",
            actor_user=manager_user_a,
        )

        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.create_storage_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                warehouse_id=wh.warehouse_id,
                code="NEG-CAP",
                name="Bad Capacity",
                capacity_pack_limit=-10,
                actor_user=manager_user_a,
            )
        assert "Capacity pack limit cannot be negative" in str(exc.value)

        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.create_storage_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                warehouse_id=wh.warehouse_id,
                code="BAD-TEMP",
                name="Inverted Temp",
                min_temp_celsius=Decimal("15.00"),
                max_temp_celsius=Decimal("4.00"),
                actor_user=manager_user_a,
            )
        assert "Minimum temperature cannot exceed maximum temperature" in str(exc.value)

    def test_cold_chain_and_quarantine_segregation_guards(
        self, tenant_a, branch_a, manager_user_a, product_ambient, product_cold_chain
    ):
        """Test 3: Cold-chain medicines rejected from ambient shelves; quarantine batches segregated."""
        wh = WarehouseLocationService.create_warehouse(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            code="WH-DISP",
            name="Dispensary",
            actor_user=manager_user_a,
        )
        shelf = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="SH-AMB",
            name="Ambient Shelf",
            location_type=StorageLocationType.SHELF,
            actor_user=manager_user_a,
        )
        fridge = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="FRIDGE-COLD",
            name="Chiller Fridge",
            location_type=StorageLocationType.FRIDGE,
            actor_user=manager_user_a,
        )
        quar_zone = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="QUAR-Z1",
            name="Quarantine Bay 1",
            location_type=StorageLocationType.QUARANTINE_ZONE,
            actor_user=manager_user_a,
        )

        cold_batch = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_cold_chain,
            batch_number="BAT-INS-001",
            quantity_base_units=50,
            storage_condition=StorageCondition.COLD_CHAIN,
            cost_per_unit=Decimal("450.00"),
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        )

        # Rejection: Cold-chain batch into ambient shelf
        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_id=cold_batch.batch_id,
                location_id=shelf.location_id,
                quantity=10,
                actor_user=manager_user_a,
            )
        assert "Cold-chain products must be stored in a certified FRIDGE or FREEZER" in str(exc.value)

        # Success: Cold-chain batch into fridge
        assign_fridge = WarehouseLocationService.assign_batch_stock_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=cold_batch.batch_id,
            location_id=fridge.location_id,
            quantity=20,
            actor_user=manager_user_a,
        )
        assert assign_fridge.quantity == 20
        fridge.refresh_from_db()
        assert fridge.current_pack_count == 20

        # Quarantined batch must be placed in quarantine zone
        quar_batch = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_ambient,
            batch_number="BAT-RECALL-01",
            quantity_base_units=30,
            is_quarantined=True,
            status=BatchStatus.QUARANTINED,
            cost_per_unit=Decimal("10.00"),
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timezone.timedelta(days=100),
        )

        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_id=quar_batch.batch_id,
                location_id=shelf.location_id,
                quantity=10,
                actor_user=manager_user_a,
            )
        assert "Quarantined batches cannot be placed in active dispensing storage" in str(exc.value)

        # Quarantined batch into quarantine zone succeeds
        assign_quar = WarehouseLocationService.assign_batch_stock_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=quar_batch.batch_id,
            location_id=quar_zone.location_id,
            quantity=30,
            actor_user=manager_user_a,
        )
        assert assign_quar.quantity == 30

    def test_controlled_location_rbac_and_dual_auth(
        self, tenant_a, branch_a, manager_user_a, pharmacist_user_a, cashier_user_a, product_narcotic
    ):
        """Test 4: Controlled location rejects Cashiers and enforces dual authorization sign-off."""
        wh = WarehouseLocationService.create_warehouse(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            code="WH-SECURE",
            name="Secure Depot",
            actor_user=manager_user_a,
        )

        vault = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="VAULT-NARCOTIC",
            name="Narcotics Safe Lock",
            location_type=StorageLocationType.NARCOTICS_VAULT,
            is_controlled_access=True,
            allowed_roles=["STAFF_PHARMACIST", "BRANCH_MANAGER", "PHARMACY_OWNER"],
            requires_dual_authorization=True,
            actor_user=manager_user_a,
        )

        narco_batch = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_narcotic,
            batch_number="BAT-MOR-100",
            quantity_base_units=40,
            cost_per_unit=Decimal("80.00"),
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timezone.timedelta(days=200),
        )

        # Cashier role rejected
        with pytest.raises(PermissionDenied) as exc:
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_id=narco_batch.batch_id,
                location_id=vault.location_id,
                quantity=10,
                actor_user=cashier_user_a,
            )
        assert "Cashiers cannot allocate" in str(exc.value)

        # Pharmacist without supervisor sign-off rejected
        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_id=narco_batch.batch_id,
                location_id=vault.location_id,
                quantity=10,
                actor_user=pharmacist_user_a,
                supervisor_user=None,
            )
        assert "Controlled location requires dual authorization" in str(exc.value)

        # Self-signing rejected
        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_id=narco_batch.batch_id,
                location_id=vault.location_id,
                quantity=10,
                actor_user=manager_user_a,
                supervisor_user=manager_user_a,
            )
        assert "Dual authorization sign-off cannot be provided by the same actor" in str(exc.value)

        # Pharmacist + Manager dual sign-off succeeds!
        assign = WarehouseLocationService.assign_batch_stock_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=narco_batch.batch_id,
            location_id=vault.location_id,
            quantity=20,
            actor_user=pharmacist_user_a,
            supervisor_user=manager_user_a,
            notes="Schedule Narcotics Intake",
        )
        assert assign.quantity == 20

    def test_capacity_exceeded_and_stock_transfer(
        self, tenant_a, branch_a, manager_user_a, product_ambient
    ):
        """Test 5: Capacity limits prevent overcrowding; stock transfer moves units accurately."""
        wh = WarehouseLocationService.create_warehouse(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            code="WH-CAP",
            name="Capacity Test Warehouse",
            actor_user=manager_user_a,
        )

        bin_small = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="BIN-SMALL",
            name="Small Bin (Cap 15)",
            capacity_pack_limit=15,
            actor_user=manager_user_a,
        )
        bin_large = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh.warehouse_id,
            code="BIN-LARGE",
            name="Large Bin (Cap 100)",
            capacity_pack_limit=100,
            actor_user=manager_user_a,
        )

        batch = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_ambient,
            batch_number="BAT-PARA-CAP",
            quantity_base_units=50,
            cost_per_unit=Decimal("10.00"),
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timezone.timedelta(days=300),
        )

        # Assign 10 to bin_small
        WarehouseLocationService.assign_batch_stock_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch.batch_id,
            location_id=bin_small.location_id,
            quantity=10,
            actor_user=manager_user_a,
        )
        bin_small.refresh_from_db()
        assert bin_small.current_pack_count == 10

        # Attempting to assign 10 more exceeds limit 15 (10 + 10 = 20 > 15)
        with pytest.raises(ValidationError) as exc:
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_id=batch.batch_id,
                location_id=bin_small.location_id,
                quantity=10,
                actor_user=manager_user_a,
            )
        assert "Storage location capacity exceeded" in str(exc.value)

        # Transfer 6 units from bin_small to bin_large
        move_res = WarehouseLocationService.transfer_batch_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch.batch_id,
            source_location_id=bin_small.location_id,
            target_location_id=bin_large.location_id,
            quantity=6,
            actor_user=manager_user_a,
        )
        assert move_res["transferred_quantity"] == 6
        bin_small.refresh_from_db()
        bin_large.refresh_from_db()
        assert bin_small.current_pack_count == 4
        assert bin_large.current_pack_count == 6

    def test_cross_tenant_isolation(
        self, tenant_a, tenant_b, branch_a, branch_b, manager_user_a, manager_user_b, product_ambient
    ):
        """Test 6: Tenant B manager cannot view or assign stock to Tenant A locations."""
        wh_a = WarehouseLocationService.create_warehouse(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            code="WH-ISO-A",
            name="Tenant A Warehouse",
            actor_user=manager_user_a,
        )
        loc_a = WarehouseLocationService.create_storage_location(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            warehouse_id=wh_a.warehouse_id,
            code="LOC-ISO-A",
            name="Shelf A",
            actor_user=manager_user_a,
        )
        batch_a = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_ambient,
            batch_number="BAT-ISO-A",
            quantity_base_units=20,
            cost_per_unit=Decimal("10.00"),
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timezone.timedelta(days=150),
        )

        with pytest.raises(NotFound):
            WarehouseLocationService.assign_batch_stock_location(
                tenant_id=tenant_b.tenant_id,
                branch_id=branch_b.branch_id,
                batch_id=batch_a.batch_id,
                location_id=loc_a.location_id,
                quantity=5,
                actor_user=manager_user_b,
            )

    def test_drf_api_endpoints(self, tenant_a, branch_a, manager_user_a, product_ambient):
        """Test 7: REST API CRUD, batch assignment, and assigned batches query."""
        client = APIClient()
        client.force_authenticate(user=manager_user_a)

        # 1. Create Warehouse via API
        resp = client.post(
            "/api/v2/inventory/warehouses/",
            {
                "branch": branch_a.branch_id,
                "name": "API Warehouse",
                "name_bn": "এপিআই গুদাম",
                "code": "WH-API",
                "warehouse_type": WarehouseType.MAIN_DISPENSARY,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        wh_id = resp.data["warehouse_id"]

        # 2. Create Storage Location via API
        resp_loc = client.post(
            "/api/v2/inventory/locations/",
            {
                "branch": branch_a.branch_id,
                "warehouse": wh_id,
                "name": "API Shelf 1",
                "name_bn": "এপিআই তাক ১",
                "code": "API-SH1",
                "location_type": StorageLocationType.SHELF,
                "capacity_pack_limit": 100,
            },
            format="json",
        )
        assert resp_loc.status_code == status.HTTP_201_CREATED
        loc_id = resp_loc.data["location_id"]

        # 3. Create Batch and assign via action
        batch = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_ambient,
            batch_number="BAT-API-01",
            quantity_base_units=50,
            cost_per_unit=Decimal("10.00"),
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timezone.timedelta(days=100),
        )

        resp_assign = client.post(
            f"/api/v2/inventory/locations/{loc_id}/assign-batch/",
            {
                "batch_id": batch.batch_id,
                "quantity": 15,
                "notes": "Assigned via REST API",
            },
            format="json",
        )
        assert resp_assign.status_code == status.HTTP_201_CREATED
        assert resp_assign.data["quantity"] == 15

        # 4. Query assigned batches
        resp_list = client.get(f"/api/v2/inventory/locations/{loc_id}/assigned-batches/")
        assert resp_list.status_code == status.HTTP_200_OK
        assert len(resp_list.data) == 1
        assert resp_list.data[0]["batch_number"] == "BAT-API-01"

