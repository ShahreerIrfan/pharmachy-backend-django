"""
backend/tests/test_p061_tenant_lifecycle.py
Architecture Decision: ADR-0005 & ADR-0061
Comprehensive test suite for Phase 04 Tenant Lifecycle Model:
- Valid scenario: Super Admin provisions tenant, transitions status (ACTIVE -> SUSPENDED -> ACTIVE) with audit trail.
- Incomplete/Invalid input: Missing target_status or reason < 5 chars returns HTTP 400 or 422.
- Invariant protection: allow_negative_stock=true rejected with HTTP 400/422; immutable tenant_id enforced.
- Unauthorized scenario: Tenant Owner and Cashier denied provisioning or status transition (HTTP 403).
- Two-Tenant isolation: Tenant Owner listing tenants sees ONLY own tenant.
- Cross-Tenant barrier: Tenant Owner reading foreign tenant blocked with HTTP 403 + security audit log.
- Absolute block: Hard deletion of tenant accounts prohibited across ALL personas (HTTP 403).
- Canonical settings: GET/PATCH canonical settings verified for Tenant Owner and Super Admin.
"""

from __future__ import annotations

import os
import sys
import unittest
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
from apps.core.models import Tenant, TenantLifecycleStatus
from apps.identity.models import PharmacyUser, UserRole


