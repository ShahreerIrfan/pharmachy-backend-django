"""
Integration Test Suite for P104 - Granular Action Permissions (ADR-0104).
Covers all 9 operational actions, role defaults, custom role mapping,
wholesale cost field masking, cross-tenant isolation, and branch isolation.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.granular_permission_service import GranularActionPermissionService
from apps.core.models import (
    Branch,
    CustomRoleDefinition,
    MedicineProduct,
    PlanTier,
    StaffMember,
    StaffMembership,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


class GranularActionPermissionsIntegrationTest(TestCase):
    """Verifies end-to-end enforcement of the 9 granular permissions and cost masking."""

    def setUp(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p104-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-pharma-{self.suffix}",
            plan_tier=PlanTier.PRO,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Dhanmondi Main Branch",
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant,
            name="Gulshan Branch",
        )

        # Foreign Tenant & Branch for Cross-Tenant Isolation
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"tn-foreign-{self.suffix}",
            name="Foreign Pharmacy Ltd",
            subdomain=f"foreign-pharma-{self.suffix}",
            plan_tier=PlanTier.BASIC,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-foreign-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Branch",
        )

        # Shared Global Master Medicine
        self.medicine = MedicineProduct.objects.create(
            product_id=f"med-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals",
            mrp_unit=Decimal("3.00"),
            cost_unit=Decimal("2.15"),
        )

        # 1. Pharmacy Owner
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )

        # 2. Branch Manager
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Manager {self.suffix}",
            role="BRANCH_MANAGER",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Cashier
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            role="CASHIER",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. Pharmacist
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            role="STAFF_PHARMACIST",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 5. Inventory Clerk
        self.clerk = PharmacyUser.objects.create_user(
            email=f"clerk_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Clerk {self.suffix}",
            role="INVENTORY_CLERK",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 6. Finance Officer
        self.finance = PharmacyUser.objects.create_user(
            email=f"finance_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Finance {self.suffix}",
            role="FINANCE_OFFICER",
            tenant_id=self.tenant.tenant_id,
        )

        # 7. Auditor
        self.auditor = PharmacyUser.objects.create_user(
            email=f"auditor_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Auditor {self.suffix}",
            role="AUDITOR",
            tenant_id=self.tenant.tenant_id,
        )

    def test_01_owner_has_all_9_actions_and_sees_cost(self):
        """Owner receives all 9 action permissions and cost visibility."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.get("/api/v2/permissions/granular/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        self.assertEqual(data["role"], "PHARMACY_OWNER")
        self.assertTrue(data["cost_visibility_enabled"])
        for act in GranularActionPermissionService.VALID_ACTIONS:
            self.assertTrue(data["actions"][act], f"Owner missing action: {act}")

        # Cost visibility on medicines
        med_resp = self.client.get("/api/v2/permissions/granular/medicines/")
        self.assertEqual(med_resp.status_code, status.HTTP_200_OK)
        med_data = med_resp.json()
        self.assertTrue(med_data["cost_visibility_granted"])
        found = [m for m in med_data["results"] if m["product_id"] == self.medicine.product_id]
        self.assertTrue(found)
        self.assertEqual(found[0]["cost_unit"], "2.15")

    def test_02_cashier_sale_only_and_cost_masked(self):
        """Cashier can execute sale, but all other 8 actions are forbidden and cost is masked."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/permissions/granular/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        self.assertTrue(data["actions"]["sale"])
        self.assertFalse(data["cost_visibility_enabled"])
        for forbidden in [
            "refund",
            "discount",
            "cost_visibility",
            "stock_change",
            "purchasing",
            "expense_approval",
            "export",
            "rx_review",
        ]:
            self.assertFalse(data["actions"][forbidden], f"Cashier should not have {forbidden}")

        # Cost price masking
        med_resp = self.client.get("/api/v2/permissions/granular/medicines/")
        self.assertEqual(med_resp.status_code, status.HTTP_200_OK)
        med_data = med_resp.json()
        self.assertFalse(med_data["cost_visibility_granted"])
        found = [m for m in med_data["results"] if m["product_id"] == self.medicine.product_id]
        self.assertTrue(found)
        self.assertIsNone(found[0]["cost_unit"])
        self.assertEqual(found[0]["mrp_unit"], "3.00")

        # Sale allowed
        sale_resp = self.client.post(
            "/api/v2/permissions/granular/sale/",
            {
                "branch_id": self.branch_a.branch_id,
                "invoice_id": "inv-cashier-001",
                "total_amount_bdt": "150.00",
            },
        )
        self.assertEqual(sale_resp.status_code, status.HTTP_200_OK)

        # Refund denied
        refund_resp = self.client.post(
            "/api/v2/permissions/granular/refund/",
            {
                "branch_id": self.branch_a.branch_id,
                "invoice_id": "inv-cashier-001",
                "refund_amount_bdt": "50.00",
            },
        )
        self.assertEqual(refund_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_GRANULAR_PERMISSION_DENIED", str(refund_resp.data))

        # Discount denied
        disc_resp = self.client.post(
            "/api/v2/permissions/granular/discount/",
            {"branch_id": self.branch_a.branch_id, "discount_percentage": "10.00"},
        )
        self.assertEqual(disc_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Stock change denied
        stock_resp = self.client.post(
            "/api/v2/permissions/granular/stock-change/",
            {
                "branch_id": self.branch_a.branch_id,
                "medicine_id": self.medicine.product_id,
                "quantity_delta": -2,
            },
        )
        self.assertEqual(stock_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Expense approval denied
        exp_resp = self.client.post(
            "/api/v2/permissions/granular/expense-approval/",
            {"branch_id": self.branch_a.branch_id, "amount_bdt": "500.00"},
        )
        self.assertEqual(exp_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Export denied
        export_resp = self.client.post("/api/v2/permissions/granular/export/", {})
        self.assertEqual(export_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Rx review denied
        rx_resp = self.client.post(
            "/api/v2/permissions/granular/rx-review/",
            {"branch_id": self.branch_a.branch_id, "prescription_id": "rx-001"},
        )
        self.assertEqual(rx_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_03_pharmacist_sale_and_rx_review_only(self):
        """Staff Pharmacist can review prescriptions and sell, but cannot approve expenses or adjust stock."""
        self.client.force_authenticate(user=self.pharmacist)

        # Rx review allowed
        rx_resp = self.client.post(
            "/api/v2/permissions/granular/rx-review/",
            {
                "branch_id": self.branch_a.branch_id,
                "prescription_id": "rx-101",
                "clinical_notes": "Course dosage verified for adult.",
            },
        )
        self.assertEqual(rx_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(rx_resp.json()["clinical_status"], "VERIFIED")

        # Expense approval denied
        exp_resp = self.client.post(
            "/api/v2/permissions/granular/expense-approval/",
            {"branch_id": self.branch_a.branch_id, "amount_bdt": "250.00"},
        )
        self.assertEqual(exp_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Refund denied
        refund_resp = self.client.post(
            "/api/v2/permissions/granular/refund/",
            {
                "branch_id": self.branch_a.branch_id,
                "invoice_id": "inv-001",
                "refund_amount_bdt": "20.00",
            },
        )
        self.assertEqual(refund_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_04_branch_manager_operations_and_cross_branch_guard(self):
        """Branch Manager can authorize refunds, discounts, stock adjustments, and expenses within branch."""
        self.client.force_authenticate(user=self.manager)

        # Refund allowed within assigned branch
        ref_resp = self.client.post(
            "/api/v2/permissions/granular/refund/",
            {
                "branch_id": self.branch_a.branch_id,
                "invoice_id": "inv-mgr-001",
                "refund_amount_bdt": "75.00",
                "reason": "Damaged packaging returned by customer",
            },
        )
        self.assertEqual(ref_resp.status_code, status.HTTP_200_OK)

        # Stock change allowed within assigned branch
        stock_resp = self.client.post(
            "/api/v2/permissions/granular/stock-change/",
            {
                "branch_id": self.branch_a.branch_id,
                "medicine_id": self.medicine.product_id,
                "quantity_delta": -3,
                "reason": "Damaged bottle writeoff",
            },
        )
        self.assertEqual(stock_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(stock_resp.json()["action"], "stock_change")

        # Expense approval allowed within assigned branch
        exp_resp = self.client.post(
            "/api/v2/permissions/granular/expense-approval/",
            {
                "branch_id": self.branch_a.branch_id,
                "category": "UTILITIES",
                "amount_bdt": "1500.00",
                "description": "Branch electricity bill",
                "approval_action": "APPROVED",
            },
        )
        self.assertEqual(exp_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(exp_resp.json()["status"], "APPROVED")

        # Cross-branch action denied (Manager assigned to branch_a attempting branch_b)
        cross_resp = self.client.post(
            "/api/v2/permissions/granular/refund/",
            {
                "branch_id": self.branch_b.branch_id,
                "invoice_id": "inv-mgr-cross",
                "refund_amount_bdt": "50.00",
            },
        )
        self.assertEqual(cross_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_05_inventory_clerk_and_finance_officer(self):
        """Inventory Clerk can adjust stock and see costs; Finance Officer can export and approve expenses."""
        # 1. Clerk
        self.client.force_authenticate(user=self.clerk)
        clerk_resp = self.client.get("/api/v2/permissions/granular/")
        self.assertEqual(clerk_resp.status_code, status.HTTP_200_OK)
        c_actions = clerk_resp.json()["actions"]
        self.assertTrue(c_actions["stock_change"])
        self.assertTrue(c_actions["purchasing"])
        self.assertTrue(c_actions["cost_visibility"])
        self.assertFalse(c_actions["sale"])
        self.assertFalse(c_actions["refund"])
        self.assertFalse(c_actions["expense_approval"])

        # 2. Finance
        self.client.force_authenticate(user=self.finance)
        fin_resp = self.client.get("/api/v2/permissions/granular/")
        self.assertEqual(fin_resp.status_code, status.HTTP_200_OK)
        f_actions = fin_resp.json()["actions"]
        self.assertTrue(f_actions["expense_approval"])
        self.assertTrue(f_actions["export"])
        self.assertTrue(f_actions["cost_visibility"])
        self.assertFalse(f_actions["sale"])
        self.assertFalse(f_actions["stock_change"])

    def test_06_auditor_export_only(self):
        """Auditor can only export data, but has no mutation or cost visibility authority."""
        self.client.force_authenticate(user=self.auditor)
        aud_resp = self.client.get("/api/v2/permissions/granular/")
        self.assertEqual(aud_resp.status_code, status.HTTP_200_OK)
        a_actions = aud_resp.json()["actions"]
        self.assertTrue(a_actions["export"])
        self.assertFalse(a_actions["cost_visibility"])
        self.assertFalse(a_actions["sale"])
        self.assertFalse(a_actions["refund"])
        self.assertFalse(a_actions["expense_approval"])

        export_resp = self.client.post(
            "/api/v2/permissions/granular/export/",
            {"export_dataset": "REGULATORY_AUDIT"},
        )
        self.assertEqual(export_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(export_resp.json()["status"], "READY_FOR_DOWNLOAD")

    def test_07_custom_role_mapping(self):
        """User bound to a CustomRoleDefinition has permissions resolved from custom permission_keys."""
        CustomRoleDefinition.objects.create(
            role_id=f"c-role-{self.suffix}",
            tenant=self.tenant,
            role_code="SENIOR_DISPENSER",
            display_name="Senior Dispenser",
            base_role="CASHIER",
            permission_keys=["sales:checkout", "sales:void", "sales:discount_override"],
        )
        dispenser = PharmacyUser.objects.create_user(
            email=f"dispenser_{self.suffix}@pharmacy.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Dispenser {self.suffix}",
            role="SENIOR_DISPENSER",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        staff_rec = StaffMember.objects.create(
            staff_id=f"stf-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            username=dispenser.email,
            full_name=dispenser.full_name,
            role="CASHIER",
            mobile_number=dispenser.phone_number,
        )
        StaffMembership.objects.create(
            membership_id=f"mem-{self.suffix}",
            tenant=self.tenant,
            staff_member=staff_rec,
            role="SENIOR_DISPENSER",
            is_active=True,
        )

        self.client.force_authenticate(user=dispenser)
        resp = self.client.get("/api/v2/permissions/granular/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        actions = resp.json()["actions"]

        # Configured keys in custom role
        self.assertTrue(actions["sale"])
        self.assertTrue(actions["refund"])
        self.assertTrue(actions["discount"])
        # Non-configured keys
        self.assertFalse(actions["cost_visibility"])
        self.assertFalse(actions["stock_change"])
        self.assertFalse(actions["expense_approval"])

    def test_08_cross_tenant_isolation(self):
        """Prevent user from acting on foreign tenant's branch or records."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/permissions/granular/stock-change/",
            {
                "branch_id": self.foreign_branch.branch_id,
                "medicine_id": self.medicine.product_id,
                "quantity_delta": -1,
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(resp.data))
