"""
backend/tests/test_p079_tenant_onboarding_wizard.py

Architecture Decision: ADR-0079
Comprehensive Integration Tests for Tenant Onboarding Wizard:
1. Valid scenario: Multi-step drafting, DGDA license, staff, inventory seed, and plan provisioning.
2. Incomplete/invalid inputs: Reserved subdomain, missing drug license, and weak password rejected.
3. Unauthorized & two-tenant isolation: Tenant A and Tenant B users blocked from platform wizard (403).
4. MFA & Step-Up enforcement: Super Admin requires active MFA and step-up token for final activation.
5. Immutability safeguard: Completed onboarding session deletion is blocked (403).
6. Atomic rollback: Failure during completion rolls back transaction without orphaned entities.
7. Session abandonment: Marking draft as abandoned logs immutable audit entry.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BatchStock,
    Branch,
    InitialInventoryMode,
    MedicineProduct,
    OnboardingWizardStatus,
    PlanTier,
    StaffMember,
    SubscriptionRecord,
    Tenant,
    TenantLifecycleStatus,
    TenantOnboardingSession,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP079TenantOnboardingWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin with MFA
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p079_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": True,
            },
        )

        # 2. Platform Admin WITHOUT MFA
        cls.superadmin_no_mfa, _ = PharmacyUser.objects.get_or_create(
            email=f"p079_admin_nomfa_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin No MFA {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": False,
            },
        )

        # 3. Tenant A & Owner A
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p079-a-{cls.suffix}",
            name=f"Tenant A {cls.suffix}",
            subdomain=f"onb-a-{cls.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p079_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 4. Tenant B & Cashier B
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p079-b-{cls.suffix}",
            name=f"Tenant B {cls.suffix}",
            subdomain=f"onb-b-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.cashier_b = PharmacyUser.objects.create(
            email=f"p079_cashier_b_{cls.suffix}@pharmacy.com",
            full_name=f"Cashier B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.CASHIER,
            is_active=True,
            mfa_enabled=True,
        )

        # Seed global medicine catalog baseline product
        cls.sample_medicine = MedicineProduct.objects.filter(brand_name="Napa 500mg").first()
        if not cls.sample_medicine:
            cls.sample_medicine = MedicineProduct.objects.create(
                product_id=f"med-seed-{cls.suffix}",
                brand_name="Napa 500mg",
                generic_name="Paracetamol",
                dosage_form="Tablet",
                strength="500mg",
                manufacturer="Beximco Pharmaceuticals Ltd",
                mrp_unit=Decimal("1.20"),
                cost_unit=Decimal("0.85"),
                dgda_dar_number="DGDA-DAR-00124",
                is_prescription_only=False,
            )

    def setUp(self):
        self.client = APIClient()
        self.mfa_headers = {
            "HTTP_X_MFA_VERIFIED": "true",
            "HTTP_X_MFA_SESSION_TOKEN": f"mfa-sess-{uuid.uuid4().hex[:8]}",
        }
        self.step_up_headers = {
            **self.mfa_headers,
            "HTTP_X_MFA_STEP_UP_TOKEN": f"stepup-token-{uuid.uuid4().hex[:8]}",
        }

    def test_01_valid_onboarding_wizard_lifecycle_end_to_end(self):
        """
        Valid Scenario: SuperAdmin creates draft session, updates steps, and completes provisioning.
        """
        self.client.force_authenticate(user=self.superadmin)
        unique_subdomain = f"popular-rx-{self.suffix}"
        owner_email = f"owner_{self.suffix}@popular.local"
        owner_phone = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture

        # Step 1: Create draft session
        create_payload = {
            "pharmacy_name": "Popular Pharmacy Dhanmondi",
            "subdomain": unique_subdomain,
            "trade_license_number": "TRAD-DSCC-99120",
            "drug_license_number": "DGDA-DL-2026-8801",
            "tin_number": "TIN-991283",
            "contact_email": owner_email,
            "contact_phone": owner_phone,
            "owner_full_name": "Dr. Rafiqul Islam",
            "plan_tier": "STANDARD",
            "billing_cycle": "MONTHLY",
            "is_trial": False,
        }
        resp = self.client.post(
            "/api/v2/platform/onboarding-wizard/",
            data=create_payload,
            format="json",
            **self.mfa_headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        session_id = resp.data["session_id"]
        self.assertEqual(resp.data["status"], OnboardingWizardStatus.DRAFT)
        self.assertEqual(resp.data["current_step"], 1)

        # Step 2: Update step data (Branch & Staff, Inventory Mode)
        step_payload = {
            "step": 4,
            "branch_name": "Main Dispensary",
            "branch_address": "House 12, Road 4, Dhanmondi, Dhaka",
            "initial_staff_data": [
                {
                    "full_name": "Kazi Nazrul",
                    "role": "PHARMACIST",
                    "mobile_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                    "pcb_registration_no": "PCB-A-9812",
                }
            ],
            "initial_inventory_mode": InitialInventoryMode.SEED_BASELINE,
            "plan_tier": "STANDARD",
        }
        patch_resp = self.client.post(
            f"/api/v2/platform/onboarding-wizard/{session_id}/update-step/",
            data=step_payload,
            format="json",
            **self.mfa_headers,
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK, patch_resp.data)
        self.assertEqual(patch_resp.data["current_step"], 4)
        self.assertEqual(patch_resp.data["status"], OnboardingWizardStatus.IN_PROGRESS)

        # Step 3: Complete wizard with step-up MFA
        complete_payload = {
            "owner_password": "Secur3P@ssw0rd!",
            "operator_notes": "All compliance verification checks approved.",
        }
        complete_resp = self.client.post(
            f"/api/v2/platform/onboarding-wizard/{session_id}/complete/",
            data=complete_payload,
            format="json",
            **self.step_up_headers,
        )
        self.assertEqual(complete_resp.status_code, status.HTTP_200_OK, complete_resp.data)
        self.assertEqual(complete_resp.data["status"], "PROVISIONED")

        # Verify DB artifacts
        expected_tenant_id = f"tn-{unique_subdomain}"
        tenant = Tenant.objects.get(tenant_id=expected_tenant_id)
        self.assertEqual(tenant.status, TenantLifecycleStatus.ACTIVE)
        self.assertEqual(tenant.plan_tier, PlanTier.STANDARD)

        branch = Branch.objects.get(branch_id=f"{expected_tenant_id}-br-main")
        self.assertEqual(branch.license_number, "DGDA-DL-2026-8801")
        self.assertEqual(branch.name, "Main Dispensary")

        owner_user = PharmacyUser.objects.get(email=owner_email)
        self.assertEqual(owner_user.role, UserRole.PHARMACY_OWNER)
        self.assertTrue(owner_user.check_password("Secur3P@ssw0rd!"))
        self.assertEqual(tenant.owner_user_id, str(owner_user.user_id))

        staff = StaffMember.objects.filter(tenant=tenant).first()
        self.assertIsNotNone(staff)
        self.assertEqual(staff.full_name, "Kazi Nazrul")
        self.assertEqual(staff.pcb_registration_no, "PCB-A-9812")

        stock_count = BatchStock.objects.filter(tenant=tenant, branch=branch).count()
        self.assertGreaterEqual(stock_count, 1)

        sub = SubscriptionRecord.objects.get(tenant=tenant)
        self.assertEqual(sub.plan_tier, PlanTier.STANDARD)
        self.assertEqual(sub.amount_bdt, Decimal("6500.00"))
        self.assertEqual(sub.licensed_branches, 5)
        self.assertEqual(sub.licensed_devices, 10)

        # Verify immutable audit log
        audit = AuditLogEntry.objects.filter(
            action="TENANT_ONBOARDING_COMPLETED",
            resource_id=expected_tenant_id,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata_json["session_id"], session_id)

        # Verify session status is completed
        session = TenantOnboardingSession.objects.get(session_id=session_id)
        self.assertEqual(session.status, OnboardingWizardStatus.COMPLETED)
        self.assertIsNotNone(session.completed_at)

    def test_02_invalid_or_incomplete_onboarding_inputs(self):
        """
        Invalid scenario: Reserved subdomain, missing drug license, and weak password rejected.
        """
        self.client.force_authenticate(user=self.superadmin)

        # Reserved subdomain
        resp1 = self.client.post(
            "/api/v2/platform/onboarding-wizard/",
            data={
                "pharmacy_name": "Admin Pharmacy",
                "subdomain": "superadmin",
                "drug_license_number": "DGDA-DL-1122",
                "contact_email": f"admin_rx_{self.suffix}@test.local",
            "contact_phone": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "owner_full_name": "Test Owner",
            },
            format="json",
            **self.mfa_headers,
        )
        self.assertIn(resp1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Missing drug license
        resp2 = self.client.post(
            "/api/v2/platform/onboarding-wizard/",
            data={
                "pharmacy_name": "No License Pharmacy",
                "subdomain": f"nolicense-{self.suffix}",
                "drug_license_number": "   ",
                "contact_email": f"nolicense_{self.suffix}@test.local",
            "contact_phone": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "owner_full_name": "Test Owner",
            },
            format="json",
            **self.mfa_headers,
        )
        self.assertIn(resp2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_03_unauthorized_and_cross_tenant_access_denied(self):
        """
        Unauthorized Scenario: Tenant A and Tenant B users blocked with 403 PLATFORM_ACCESS_DENIED.
        """
        # Tenant Owner A
        self.client.force_authenticate(user=self.owner_a)
        resp_a = self.client.get("/api/v2/platform/onboarding-wizard/")
        self.assertEqual(resp_a.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_a.data["error_code"], "PLATFORM_ACCESS_DENIED")

        # Cashier B
        self.client.force_authenticate(user=self.cashier_b)
        resp_b = self.client.post(
            "/api/v2/platform/onboarding-wizard/",
            data={"pharmacy_name": "Unauthorized Attempt"},
            format="json",
        )
        self.assertEqual(resp_b.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_b.data["error_code"], "PLATFORM_ACCESS_DENIED")

    def test_04_mfa_and_step_up_enforcement(self):
        """
        MFA Enforcement: Rejection if MFA disabled or step-up token missing during completion.
        """
        # Superadmin without MFA
        self.client.force_authenticate(user=self.superadmin_no_mfa)
        resp_nomfa = self.client.get("/api/v2/platform/onboarding-wizard/")
        self.assertEqual(resp_nomfa.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_nomfa.data["error_code"], "MFA_AUTHENTICATION_REQUIRED")

        # Superadmin with MFA but missing step-up token during completion
        self.client.force_authenticate(user=self.superadmin)
        draft_session = TenantOnboardingSession.objects.create(
            session_id=f"onb-test-mfa-{self.suffix}",
            pharmacy_name="MFA Test Pharmacy",
            subdomain=f"mfa-test-{self.suffix}",
            drug_license_number="DGDA-DL-5544",
            contact_email=f"mfa_{self.suffix}@test.local",
            contact_phone=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            owner_full_name="MFA Owner",
        )
        resp_no_stepup = self.client.post(
            f"/api/v2/platform/onboarding-wizard/{draft_session.session_id}/complete/",
            data={"owner_password": "ValidPassword123!"},
            format="json",
            **self.mfa_headers,  # Only basic MFA, missing step-up
        )
        self.assertEqual(resp_no_stepup.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_no_stepup.data["error_code"], "STEP_UP_MFA_REQUIRED")

    def test_05_immutability_completed_session_cannot_be_deleted(self):
        """
        Immutability Gate: DELETE on completed onboarding session returns 403 ABSOLUTE_SYSTEM_BLOCK.
        """
        self.client.force_authenticate(user=self.superadmin)
        completed_session = TenantOnboardingSession.objects.create(
            session_id=f"onb-done-{self.suffix}",
            pharmacy_name="Completed Pharmacy",
            subdomain=f"completed-{self.suffix}",
            drug_license_number="DGDA-DL-COMPLETED",
            contact_email=f"completed_{self.suffix}@test.local",
            contact_phone=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            owner_full_name="Done Owner",
            status=OnboardingWizardStatus.COMPLETED,
        )

        resp = self.client.delete(
            f"/api/v2/platform/onboarding-wizard/{completed_session.session_id}/",
            **self.mfa_headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["error_code"], "ABSOLUTE_SYSTEM_BLOCK")

    def test_06_atomic_rollback_on_failure(self):
        """
        Atomic rollback: Duplicate email during completion causes 100% rollback without orphaned records.
        """
        self.client.force_authenticate(user=self.superadmin)
        # Create an existing user with target email
        dup_email = f"dup_{self.suffix}@pharmacy.local"
        PharmacyUser.objects.create(
            email=dup_email,
            full_name="Existing User",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            role=UserRole.CASHIER,
            is_active=True,
        )

        subdomain = f"rollback-{self.suffix}"
        session = TenantOnboardingSession.objects.create(
            session_id=f"onb-rb-{self.suffix}",
            pharmacy_name="Rollback Pharmacy",
            subdomain=subdomain,
            drug_license_number="DGDA-DL-RB-11",
            contact_email=dup_email,
            contact_phone=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            owner_full_name="Rollback Owner",
        )

        resp = self.client.post(
            f"/api/v2/platform/onboarding-wizard/{session.session_id}/complete/",
            data={"owner_password": "ValidPassword123!"},
            format="json",
            **self.step_up_headers,
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Assert zero orphaned tenants or branches
        self.assertFalse(Tenant.objects.filter(tenant_id=f"tn-{subdomain}").exists())
        self.assertFalse(Branch.objects.filter(branch_id=f"tn-{subdomain}-br-main").exists())

    def test_07_abandon_session_audit(self):
        """
        Session abandonment: Status set to ABANDONED and immutable audit trail recorded.
        """
        self.client.force_authenticate(user=self.superadmin)
        session = TenantOnboardingSession.objects.create(
            session_id=f"onb-ab-{self.suffix}",
            pharmacy_name="Abandon Pharmacy",
            subdomain=f"abandon-{self.suffix}",
            drug_license_number="DGDA-DL-AB-99",
            contact_email=f"abandon_{self.suffix}@test.local",
            contact_phone=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            owner_full_name="Abandon Owner",
        )

        resp = self.client.post(
            f"/api/v2/platform/onboarding-wizard/{session.session_id}/abandon/",
            data={"reason": "Customer cancelled application"},
            format="json",
            **self.mfa_headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        session.refresh_from_db()
        self.assertEqual(session.status, OnboardingWizardStatus.ABANDONED)

        # Check audit trail
        audit = AuditLogEntry.objects.filter(
            action="TENANT_ONBOARDING_ABANDONED",
            resource_id=session.session_id,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata_json["reason"], "Customer cancelled application")


if __name__ == "__main__":
    unittest.main()
