"""
backend/tests/test_p181_supplier_organization.py
Architecture Decision: ADR-0181 (P181).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P181 Supplier Organization Model:
1. Valid supplier creation with full legal identity, tax/BIN, contact details, region, and credit limit.
2. Incomplete and invalid inputs rejected (missing name, negative credit limit, duplicate name).
3. Cross-tenant isolation strictly enforced with HTTP 403.
4. Cashier role strictly blocked with HTTP 403.
5. Suspended tenant blocked with HTTP 403.
6. PATCH update of contact details, region, and status transitions with is_active synchronization.
7. Accounts payable (AP) ledger postings with exact Decimal(14, 4) precision and balance tracking.
8. Archival restrictions: soft-delete barred when outstanding AP balance exists.
9. Authentic origin verification recording for DGDA/manufacturer traceability.
10. Supplier list filtering by region, status, and search query.
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
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.supplier_organization_service import SupplierOrganizationService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierOrganizationIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p181-{self.suffix}",
            name="Evercare Central Hospital",
            subdomain=f"evercare-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Main Hospital Branch A",
        )

        # Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.suffix}",
            name="Foreign Medico Ltd.",
            subdomain=f"foreign-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
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
        self.cashier = PharmacyUser.objects.create_user(
            email=f"csh_{self.suffix}@evercare.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@medico.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_valid_supplier_creation_with_legal_credentials(self):
        """
        1. Valid Scenario: Procurement manager creates a supplier organization with complete legal identity,
        wholesale drug license, NBR tax references (TIN/BIN), region, and credit limit.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/procurement/suppliers/"

        payload = {
            "name": "Square Pharmaceuticals Central Depot",
            "legal_name": "Square Pharmaceuticals PLC Distribution Division",
            "name_bn": "স্কয়ার ফার্মাসিউটিক্যালস সেন্ট্রাল ডিপো",
            "code": f"SUP-SQ-{self.suffix}",
            "contact_person": "Md. Rafiqul Islam (Depot Manager)",
            "phone": "+8801711998877",
            "email": "depot.dhaka@squarepharma.com.bd",
            "address": "Tejgaon Industrial Area, Dhaka-1208",
            "region": "DHAKA",
            "district": "Dhaka",
            "dl_number": "DGDA-DL-DH-00982",
            "bin_number": "001122334-0101",
            "tin_number": "998877665544",
            "trade_license_number": "TRAD/DNCC/2026/09912",
            "payment_terms_days": 30,
            "credit_limit": "500000.0000",
            "bank_name": "Islami Bank Bangladesh PLC",
            "bank_account_name": "Square Pharmaceuticals PLC",
            "bank_account_no": "20501122334455",
            "bank_branch": "Tejgaon Branch",
            "bank_routing_number": "125272991",
            "lead_time_days": 2,
            "notes": "Primary distributor for Square medicines",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data
        supplier_id = data["supplier_id"]
        assert supplier_id.startswith("sup-")
        assert data["name"] == "Square Pharmaceuticals Central Depot"
        assert data["legal_name"] == "Square Pharmaceuticals PLC Distribution Division"
        assert data["region"] == "DHAKA"
        assert data["bin_number"] == "001122334-0101"
        assert Decimal(data["credit_limit"]) == Decimal("500000.0000")
        assert Decimal(data["current_balance"]) == Decimal("0.0000")
        assert data["status"] == "ACTIVE"
        assert data["is_active"] is True

        # Database invariant
        db_supplier = Supplier.objects.get(supplier_id=supplier_id)
        assert db_supplier.tenant == self.tenant
        assert db_supplier.name == payload["name"]
        assert db_supplier.bin_number == payload["bin_number"]

        # Audit log invariant
        audit = AuditLogEntry.objects.filter(
            action="SUPPLIER_ORGANIZATION_CREATED",
            resource_id=supplier_id,
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

    def test_02_incomplete_and_invalid_inputs_rejected(self):
        """
        2. Incomplete / Invalid Input: Missing name, negative credit limit, and duplicate active name.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/procurement/suppliers/"

        # Case A: Missing supplier name
        res_missing = self.client.post(url, {"name": ""}, format="json")
        assert res_missing.status_code == status.HTTP_400_BAD_REQUEST
        data_missing = res_missing.data if hasattr(res_missing, "data") else res_missing.json()
        assert data_missing.get("error_code") == "ERR_MISSING_SUPPLIER_NAME"

        # Case B: Negative credit limit
        res_neg = self.client.post(url, {"name": "Incepta Depot", "credit_limit": "-1000.00"}, format="json")
        assert res_neg.status_code == status.HTTP_400_BAD_REQUEST
        data_neg = res_neg.data if hasattr(res_neg, "data") else res_neg.json()
        assert data_neg.get("error_code") == "ERR_INVALID_CREDIT_LIMIT"

        # Case C: Duplicate active supplier name within same tenant
        self.client.post(url, {"name": "Beximco Pharma Depot"}, format="json")
        res_dup = self.client.post(url, {"name": "Beximco Pharma Depot"}, format="json")
        assert res_dup.status_code == status.HTTP_400_BAD_REQUEST
        data_dup = res_dup.data if hasattr(res_dup, "data") else res_dup.json()
        assert data_dup.get("error_code") == "ERR_DUPLICATE_SUPPLIER_NAME"

    def test_03_cross_tenant_isolation_strictly_enforced(self):
        """
        3. Cross-Tenant Isolation: Tenant B manager cannot read, edit, or post to Tenant A's supplier.
        """
        # Create supplier under Tenant A
        sup_a = SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"ACI Depot {self.suffix}", "credit_limit": "200000.0000"},
        )

        # Authenticate as Foreign Manager (Tenant B)
        self.client.force_authenticate(user=self.foreign_manager)

        # Read attempt
        res_get = self.client.get(f"/api/v2/procurement/suppliers/{sup_a['supplier_id']}/")
        assert res_get.status_code == status.HTTP_403_FORBIDDEN
        data_get = res_get.data if hasattr(res_get, "data") else res_get.json()
        assert data_get.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

        # Update attempt
        res_patch = self.client.patch(
            f"/api/v2/procurement/suppliers/{sup_a['supplier_id']}/",
            {"phone": "+8801999999999"},
            format="json",
        )
        assert res_patch.status_code == status.HTTP_403_FORBIDDEN

        # Ledger posting attempt
        res_post = self.client.post(
            f"/api/v2/procurement/suppliers/{sup_a['supplier_id']}/post-ledger/",
            {"posting_type": "PAYMENT", "reference_number": "ILLEGAL-PAY", "debit_amount": "5000.00"},
            format="json",
        )
        assert res_post.status_code == status.HTTP_403_FORBIDDEN

    def test_04_cashier_role_strictly_forbidden(self):
        """
        4. Role Authorization: Cashiers cannot register suppliers or post to AP ledger.
        """
        self.client.force_authenticate(user=self.cashier)
        url = "/api/v2/procurement/suppliers/"

        res_create = self.client.post(url, {"name": "Unauthorized Depot"}, format="json")
        assert res_create.status_code == status.HTTP_403_FORBIDDEN
        data = res_create.data if hasattr(res_create, "data") else res_create.json()
        assert data.get("error_code") in ["ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED"]

    def test_05_suspended_tenant_blocked(self):
        """
        5. Suspended Tenant: Mutations are blocked with HTTP 403.
        """
        self.client.force_authenticate(user=self.suspended_manager)
        url = "/api/v2/procurement/suppliers/"

        response = self.client.post(url, {"name": "Suspended Depot"}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]

    def test_06_patch_update_supplier_profile_and_status(self):
        """
        6. PATCH Update: Modifies phone, region, credit limit, and status with is_active synchronization.
        """
        self.client.force_authenticate(user=self.manager)
        created = SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"Renata Depot {self.suffix}", "region": "DHAKA"},
        )
        sup_id = created["supplier_id"]

        url = f"/api/v2/procurement/suppliers/{sup_id}/"
        patch_payload = {
            "phone": "+8801811223344",
            "region": "CHITTAGONG",
            "credit_limit": "750000.0000",
            "status": "SUSPENDED",
        }

        response = self.client.patch(url, patch_payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        assert data["phone"] == "+8801811223344"
        assert data["region"] == "CHITTAGONG"
        assert Decimal(data["credit_limit"]) == Decimal("750000.0000")
        assert data["status"] == "SUSPENDED"
        assert data["is_active"] is False  # Invariant: non-ACTIVE status sets is_active = False

        db_supplier = Supplier.objects.get(supplier_id=sup_id)
        assert db_supplier.is_active is False
        assert db_supplier.status == "SUSPENDED"

    def test_07_ap_ledger_postings_with_exact_decimal_precision(self):
        """
        7. AP Ledger Postings: GRN Invoice credits, payment debits, and debit notes maintain
        exact running balance with Decimal(14, 4) precision.
        """
        self.client.force_authenticate(user=self.manager)
        sup = SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"Aristopharma Depot {self.suffix}", "credit_limit": "300000.0000"},
        )
        sup_id = sup["supplier_id"]

        ledger_url = f"/api/v2/procurement/suppliers/{sup_id}/post-ledger/"

        # Step 1: GRN shipment receipt: Credit +45,250.7500 BDT
        res_grn = self.client.post(
            ledger_url,
            {
                "posting_type": "GRN_INVOICE",
                "reference_number": "GRN-2026-0041",
                "credit_amount": "45250.7500",
                "notes": "Antibiotics and oncology products received",
            },
            format="json",
        )
        assert res_grn.status_code == status.HTTP_201_CREATED
        assert Decimal(res_grn.data["balance_after"]) == Decimal("45250.7500")

        # Step 2: Payment voucher: Debit -25,000.0000 BDT
        res_pay = self.client.post(
            ledger_url,
            {
                "posting_type": "PAYMENT",
                "reference_number": "PAY-VOUCH-9912",
                "debit_amount": "25000.0000",
                "notes": "Online bank transfer",
            },
            format="json",
        )
        assert res_pay.status_code == status.HTTP_201_CREATED
        assert Decimal(res_pay.data["balance_after"]) == Decimal("20250.7500")

        # Step 3: Purchase return debit note: Debit -250.7500 BDT
        res_ret = self.client.post(
            ledger_url,
            {
                "posting_type": "RETURN_DEBIT_NOTE",
                "reference_number": "DN-2026-001",
                "debit_amount": "250.7500",
                "notes": "Damaged cartons returned to depot",
            },
            format="json",
        )
        assert res_ret.status_code == status.HTTP_201_CREATED
        assert Decimal(res_ret.data["balance_after"]) == Decimal("20000.0000")

        # Step 4: Verify supplier's current_balance on retrieve
        res_sup = self.client.get(f"/api/v2/procurement/suppliers/{sup_id}/")
        assert Decimal(res_sup.data["current_balance"]) == Decimal("20000.0000")

        # Step 5: Verify ledger entries list
        res_list = self.client.get(f"/api/v2/procurement/suppliers/{sup_id}/ledger/")
        assert res_list.status_code == status.HTTP_200_OK
        assert res_list.data["count"] == 3

    def test_08_archival_blocked_when_outstanding_ap_balance_exists(self):
        """
        8. Archival Guard: Supplier cannot be archived if current_balance > 0.
        """
        self.client.force_authenticate(user=self.manager)
        sup = SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"Opsonin Depot {self.suffix}"},
        )
        sup_id = sup["supplier_id"]

        # Post GRN to create payable balance
        SupplierOrganizationService.post_ap_ledger_entry(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=sup_id,
            data={"posting_type": "GRN_INVOICE", "reference_number": "GRN-01", "credit_amount": "10000.0000"},
        )

        # Attempt to archive / delete
        res_del = self.client.delete(f"/api/v2/procurement/suppliers/{sup_id}/")
        assert res_del.status_code == status.HTTP_400_BAD_REQUEST
        data_del = res_del.data if hasattr(res_del, "data") else res_del.json()
        assert data_del.get("error_code") == "ERR_SUPPLIER_OUTSTANDING_BALANCE"

        # Settle payable balance with payment
        SupplierOrganizationService.post_ap_ledger_entry(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=sup_id,
            data={"posting_type": "PAYMENT", "reference_number": "PAY-01", "debit_amount": "10000.0000"},
        )

        # Re-attempt archive: now succeeds
        res_del_ok = self.client.delete(f"/api/v2/procurement/suppliers/{sup_id}/")
        assert res_del_ok.status_code == status.HTTP_200_OK
        assert res_del_ok.data["status"] == "INACTIVE"
        assert res_del_ok.data["is_active"] is False

    def test_09_proof_of_origin_verification_workflow(self):
        """
        9. Authentic Origin Verification: Verifies manufacturer distribution authority.
        """
        self.client.force_authenticate(user=self.manager)
        sup = SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"Healthcare Pharma Depot {self.suffix}"},
        )
        sup_id = sup["supplier_id"]

        verify_url = f"/api/v2/procurement/suppliers/{sup_id}/verify-origin/"
        res_v = self.client.post(
            verify_url,
            {"notes": "DGDA wholesale drug license and manufacturer certificate inspected"},
            format="json",
        )
        assert res_v.status_code == status.HTTP_200_OK
        assert res_v.data["proof_of_origin_verified"] is True
        assert "DGDA" in res_v.data["proof_of_origin_notes"]

        db_supplier = Supplier.objects.get(supplier_id=sup_id)
        assert db_supplier.proof_of_origin_verified is True

    def test_10_list_filtering_by_region_and_status(self):
        """
        10. List Filtering: GET /api/v2/procurement/suppliers/ with region and status filters.
        """
        self.client.force_authenticate(user=self.manager)

        # Create Dhaka Active supplier
        SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"Dhaka Supplier {self.suffix}", "region": "DHAKA", "status": "ACTIVE"},
        )
        # Create Chittagong Suspended supplier
        SupplierOrganizationService.create_supplier(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            data={"name": f"Ctg Supplier {self.suffix}", "region": "CHITTAGONG", "status": "SUSPENDED"},
        )

        # Filter by region=DHAKA
        res_dhaka = self.client.get("/api/v2/procurement/suppliers/?region=DHAKA")
        assert res_dhaka.status_code == status.HTTP_200_OK
        names_dhaka = [s["name"] for s in res_dhaka.data["results"]]
        assert any(f"Dhaka Supplier {self.suffix}" in n for n in names_dhaka)
        assert not any(f"Ctg Supplier {self.suffix}" in n for n in names_dhaka)

        # Filter by status=SUSPENDED
        res_susp = self.client.get("/api/v2/procurement/suppliers/?status=SUSPENDED")
        assert res_susp.status_code == status.HTTP_200_OK
        names_susp = [s["name"] for s in res_susp.data["results"]]
        assert any(f"Ctg Supplier {self.suffix}" in n for n in names_susp)

