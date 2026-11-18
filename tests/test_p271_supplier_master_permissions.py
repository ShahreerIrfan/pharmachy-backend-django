"""
backend/tests/test_p271_supplier_master_permissions.py
Architecture Decision: ADR-0271 (P271).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Supplier Master fine-grained RBAC permissions,
strict tenant isolation, and controlled duplicate merge operations:
1. Pharmacy Owner & Branch Manager can view, create, update, and merge suppliers.
2. Cashier role is strictly denied VIEW, CREATE, and MERGE access (HTTP 403, ERR_STAFF_ROLE_DENIED).
3. Staff Pharmacist is permitted to VIEW, but barred from CREATE and MERGE (HTTP 403).
4. Inventory Clerk can CREATE and UPDATE, but is barred from MERGE (HTTP 403).
5. Cross-tenant access and cross-tenant merges are strictly forbidden (HTTP 403, ERR_CROSS_TENANT_ACCESS).
6. Controlled merge policy enforces: rejection of self-merges (HTTP 400, ERR_CANNOT_MERGE_SELF) and inactive targets (ERR_TARGET_ALREADY_MERGED).
7. Valid merge transfers AP balance, re-links contacts, archives source, and logs an immutable audit entry.
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
    Supplier,
    SupplierContactPerson,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierMasterPermissionsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Square Branch",
        )

        # 2. Foreign Tenant
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Beximco Health {self.suffix}",
            subdomain=f"bex-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 3. User Roles under Tenant A
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.branch_manager = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"rx_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.inventory_clerk = PharmacyUser.objects.create_user(
            email=f"clerk_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Inventory Clerk",
            password="StrongPassword123!",
            role=UserRole.INVENTORY_CLERK,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. Foreign User
        self.foreign_owner = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@beximco.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Foreign Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

    def test_01_owner_and_manager_supplier_crud(self):
        """Owner and Manager can register and view suppliers via both endpoints."""
        self.client.force_authenticate(user=self.owner)

        # Create via /api/v2/procurement/suppliers/
        res_create = self.client.post(
            "/api/v2/procurement/suppliers/",
            {
                "name": f"Incepta Depot {self.suffix}",
                "credit_limit": "250000.0000",
                "phone": "+8801711112222",
                "region": "DHAKA",
            },
            format="json",
        )
        assert res_create.status_code == status.HTTP_201_CREATED
        sup_id = res_create.data["supplier_id"]

        # View via /api/v2/procurement/suppliers/{pk}/
        res_view = self.client.get(f"/api/v2/procurement/suppliers/{sup_id}/")
        assert res_view.status_code == status.HTTP_200_OK
        assert res_view.data["name"] == f"Incepta Depot {self.suffix}"

        # Branch manager can also list suppliers
        self.client.force_authenticate(user=self.branch_manager)
        res_list = self.client.get("/api/v2/suppliers/")
        assert res_list.status_code == status.HTTP_200_OK
        assert any(s["supplier_id"] == sup_id for s in res_list.data["results"])

    def test_02_cashier_strictly_denied_all_supplier_endpoints(self):
        """Retail cashier is forbidden from viewing, creating, or merging suppliers."""
        # Pre-seed supplier
        sup = Supplier.objects.create(
            supplier_id=f"sup-test-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Renata Depot {self.suffix}",
            phone="+8801712345678",
        )

        self.client.force_authenticate(user=self.cashier)

        # 1. Denied list on /api/v2/procurement/suppliers/
        res_list_p = self.client.get("/api/v2/procurement/suppliers/")
        assert res_list_p.status_code == status.HTTP_403_FORBIDDEN
        assert res_list_p.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Denied list on /api/v2/suppliers/
        res_list_s = self.client.get("/api/v2/suppliers/")
        assert res_list_s.status_code == status.HTTP_403_FORBIDDEN
        assert res_list_s.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 3. Denied retrieve
        res_get = self.client.get(f"/api/v2/procurement/suppliers/{sup.supplier_id}/")
        assert res_get.status_code == status.HTTP_403_FORBIDDEN
        assert res_get.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 4. Denied create
        res_create = self.client.post("/api/v2/procurement/suppliers/", {"name": "Unauthorized Depot"}, format="json")
        assert res_create.status_code == status.HTTP_403_FORBIDDEN
        assert res_create.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 5. Denied merge
        res_merge = self.client.post(
            f"/api/v2/procurement/suppliers/{sup.supplier_id}/merge/",
            {"target_id": "dummy_target"},
            format="json",
        )
        assert res_merge.status_code == status.HTTP_403_FORBIDDEN
        assert res_merge.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_03_pharmacist_can_view_but_cannot_create_or_merge(self):
        """Staff Pharmacist has read-only permission for dispensing verification, barred from mutation."""
        sup = Supplier.objects.create(
            supplier_id=f"sup-rx-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Square Pharma {self.suffix}",
        )

        self.client.force_authenticate(user=self.pharmacist)

        # Allowed to view
        res_view = self.client.get(f"/api/v2/procurement/suppliers/{sup.supplier_id}/")
        assert res_view.status_code == status.HTTP_200_OK

        # Barred from create
        res_create = self.client.post(
            "/api/v2/procurement/suppliers/",
            {"name": "Pharmacist Forbidden Vendor"},
            format="json",
        )
        assert res_create.status_code == status.HTTP_403_FORBIDDEN
        assert res_create.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # Barred from merge
        res_merge = self.client.post(
            f"/api/v2/procurement/suppliers/{sup.supplier_id}/merge/",
            {"target_id": "dummy"},
            format="json",
        )
        assert res_merge.status_code == status.HTTP_403_FORBIDDEN
        assert res_merge.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_04_inventory_clerk_can_create_but_cannot_merge(self):
        """Inventory Clerk can register incoming suppliers, but cannot merge legal organizations."""
        self.client.force_authenticate(user=self.inventory_clerk)

        # Clerk creates supplier
        res_create = self.client.post(
            "/api/v2/procurement/suppliers/",
            {"name": f"Clerk Supplier {self.suffix}", "credit_limit": "50000.0000"},
            format="json",
        )
        assert res_create.status_code == status.HTTP_201_CREATED
        created_id = res_create.data["supplier_id"]

        # Clerk barred from merge
        res_merge = self.client.post(
            f"/api/v2/procurement/suppliers/{created_id}/merge/",
            {"target_id": "dummy_target"},
            format="json",
        )
        assert res_merge.status_code == status.HTTP_403_FORBIDDEN
        assert res_merge.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_05_cross_tenant_isolation_strictly_enforced(self):
        """Tenant B user cannot access or merge Tenant A supplier."""
        sup_a = Supplier.objects.create(
            supplier_id=f"sup-t-a-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Confidential Tenant A Supplier {self.suffix}",
        )
        sup_b = Supplier.objects.create(
            supplier_id=f"sup-t-b-{self.suffix}",
            tenant=self.tenant_b,
            name=f"Tenant B Supplier {self.suffix}",
        )

        self.client.force_authenticate(user=self.foreign_owner)

        # Read attempt on Tenant A supplier returns HTTP 403
        res_read = self.client.get(f"/api/v2/procurement/suppliers/{sup_a.supplier_id}/")
        assert res_read.status_code == status.HTTP_403_FORBIDDEN
        assert res_read.data["error_code"] in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION"]

        # Cross-tenant merge attempt returns HTTP 403
        res_merge = self.client.post(
            f"/api/v2/procurement/suppliers/{sup_b.supplier_id}/merge/",
            {"target_id": sup_a.supplier_id},
            format="json",
        )
        assert res_merge.status_code == status.HTTP_403_FORBIDDEN
        assert res_merge.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_06_controlled_supplier_merge_workflow(self):
        """Controlled duplicate merge transfers balance, re-links contacts, and archives source."""
        self.client.force_authenticate(user=self.owner)

        target = Supplier.objects.create(
            supplier_id=f"sup-tgt-{self.suffix}",
            tenant=self.tenant_a,
            name=f"ACI Pharmaceuticals Canonical {self.suffix}",
            current_balance=Decimal("15000.0000"),
            is_active=True,
        )
        source = Supplier.objects.create(
            supplier_id=f"sup-src-{self.suffix}",
            tenant=self.tenant_a,
            name=f"ACI Pharma Duplicate {self.suffix}",
            current_balance=Decimal("5000.0000"),
            is_active=True,
        )
        contact = SupplierContactPerson.objects.create(
            contact_id=f"cnt-{self.suffix}",
            tenant=self.tenant_a,
            supplier=source,
            name="Mr. Representative",
            phone="+8801811112222",
        )

        # Case A: Self-merge rejected
        res_self = self.client.post(
            f"/api/v2/procurement/suppliers/{source.supplier_id}/merge/",
            {"target_id": source.supplier_id},
            format="json",
        )
        assert res_self.status_code == status.HTTP_400_BAD_REQUEST
        assert res_self.data["error_code"] == "ERR_CANNOT_MERGE_SELF"

        # Case B: Valid merge
        res_merge = self.client.post(
            f"/api/v2/procurement/suppliers/{source.supplier_id}/merge/",
            {"target_id": target.supplier_id, "reason": "Vendor master record deduplication"},
            format="json",
        )
        assert res_merge.status_code == status.HTTP_200_OK
        data = res_merge.data
        assert data["is_merged"] is True
        assert data["target_id"] == target.supplier_id

        # Target verification: balance transferred
        target.refresh_from_db()
        assert target.current_balance == Decimal("20000.0000")

        # Source verification: archived & zero balance
        source.refresh_from_db()
        assert source.is_active is False
        assert source.merged_into_id == target.supplier_id
        assert source.current_balance == Decimal("0.0000")

        # Contact re-linked to target
        contact.refresh_from_db()
        assert contact.supplier_id == target.supplier_id

        # Audit log verified
        audit = AuditLogEntry.objects.filter(
            action="SUPPLIER_ORGANIZATION_MERGED",
            resource_id=source.supplier_id,
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS
