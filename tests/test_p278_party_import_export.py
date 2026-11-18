"""
backend/tests/test_p278_party_import_export.py
Architecture Decision: ADR-0278 (P278).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Party (Customer & Supplier) Import/Export:
1. Customer export with privacy masking for non-owners.
2. Owner customer export with unmasked sensitive fields.
3. Supplier export with bank account masking.
4. Import preview detecting schema errors and duplicates.
5. Import commit with SKIP resolution.
6. Import commit with UPDATE resolution.
7. Supplier import preview and commit.
8. RBAC enforcement: Cashier and staff pharmacist strictly denied (ERR_STAFF_ROLE_DENIED, 403).
9. Cross-tenant isolation strictly enforced.
10. Suspended tenant account write protection.
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
    PosCustomer,
    Supplier,
    SupplierRegion,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPartyImportExportIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )

        # 2. Foreign Tenant & Branch
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Foreign Health {self.suffix}",
            subdomain=f"for-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Foreign Branch",
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 4. Existing Customers for Tenant A
        self.phone_1 = f"+880171{random.randint(1000000, 9999999)}"
        self.customer_1 = PosCustomer.objects.create(
            customer_id=f"cust-1-{self.suffix}",
            tenant=self.tenant_a,
            primary_branch=self.branch_a,
            full_name="Rafiqul Islam",
            phone=self.phone_1,
            email="rafiq@square.com",
            address="House 12, Road 4, Dhanmondi",
            credit_limit_bdt=Decimal("5000.00"),
            current_due_bdt=Decimal("1200.00"),
        )

        # 5. Existing Suppliers for Tenant A
        self.supplier_1 = Supplier.objects.create(
            supplier_id=f"sup-1-{self.suffix}",
            tenant=self.tenant_a,
            name="Beximco Pharma Depot",
            code=f"BEX-{self.suffix.upper()}",
            contact_person="Kabir Ahmed",
            phone=f"+880181{random.randint(1000000, 9999999)}",
            email="depot@beximco.com",
            bank_name="Dutch-Bangla Bank",
            bank_account_no="123456789012",
            status=SupplierStatus.ACTIVE,
        )

        # Foreign Customer for Tenant B
        self.foreign_customer = PosCustomer.objects.create(
            customer_id=f"cust-for-{self.suffix}",
            tenant=self.tenant_b,
            primary_branch=self.branch_b,
            full_name="Foreign Customer",
            phone="+8801999999999",
        )

        # 6. Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@foreign.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_customer_export_with_privacy_masking(self):
        """Manager exports customers and receives privacy-masked PII."""
        self.client.force_authenticate(user=self.manager)

        resp = self.client.post(
            "/api/v2/parties/export/",
            {"party_type": "CUSTOMER", "include_sensitive": False},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["privacy_masked"] is True
        assert data["count"] == 1
        c = data["parties"][0]
        assert "****" in c["phone"]
        assert "***@" in c["email"]
        assert c["address"] == "[REDACTED]"
        assert c["credit_limit_bdt"] == "[RESTRICTED]"

    def test_02_owner_customer_export_unmasked(self):
        """Owner exports customers with include_sensitive=True and receives unmasked records."""
        self.client.force_authenticate(user=self.owner)

        resp = self.client.post(
            "/api/v2/parties/export/",
            {"party_type": "CUSTOMER", "include_sensitive": True},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["privacy_masked"] is False
        c = data["parties"][0]
        assert c["phone"] == self.customer_1.phone
        assert c["email"] == "rafiq@square.com"
        assert c["address"] == "House 12, Road 4, Dhanmondi"
        assert c["credit_limit_bdt"] == "5000.00"

    def test_03_supplier_export_with_bank_masking(self):
        """Manager exports suppliers and sensitive bank accounts are masked."""
        self.client.force_authenticate(user=self.manager)

        resp = self.client.post(
            "/api/v2/parties/export/",
            {"party_type": "SUPPLIER"},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        s = data["parties"][0]
        assert s["bank_account_no"] == "****9012"

    def test_04_import_preview_detects_duplicates_and_invalids(self):
        """Import preview identifies valid records, duplicate phones, and schema errors."""
        self.client.force_authenticate(user=self.manager)

        batch = [
            {"full_name": "New Person", "phone": "01799881122", "email": "new@pharma.com"},
            {"full_name": "Duplicate Person", "phone": self.phone_1},
            {"full_name": "", "phone": "01755443322"},  # Missing name
        ]

        resp = self.client.post(
            "/api/v2/parties/import-preview/",
            {"party_type": "CUSTOMER", "records": batch},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        preview = resp.json()
        assert preview["total_records"] == 3
        assert preview["valid_count"] == 1
        assert preview["duplicate_count"] == 1
        assert preview["invalid_count"] == 1

        items = preview["preview_items"]
        assert items[0]["is_valid"] is True
        assert items[0]["is_duplicate"] is False
        assert items[1]["is_duplicate"] is True
        assert items[1]["duplicate_match_id"] == self.customer_1.customer_id
        assert items[2]["is_valid"] is False

    def test_05_import_commit_skip_resolution(self):
        """Import commit with SKIP resolution creates new records and skips duplicates."""
        self.client.force_authenticate(user=self.manager)

        batch = [
            {"full_name": "Sole New Customer", "phone": "01788776655", "email": "sole@pharma.com"},
            {"full_name": "Ignored Duplicate", "phone": self.phone_1},
        ]

        resp = self.client.post(
            "/api/v2/parties/import-commit/",
            {"party_type": "CUSTOMER", "records": batch, "duplicate_resolution": "SKIP"},
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["created_count"] == 1
        assert data["skipped_count"] == 1
        assert data["updated_count"] == 0

        # Verify new customer exists
        created_cust = PosCustomer.objects.filter(tenant=self.tenant_a, full_name="Sole New Customer").first()
        assert created_cust is not None

        # Verify duplicate customer was not modified
        self.customer_1.refresh_from_db()
        assert self.customer_1.full_name == "Rafiqul Islam"

    def test_06_import_commit_update_resolution(self):
        """Import commit with UPDATE resolution updates existing customer details while preserving ID."""
        self.client.force_authenticate(user=self.manager)

        batch = [
            {
                "full_name": "Rafiqul Islam Updated",
                "phone": self.phone_1,
                "email": "updated_email@square.com",
                "address": "Banani DOHS",
            }
        ]

        resp = self.client.post(
            "/api/v2/parties/import-commit/",
            {"party_type": "CUSTOMER", "records": batch, "duplicate_resolution": "UPDATE"},
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["updated_count"] == 1
        assert data["created_count"] == 0

        self.customer_1.refresh_from_db()
        assert self.customer_1.full_name == "Rafiqul Islam Updated"
        assert self.customer_1.email == "updated_email@square.com"
        assert self.customer_1.address == "Banani DOHS"

    def test_07_supplier_import_preview_and_commit(self):
        """Supplier batch import preview detects duplicate codes and commits new suppliers."""
        self.client.force_authenticate(user=self.manager)

        batch = [
            {"name": "Incepta Pharmaceuticals", "code": "INC-01", "phone": "01733221100"},
            {"name": "Duplicate Beximco", "code": self.supplier_1.code, "phone": "01744556677"},
        ]

        # Preview
        prev_resp = self.client.post(
            "/api/v2/parties/import-preview/",
            {"party_type": "SUPPLIER", "records": batch},
            format="json",
        )
        assert prev_resp.status_code == status.HTTP_200_OK
        prev_data = prev_resp.json()
        assert prev_data["valid_count"] == 1
        assert prev_data["duplicate_count"] == 1

        # Commit
        com_resp = self.client.post(
            "/api/v2/parties/import-commit/",
            {"party_type": "SUPPLIER", "records": batch, "duplicate_resolution": "SKIP"},
            format="json",
        )
        assert com_resp.status_code == status.HTTP_201_CREATED
        assert com_resp.json()["created_count"] == 1
        assert com_resp.json()["skipped_count"] == 1

    def test_08_cashier_and_pharmacist_denied_rbac(self):
        """Cashier and staff pharmacist are denied all import/export endpoints."""
        for user in [self.cashier, self.pharmacist]:
            self.client.force_authenticate(user=user)

            # Export denied
            assert self.client.post("/api/v2/parties/export/", {"party_type": "CUSTOMER"}).status_code == status.HTTP_403_FORBIDDEN

            # Preview denied
            assert self.client.post("/api/v2/parties/import-preview/", {"party_type": "CUSTOMER", "records": []}).status_code == status.HTTP_403_FORBIDDEN

            # Commit denied
            assert self.client.post("/api/v2/parties/import-commit/", {"party_type": "CUSTOMER", "records": []}).status_code == status.HTTP_403_FORBIDDEN

    def test_09_cross_tenant_isolation(self):
        """Foreign tenant user only sees their own tenant's data on export."""
        self.client.force_authenticate(user=self.foreign_user)

        resp = self.client.post(
            "/api/v2/parties/export/",
            {"party_type": "CUSTOMER", "include_sensitive": True},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["count"] == 1
        assert data["parties"][0]["customer_id"] == self.foreign_customer.customer_id

    def test_10_suspended_tenant_blocked(self):
        """Suspended tenant accounts are blocked from import/export operations."""
        self.client.force_authenticate(user=self.suspended_user)

        resp = self.client.post(
            "/api/v2/parties/export/",
            {"party_type": "CUSTOMER"},
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error_code"] in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
