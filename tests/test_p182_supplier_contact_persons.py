"""
backend/tests/test_p182_supplier_contact_persons.py
Architecture Decision: ADR-0182 (P182).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P182 Supplier Contact Persons:
1. Valid supplier contact creation with functional role, preferred communication channel, and primary flag.
2. Atomic primary representative reassignment across multiple contacts for a supplier.
3. Incomplete and invalid inputs cleanly rejected (missing name, missing phone).
4. Cross-tenant isolation strictly enforced with HTTP 403.
5. Cashier role strictly blocked with HTTP 403.
6. Suspended tenant blocked with HTTP 403.
7. PATCH update of contact coordinates, role, and preferred channel.
8. Safe soft-archival of contact person with is_primary unsetting.
9. List filtering by role and active status with primary contact ordering.
10. Multiple contacts for diverse procurement roles (depot manager, sales rep, accounts, dispatch).
"""

from __future__ import annotations

import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    Supplier,
    SupplierContactPerson,
    SupplierContactPersonRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.supplier_organization_service import SupplierOrganizationService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierContactPersonsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p182-{self.suffix}",
            name="Evercare Procurement Central",
            subdomain=f"evercare-p182-{self.suffix}",
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
            subdomain=f"foreign-p182-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name="Suspended Pharma Ltd.",
            subdomain=f"susp-p182-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 2. Setup Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@evercare.com",
            phone_number=f"+880181{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"csh_{self.suffix}@evercare.com",
            phone_number=f"+880182{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@medico.com",
            phone_number=f"+880183{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880184{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 3. Setup Base Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-test-{self.suffix}",
            tenant=self.tenant,
            name=f"Square Pharma Central Depot {self.suffix}",
            legal_name="Square Pharmaceuticals PLC Distribution Division",
            region="DHAKA",
            status="ACTIVE",
            is_active=True,
        )

    def test_01_valid_supplier_contact_creation(self):
        """
        1. Valid Scenario: Procurement manager creates a contact person for a supplier with
        role, preferred communication channel (WhatsApp), and primary representative flag.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/"

        payload = {
            "name": "Md. Kamrul Hasan",
            "name_bn": "মোঃ কামরুল হাসান",
            "designation": "Senior Territory Executive (MIO)",
            "role": "SALES_REPRESENTATIVE",
            "phone": "+8801711223344",
            "alternate_phone": "+8801711223345",
            "email": "kamrul.hasan@squarepharma.com.bd",
            "preferred_communication_method": "WHATSAPP",
            "is_primary": True,
            "notes": "Available Saturday to Thursday for hospital emergency orders",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data
        contact_id = data["contact_id"]
        assert contact_id.startswith("sup-cnt-")
        assert data["name"] == "Md. Kamrul Hasan"
        assert data["role"] == "SALES_REPRESENTATIVE"
        assert data["preferred_communication_method"] == "WHATSAPP"
        assert data["is_primary"] is True
        assert data["is_active"] is True
        assert data["supplier_id"] == self.supplier.supplier_id

        # Database invariant
        db_contact = SupplierContactPerson.objects.get(contact_id=contact_id)
        assert db_contact.tenant == self.tenant
        assert db_contact.supplier == self.supplier
        assert db_contact.is_primary is True

        # Audit log invariant
        audit = AuditLogEntry.objects.filter(
            action="SUPPLIER_CONTACT_CREATED",
            resource_id=contact_id,
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

    def test_02_atomic_primary_contact_reassignment(self):
        """
        2. Primary Reassignment: Designating a new contact as primary atomically unsets
        prior contacts as primary for that supplier.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/"

        # Contact 1: Created as primary
        res1 = self.client.post(
            url,
            {"name": "Contact One", "phone": "+8801711111111", "role": "SALES_REPRESENTATIVE", "is_primary": True},
            format="json",
        )
        assert res1.status_code == status.HTTP_201_CREATED
        c1_id = res1.data["contact_id"]
        assert res1.data["is_primary"] is True

        # Contact 2: Created as primary -> Contact 1 must be unset
        res2 = self.client.post(
            url,
            {"name": "Contact Two", "phone": "+8801722222222", "role": "DEPOT_MANAGER", "is_primary": True},
            format="json",
        )
        assert res2.status_code == status.HTTP_201_CREATED
        c2_id = res2.data["contact_id"]
        assert res2.data["is_primary"] is True

        # Database check: c1 must now be False, c2 must be True
        c1_db = SupplierContactPerson.objects.get(contact_id=c1_id)
        c2_db = SupplierContactPerson.objects.get(contact_id=c2_id)
        assert c1_db.is_primary is False
        assert c2_db.is_primary is True

        # Reassign primary back to Contact 1 via PATCH
        patch_res = self.client.patch(
            f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/{c1_id}/",
            {"is_primary": True},
            format="json",
        )
        assert patch_res.status_code == status.HTTP_200_OK
        assert patch_res.data["is_primary"] is True

        c1_db.refresh_from_db()
        c2_db.refresh_from_db()
        assert c1_db.is_primary is True
        assert c2_db.is_primary is False

    def test_03_incomplete_and_invalid_inputs_rejected(self):
        """
        3. Invalid / Incomplete Input: Missing name or missing phone number cleanly rejected.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/"

        # Missing name (<2 chars)
        res_name = self.client.post(url, {"name": "A", "phone": "+8801711223344"}, format="json")
        assert res_name.status_code == status.HTTP_400_BAD_REQUEST
        data_name = res_name.data if hasattr(res_name, "data") else res_name.json()
        assert data_name.get("error_code") == "ERR_MISSING_CONTACT_NAME"

        # Missing phone
        res_phone = self.client.post(url, {"name": "Valid Name", "phone": ""}, format="json")
        assert res_phone.status_code == status.HTTP_400_BAD_REQUEST
        data_phone = res_phone.data if hasattr(res_phone, "data") else res_phone.json()
        assert data_phone.get("error_code") == "ERR_MISSING_CONTACT_PHONE"

    def test_04_cross_tenant_isolation_strictly_enforced(self):
        """
        4. Cross-Tenant Isolation: Foreign manager cannot view, edit, or archive Tenant A's contact person.
        """
        # Create contact under Tenant A
        contact = SupplierOrganizationService.create_supplier_contact(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=self.supplier.supplier_id,
            data={"name": "Confidential Rep", "phone": "+8801733333333"},
        )
        contact_id = contact["contact_id"]

        # Authenticate as Foreign Manager (Tenant B)
        self.client.force_authenticate(user=self.foreign_manager)

        # GET attempt
        res_get = self.client.get(f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/{contact_id}/")
        assert res_get.status_code == status.HTTP_403_FORBIDDEN
        data_get = res_get.data if hasattr(res_get, "data") else res_get.json()
        assert data_get.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

        # PATCH attempt
        res_patch = self.client.patch(
            f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/{contact_id}/",
            {"phone": "+8801999999999"},
            format="json",
        )
        assert res_patch.status_code == status.HTTP_403_FORBIDDEN

        # DELETE attempt
        res_del = self.client.delete(f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/{contact_id}/")
        assert res_del.status_code == status.HTTP_403_FORBIDDEN

    def test_05_cashier_role_strictly_forbidden(self):
        """
        5. Role RBAC: Cashiers cannot create or modify supplier contacts.
        """
        self.client.force_authenticate(user=self.cashier)
        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/"

        res_create = self.client.post(url, {"name": "Staff Contact", "phone": "01700000000"}, format="json")
        assert res_create.status_code == status.HTTP_403_FORBIDDEN
        data = res_create.data if hasattr(res_create, "data") else res_create.json()
        assert data.get("error_code") in ["ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED"]

    def test_06_suspended_tenant_blocked(self):
        """
        6. Suspended Tenant: Contact mutations barred with HTTP 403.
        """
        self.client.force_authenticate(user=self.suspended_manager)
        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/"

        response = self.client.post(url, {"name": "Suspended Staff", "phone": "01700000000"}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]

    def test_07_patch_update_contact_details(self):
        """
        7. PATCH Update: Modifies designation, functional role, and preferred communication channel.
        """
        self.client.force_authenticate(user=self.manager)
        created = SupplierOrganizationService.create_supplier_contact(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=self.supplier.supplier_id,
            data={"name": "Anisur Rahman", "phone": "+8801744444444", "role": "ORDER_BOOKER"},
        )
        cnt_id = created["contact_id"]

        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/{cnt_id}/"
        patch_payload = {
            "designation": "Head of Depot Commercial Operations",
            "role": "ACCOUNTS_EXECUTIVE",
            "preferred_communication_method": "EMAIL",
            "email": "anisur.accounts@squarepharma.com.bd",
            "notes": "Handles billing statements and credit reconciliation",
        }

        response = self.client.patch(url, patch_payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        assert data["designation"] == "Head of Depot Commercial Operations"
        assert data["role"] == "ACCOUNTS_EXECUTIVE"
        assert data["preferred_communication_method"] == "EMAIL"
        assert data["email"] == "anisur.accounts@squarepharma.com.bd"

        # Verify DB
        db_c = SupplierContactPerson.objects.get(contact_id=cnt_id)
        assert db_c.role == "ACCOUNTS_EXECUTIVE"
        assert db_c.preferred_communication_method == "EMAIL"

    def test_08_soft_archival_of_contact_person(self):
        """
        8. Soft-Archival: Deleting a contact marks is_active=False and clears is_primary.
        """
        self.client.force_authenticate(user=self.manager)
        created = SupplierOrganizationService.create_supplier_contact(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=self.supplier.supplier_id,
            data={"name": "Departing Officer", "phone": "+8801755555555", "is_primary": True},
        )
        cnt_id = created["contact_id"]

        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/{cnt_id}/"
        del_res = self.client.delete(url)
        assert del_res.status_code == status.HTTP_200_OK
        assert del_res.data["is_active"] is False
        assert del_res.data["is_primary"] is False

        db_c = SupplierContactPerson.objects.get(contact_id=cnt_id)
        assert db_c.is_active is False
        assert db_c.is_primary is False

    def test_09_list_filtering_by_role_and_active_status(self):
        """
        9. List Filtering: Filter contacts by role and active status, ordered with primary contact first.
        """
        self.client.force_authenticate(user=self.manager)

        # Contact A: Sales Rep (Primary)
        SupplierOrganizationService.create_supplier_contact(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=self.supplier.supplier_id,
            data={"name": "Kamal Sales", "phone": "+8801766666666", "role": "SALES_REPRESENTATIVE", "is_primary": True},
        )
        # Contact B: Logistics (Non-primary)
        SupplierOrganizationService.create_supplier_contact(
            tenant_id=self.tenant.tenant_id,
            actor_user=self.manager,
            supplier_id=self.supplier.supplier_id,
            data={"name": "Jamal Logistics", "phone": "+8801777777777", "role": "DISPATCH_LOGISTICS", "is_primary": False},
        )

        # Filter by role=SALES_REPRESENTATIVE
        url_role = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/?role=SALES_REPRESENTATIVE"
        res_role = self.client.get(url_role)
        assert res_role.status_code == status.HTTP_200_OK
        names_role = [c["name"] for c in res_role.data["results"]]
        assert "Kamal Sales" in names_role
        assert "Jamal Logistics" not in names_role

        # Full list check: Primary contact comes first
        res_all = self.client.get(f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/contacts/")
        assert res_all.status_code == status.HTTP_200_OK
        assert res_all.data["results"][0]["name"] == "Kamal Sales"
        assert res_all.data["results"][0]["is_primary"] is True

    def test_10_multiple_contacts_for_different_procurement_roles(self):
        """
        10. Functional Role Taxonomy: Supports depot manager, sales rep, accounts, and logistics.
        """
        self.client.force_authenticate(user=self.manager)

        roles_to_test = [
            ("Depot In-Charge", SupplierContactPersonRole.DEPOT_MANAGER, "+8801788888881"),
            ("Area Sales MIO", SupplierContactPersonRole.SALES_REPRESENTATIVE, "+8801788888882"),
            ("Billing Officer", SupplierContactPersonRole.ACCOUNTS_EXECUTIVE, "+8801788888883"),
            ("Delivery Van Driver", SupplierContactPersonRole.DISPATCH_LOGISTICS, "+8801788888884"),
        ]

        for name, role, phone in roles_to_test:
            res = SupplierOrganizationService.create_supplier_contact(
                tenant_id=self.tenant.tenant_id,
                actor_user=self.manager,
                supplier_id=self.supplier.supplier_id,
                data={"name": name, "role": role, "phone": phone},
            )
            assert res["role"] == role
            assert res["contact_id"].startswith("sup-cnt-")

        # Total active contacts under supplier
        all_contacts = SupplierContactPerson.objects.filter(
            tenant=self.tenant,
            supplier=self.supplier,
            is_active=True,
        )
        assert all_contacts.count() >= 4

