"""
backend/tests/test_p107_branch_lifecycle.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0082, ADR-0091, ADR-0096, ADR-0105, ADR-0107
Phase 06: Milestone M03 - Branch Lifecycle Management (P107)
Requirement Trace: FR-001, FR-004, FR-049, FR-070, FR-071, FR-082

Comprehensive Integration Test Suite for Physical Pharmacy Branch Lifecycle:
1. Valid branch store creation within plan quota with audit log.
2. Branch profile update and main branch toggling.
3. Temporary branch suspension with offline lease revocation.
4. Quota-safeguarded branch reactivation and over-quota blocking.
5. Permanent branch closure with 100% historical invoice, batch, and Rx trail preservation.
6. Guarded DELETE: hard deletion blocked when historical sales/batches exist (ERR_CANNOT_DELETE_BRANCH_WITH_HISTORY).
7. Plan branch quota limit exceeded blocks branch creation (ERR_QUOTA_EXCEEDED).
8. Incomplete or invalid input rejections (400 Bad Request).
9. Cross-tenant isolation barrier (ERR_CROSS_TENANT_VIOLATION).
10. Branch Manager separation of duties (SoD) boundaries.
11. Frontline cashier denied branch lifecycle mutations (ERR_PERMISSION_DENIED).
12. Suspended tenant subscription blocks branch lifecycle mutations.
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    BranchLifecycleEvent,
    BranchLifecycleEventType,
    BranchLifecycleStatus,
    DeviceLeaseCertificate,
    MedicineProduct,
    OfflineLeaseStatus,
    PlanTier,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import (
    PharmacyUser,
    UserRole,
)


class BranchLifecycleIntegrationTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant (PRO plan allows 100 branches)
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p107-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-p107-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Dhanmondi Central Store",
            branch_code="BR-DHN-01",
            license_number=f"DGDA-DL-DHN-{self.suffix}",
            is_main_branch=True,
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant,
            name="Gulshan Dispensary",
            branch_code="BR-GLS-02",
            license_number=f"DGDA-DL-GLS-{self.suffix}",
            is_main_branch=False,
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 2. Foreign Tenant for Cross-Tenant Isolation
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"tn-foreign-{self.suffix}",
            name=f"Foreign Pharma {self.suffix}",
            subdomain=f"foreign-p107-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-foreign-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Store",
            license_number=f"DGDA-DL-FOR-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 3. Master Medicine
        self.medicine = MedicineProduct.objects.create(
            product_id=f"med-p107-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals",
            mrp_unit=Decimal("3.00"),
            cost_unit=Decimal("2.10"),
        )

        # 4. Primary Tenant Owner
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )

        # 5. Foreign Tenant Owner
        self.foreign_owner = PharmacyUser.objects.create_user(
            email=f"f_owner_{self.suffix}@foreign.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Foreign Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.foreign_tenant.tenant_id,
        )

        # 6. Branch Manager for Branch A
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 7. Frontline Cashier
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier Tariq",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

    def test_01_valid_branch_creation_within_plan_quota(self):
        """Owner creates new branch store within plan branch quota."""
        self.client.force_authenticate(user=self.owner)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "name": f"Uttara Sector 3 Store {self.suffix}",
            "branch_code": f"BR-UTT-{self.suffix[:4]}",
            "license_number": f"DGDA-DL-UTT-{self.suffix}",
            "address": "Sector 3, Uttara, Dhaka-1230",
            "phone_number": "+8801711223344",
            "email": "uttara@popular.local",
            "manager_name": "Tariqul Islam",
            "is_main_branch": False,
        }

        res = self.client.post("/api/v2/branches/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.json()

        self.assertEqual(data["name"], payload["name"])
        self.assertEqual(data["status"], "ACTIVE")
        self.assertTrue(data["is_active"])
        self.assertEqual(data["license_number"], payload["license_number"])

        # Check database
        branch = Branch.objects.get(branch_id=data["branch_id"], tenant=self.tenant)
        self.assertEqual(branch.status, BranchLifecycleStatus.ACTIVE)
        self.assertTrue(branch.is_active)

        # Check immutable audit event
        evt = BranchLifecycleEvent.objects.filter(branch=branch, event_type=BranchLifecycleEventType.CREATED).first()
        self.assertIsNotNone(evt)
        self.assertEqual(evt.performed_by_username, self.owner.email)

    def test_02_update_branch_profile_and_main_branch_toggle(self):
        """Owner updates branch details and changes main branch designation."""
        self.client.force_authenticate(user=self.owner)

        # Branch A was main branch initially; set Branch B as main branch
        res = self.client.patch(f"/api/v2/branches/{self.branch_b.branch_id}/", {
            "address": "Gulshan Avenue 12, Dhaka",
            "phone_number": "+8801899887766",
            "manager_name": "Senior Manager Faruk",
            "is_main_branch": True,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        data = res.json()

        self.assertTrue(data["is_main_branch"])
        self.assertEqual(data["address"], "Gulshan Avenue 12, Dhaka")

        # Verify Branch A is no longer main branch
        self.branch_a.refresh_from_db()
        self.assertFalse(self.branch_a.is_main_branch)

    def test_03_temporary_suspension_revokes_offline_leases(self):
        """Suspending a branch store sets status to SUSPENDED and revokes active offline POS leases."""
        self.client.force_authenticate(user=self.owner)

        # Provision active offline POS lease for Branch B
        lease = DeviceLeaseCertificate.objects.create(
            lease_id=f"ls-b-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_b,
            device_id="dev-pos-b",
            user_id=str(self.owner.pk),
            user_email=self.owner.email,
            expires_at=timezone.now() + timezone.timedelta(days=2),
            signature="sig-b",
            status=OfflineLeaseStatus.ACTIVE,
        )

        res = self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/suspend/", {
            "reason": "Temporary renovation and air-conditioner repair",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        data = res.json()

        self.assertEqual(data["status"], "SUSPENDED")
        self.assertFalse(data["is_active"])

        # Check offline lease revoked
        lease.refresh_from_db()
        self.assertEqual(lease.status, OfflineLeaseStatus.REVOKED)

    def test_04_reactivation_restores_active_and_enforces_quota(self):
        """Reactivating a suspended branch restores ACTIVE status, but is blocked when branch quota is exceeded."""
        self.client.force_authenticate(user=self.owner)

        # Suspend Branch B
        self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/suspend/", {"reason": "Pause"}, format="json")

        # 1. Reactivate successfully under PRO plan (quota allows 100 branches)
        react_res = self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/reactivate/", {
            "reason": "Renovation complete",
        }, format="json")
        self.assertEqual(react_res.status_code, status.HTTP_200_OK, react_res.data)
        self.assertEqual(react_res.json()["status"], "ACTIVE")
        self.assertTrue(react_res.json()["is_active"])

        # 2. Suspend again and switch tenant to BASIC plan (quota is 1 branch, and Branch A is active)
        self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/suspend/", {"reason": "Pause 2"}, format="json")
        self.tenant.plan_tier = PlanTier.BASIC
        self.tenant.save()

        # Reactivation now blocked by QuotaGuard (1/1 active branches)
        blocked_res = self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/reactivate/", {}, format="json")
        self.assertEqual(blocked_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(blocked_res.json()["error_code"], "ERR_QUOTA_EXCEEDED")

    def test_05_permanent_closure_preserves_100_percent_historical_invoices_and_inventory(self):
        """
        Closing a branch store transitions status to CLOSED, revokes offline leases,
        and guarantees 100% preservation of historical invoices and stock batches.
        """
        self.client.force_authenticate(user=self.owner)

        # 1. Provision historical transactions & stock for Branch B
        invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-hist-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_b,
            invoice_number=f"INV-B-{self.suffix}",
            total_amount=Decimal("500.00"),
            net_amount=Decimal("500.00"),
            status="PAID",
        )
        batch = BatchStock.objects.create(
            batch_id=f"bat-hist-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_b,
            product=self.medicine,
            batch_number="BAT-990",
            quantity_base_units=200,
            expiry_date=timezone.now().date() + timezone.timedelta(days=365),
            purchase_date=timezone.now().date(),
            cost_per_unit=Decimal("2.10"),
        )

        # 2. Execute closure
        close_res = self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/close/", {
            "reason": "Store commercial lease expired; operations consolidated to central store",
        }, format="json")
        self.assertEqual(close_res.status_code, status.HTTP_200_OK, close_res.data)
        data = close_res.json()

        self.assertEqual(data["status"], "CLOSED")
        self.assertFalse(data["is_active"])
        self.assertFalse(data["is_main_branch"])
        self.assertIsNotNone(data["closed_at"])

        # 3. Check historical records are 100% preserved in database
        invoice.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(invoice.branch, self.branch_b)
        self.assertEqual(batch.branch, self.branch_b)

        # 4. Check historical summary endpoint
        summary_res = self.client.get(f"/api/v2/branches/{self.branch_b.branch_id}/historical-summary/")
        self.assertEqual(summary_res.status_code, status.HTTP_200_OK)
        s_data = summary_res.json()
        self.assertTrue(s_data["is_closed"])
        self.assertTrue(s_data["zero_data_loss_guaranteed"])
        self.assertEqual(s_data["historical_invoices_count"], 1)
        self.assertEqual(s_data["historical_stock_batches_count"], 1)

        # 5. Cannot reactivate closed branch
        react_try = self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/reactivate/", {}, format="json")
        self.assertEqual(react_try.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(react_try.json()["error_code"], "CANNOT_REACTIVATE_CLOSED_BRANCH")

    def test_06_hard_delete_blocked_when_history_exists(self):
        """Hard deletion of a branch with historical invoices is blocked (ERR_CANNOT_DELETE_BRANCH_WITH_HISTORY)."""
        self.client.force_authenticate(user=self.owner)

        # Provision invoice on Branch A
        SaleInvoice.objects.create(
            invoice_id=f"inv-del-chk-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            invoice_number=f"INV-A-{self.suffix}",
            total_amount=Decimal("150.00"),
            net_amount=Decimal("150.00"),
            status="PAID",
        )

        del_res = self.client.delete(f"/api/v2/branches/{self.branch_a.branch_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_400_BAD_REQUEST)
        data = del_res.json()
        self.assertEqual(data["error_code"], "ERR_CANNOT_DELETE_BRANCH_WITH_HISTORY")
        self.assertTrue("ক্লোজ" in data["message_bn"])

        # Create provisional branch with 0 history: hard delete succeeds (204 No Content)
        provisional = Branch.objects.create(
            branch_id=f"br-prov-{self.suffix}",
            tenant=self.tenant,
            name="Provisional Store",
            license_number="PROV-001",
        )
        del_prov = self.client.delete(f"/api/v2/branches/{provisional.branch_id}/")
        self.assertEqual(del_prov.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Branch.objects.filter(branch_id=provisional.branch_id).exists())

    def test_07_plan_branch_quota_exceeded_blocks_new_branch(self):
        """Tenant on Basic plan (max 1 branch) is blocked from creating a 2nd branch (ERR_QUOTA_EXCEEDED)."""
        self.client.force_authenticate(user=self.owner)

        # Switch tenant to BASIC plan
        self.tenant.plan_tier = PlanTier.BASIC
        self.tenant.save()

        # Branch A already active; attempt to create 2nd branch
        res = self.client.post("/api/v2/branches/", {
            "tenant_id": self.tenant.tenant_id,
            "name": "Over Quota Branch",
            "license_number": "DGDA-DL-OVER",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_QUOTA_EXCEEDED")

    def test_08_invalid_input_validation_errors(self):
        """Incomplete or invalid input is rejected with HTTP 400 Bad Request."""
        self.client.force_authenticate(user=self.owner)

        # 1. Short name
        r1 = self.client.post("/api/v2/branches/", {
            "tenant_id": self.tenant.tenant_id,
            "name": "A",
            "license_number": "DGDA-DL-001",
        }, format="json")
        self.assertEqual(r1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r1.json()["error_code"], "INVALID_BRANCH_NAME")

        # 2. Short license number
        r2 = self.client.post("/api/v2/branches/", {
            "tenant_id": self.tenant.tenant_id,
            "name": "Valid Name",
            "license_number": "DL",
        }, format="json")
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r2.json()["error_code"], "INVALID_LICENSE_NUMBER")

        # 3. Duplicate branch name within same tenant
        r3 = self.client.post("/api/v2/branches/", {
            "tenant_id": self.tenant.tenant_id,
            "name": self.branch_a.name,
            "license_number": "DGDA-DL-DUP",
        }, format="json")
        self.assertEqual(r3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r3.json()["error_code"], "BRANCH_NAME_ALREADY_EXISTS")

        # 4. Short closure reason
        r4 = self.client.post(f"/api/v2/branches/{self.branch_b.branch_id}/close/", {
            "reason": "no",
        }, format="json")
        self.assertEqual(r4.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r4.json()["error_code"], "CLOSURE_REASON_REQUIRED")

    def test_09_cross_tenant_isolation_barrier(self):
        """Foreign tenant owner is denied inspecting or modifying primary tenant branches."""
        self.client.force_authenticate(user=self.foreign_owner)

        # Inspect
        r1 = self.client.get(f"/api/v2/branches/{self.branch_a.branch_id}/?tenant_id={self.tenant.tenant_id}")
        self.assertEqual(r1.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r1.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

        # Close attempt
        r2 = self.client.post(
            f"/api/v2/branches/{self.branch_a.branch_id}/close/?tenant_id={self.tenant.tenant_id}",
            {"reason": "Malicious closure attempt"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)

    def test_10_branch_manager_sod_boundaries(self):
        """Branch Manager can view/update own branch contact info, but cannot create or close branches."""
        self.client.force_authenticate(user=self.manager)

        # 1. Can view own branch
        r1 = self.client.get(f"/api/v2/branches/{self.branch_a.branch_id}/")
        self.assertEqual(r1.status_code, status.HTTP_200_OK)

        # 2. Can update contact info on own branch
        r2 = self.client.patch(f"/api/v2/branches/{self.branch_a.branch_id}/", {
            "phone_number": "+8801799887766",
            "address": "Updated Dhanmondi Address",
        }, format="json")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)

        # 3. Blocked from inspecting other branch
        r3 = self.client.get(f"/api/v2/branches/{self.branch_b.branch_id}/")
        self.assertEqual(r3.status_code, status.HTTP_403_FORBIDDEN)

        # 4. Blocked from renaming branch or altering drug license
        r4 = self.client.patch(f"/api/v2/branches/{self.branch_a.branch_id}/", {
            "name": "New Manager Name",
            "license_number": "FORGED-LIC",
        }, format="json")
        self.assertEqual(r4.status_code, status.HTTP_403_FORBIDDEN)

        # 5. Blocked from closing branch
        r5 = self.client.post(f"/api/v2/branches/{self.branch_a.branch_id}/close/", {
            "reason": "Manager closing branch",
        }, format="json")
        self.assertEqual(r5.status_code, status.HTTP_403_FORBIDDEN)

        # 6. Blocked from creating new branch
        r6 = self.client.post("/api/v2/branches/", {
            "name": "Manager New Store",
            "license_number": "DGDA-002",
        }, format="json")
        self.assertEqual(r6.status_code, status.HTTP_403_FORBIDDEN)

    def test_11_frontline_cashier_denied_branch_mutations(self):
        """Frontline cashier is denied all branch mutation endpoints."""
        self.client.force_authenticate(user=self.cashier)

        # Denied Create
        r1 = self.client.post("/api/v2/branches/", {"name": "Cashier Store"}, format="json")
        self.assertEqual(r1.status_code, status.HTTP_403_FORBIDDEN)

        # Denied Update
        r2 = self.client.patch(f"/api/v2/branches/{self.branch_a.branch_id}/", {"address": "New"}, format="json")
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)

        # Denied Suspend
        r3 = self.client.post(f"/api/v2/branches/{self.branch_a.branch_id}/suspend/", {}, format="json")
        self.assertEqual(r3.status_code, status.HTTP_403_FORBIDDEN)

        # Denied Close
        r4 = self.client.post(f"/api/v2/branches/{self.branch_a.branch_id}/close/", {}, format="json")
        self.assertEqual(r4.status_code, status.HTTP_403_FORBIDDEN)

    def test_12_suspended_tenant_subscription_blocks_branch_mutations(self):
        """Suspended tenant subscription blocks branch lifecycle mutations."""
        self.client.force_authenticate(user=self.owner)

        # Suspend tenant
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        res = self.client.post("/api/v2/branches/", {
            "tenant_id": self.tenant.tenant_id,
            "name": f"Suspended Branch {self.suffix}",
            "license_number": "DGDA-SUSP-01",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        data = res.json()
        self.assertIn(data["error_code"], ["ERR_SUBSCRIPTION_INACTIVE", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"])