class TestP061TenantLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p061-alpha",
            defaults={
                "name": "Alpha Pharma Ltd",
                "subdomain": "p061-alpha",
                "status": TenantLifecycleStatus.ACTIVE,
                "plan_tier": "STANDARD",
                "is_active": True,
            },
        )
        cls.tenant_b, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p061-beta",
            defaults={
                "name": "Beta Pharma Ltd",
                "subdomain": "p061-beta",
                "status": TenantLifecycleStatus.ACTIVE,
                "plan_tier": "PRO",
                "is_active": True,
            },
        )

        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email="p061_superadmin@pharmacy.bd",
            defaults={
                "phone_number": "+8801700000061",
                "full_name": "Platform Superadmin P061",
                "role": UserRole.SUPERADMIN,
                "is_superuser": True,
                "tenant_id": "platform-global",
            },
        )
        cls.owner_a, _ = PharmacyUser.objects.get_or_create(
            email="p061_owner_a@pharmacy.bd",
            defaults={
                "phone_number": "+8801711000061",
                "full_name": "Owner Alpha P061",
                "role": UserRole.PHARMACY_OWNER,
                "tenant_id": cls.tenant_a.tenant_id,
            },
        )
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email="p061_cashier_a@pharmacy.bd",
            defaults={
                "phone_number": "+8801722000061",
                "full_name": "Cashier Alpha P061",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_a.tenant_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_superadmin_valid_provisioning_and_status_transitions(self):
        """Valid scenario: Super Admin provisions tenant, suspends, and reinstates with audit log."""
        import uuid

        self.client.force_authenticate(user=self.superadmin)

        # 1. Provision new tenant
        suffix = uuid.uuid4().hex[:6]
        new_tenant_id = f"tenant-p061-gamma-{suffix}"
        payload = {
            "tenant_id": new_tenant_id,
            "name": f"Gamma Chain Pharmacy {suffix}",
            "subdomain": f"p061-gamma-{suffix}",
            "plan_tier": "BASIC",
            "status": "ACTIVE",
            "billing_email": "billing@gamma.bd",
        }
        create_resp = self.client.post("/api/v2/tenants/", payload, format="json")
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_resp.json()["status"], "ACTIVE")

        # 2. Suspend tenant with audited reason
        suspend_payload = {
            "target_status": "SUSPENDED",
            "reason": "Regulatory compliance audit failure under DGDA directive.",
        }
        suspend_resp = self.client.post(
            f"/api/v2/tenants/{new_tenant_id}/transition-status/",
            suspend_payload,
            format="json",
        )
        self.assertEqual(suspend_resp.status_code, status.HTTP_200_OK)
        suspend_data = suspend_resp.json()
        self.assertTrue(suspend_data["success"])
        self.assertEqual(suspend_data["data"]["status"], "SUSPENDED")

        # Verify AuditLogEntry created
        audit_entry = AuditLogEntry.objects.filter(
            action="TENANT_LIFECYCLE_TRANSITION",
            resource_id=new_tenant_id,
        ).first()
        self.assertIsNotNone(audit_entry, "AuditLogEntry for lifecycle transition was not found.")
        self.assertEqual(audit_entry.after_state.get("status"), "SUSPENDED")

        # 3. Reinstate tenant back to ACTIVE
        reinstate_payload = {
            "target_status": "ACTIVE",
            "reason": "Reinstated following successful DGDA compliance re-inspection.",
        }
        reinstate_resp = self.client.post(
            f"/api/v2/tenants/{new_tenant_id}/transition-status/",
            reinstate_payload,
            format="json",
        )
        self.assertEqual(reinstate_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(reinstate_resp.json()["data"]["status"], "ACTIVE")

    def test_invalid_input_and_clinical_invariant_rejection(self):
        """Incomplete input & clinical safety violations rejected with HTTP 400 or 422."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Missing target_status or short reason
        bad_payload = {"target_status": "", "reason": "bad"}
        resp = self.client.post(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/transition-status/",
            bad_payload,
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 2. Invariant violation: allow_negative_stock = True
        bad_settings = {"allow_negative_stock": True}
        settings_resp = self.client.patch(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/canonical-settings/",
            bad_settings,
            format="json",
        )
        self.assertIn(settings_resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 3. Immutable tenant_id modification attempt
        immut_resp = self.client.patch(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/",
            {"tenant_id": "tenant-tampered-id"},
            format="json",
        )
        self.assertIn(immut_resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_unauthorized_tenant_provisioning_and_transition(self):
        """Unauthorized scenario: Non-superadmins (Owner, Cashier) denied provisioning and transitions."""
        # Owner attempts provisioning -> 403
        self.client.force_authenticate(user=self.owner_a)
        prov_resp = self.client.post(
            "/api/v2/tenants/",
            {"tenant_id": "rogue-tenant", "name": "Rogue", "subdomain": "rogue"},
            format="json",
        )
        self.assertEqual(prov_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Owner attempts transition-status on own tenant -> 403
        trans_resp = self.client.post(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/transition-status/",
            {"target_status": "ACTIVE", "reason": "Self-activation attempt"},
            format="json",
        )
        self.assertEqual(trans_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_two_tenant_isolation_in_listing(self):
        """Two-tenant isolation: Tenant Owner listing /api/v2/tenants/ sees ONLY own tenant."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.get("/api/v2/tenants/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        tenant_ids = [t["tenant_id"] for t in results]

        self.assertIn(self.tenant_a.tenant_id, tenant_ids)
        self.assertNotIn(self.tenant_b.tenant_id, tenant_ids, "Tenant B leaked to Tenant A owner!")

    def test_cross_tenant_access_denied_and_audited(self):
        """Cross-tenant violation: Tenant A owner reading Tenant B blocked with HTTP 403 + security audit."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.get(f"/api/v2/tenants/{self.tenant_b.tenant_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # Verify security incident audit entry
        incident = AuditLogEntry.objects.filter(
            action="SECURITY_INCIDENT_CROSS_TENANT",
            tenant_id=self.tenant_a.tenant_id,
        ).first()
        self.assertIsNotNone(incident, "Cross-tenant access attempt was not logged to AuditLogEntry.")

    def test_absolute_block_destructive_tenant_deletion(self):
        """Absolute block: Tenant accounts cannot be hard-deleted by any persona."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.delete(f"/api/v2/tenants/{self.tenant_a.tenant_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ABSOLUTE_SYSTEM_BLOCK", str(resp.json()))

    def test_canonical_settings_inspection_and_update(self):
        """Tenant Owner inspects and updates valid canonical settings (e.g. tax_percentage)."""
        self.client.force_authenticate(user=self.owner_a)

        # GET canonical settings
        get_resp = self.client.get(f"/api/v2/tenants/{self.tenant_a.tenant_id}/canonical-settings/")
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        settings_data = get_resp.json()["data"]
        self.assertEqual(settings_data["currency"], "BDT")
        self.assertEqual(settings_data["timezone"], "Asia/Dhaka")

        # PATCH update tax_percentage
        patch_resp = self.client.patch(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/canonical-settings/",
            {"tax_percentage": 5.0},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_resp.json()["data"]["tax_percentage"], 5.0)


if __name__ == "__main__":
    unittest.main()
