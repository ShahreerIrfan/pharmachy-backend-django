"""
Integration tests for P111: Login device registration, cryptographic hardware trust,
and audited enrollment (ADR-0111).
Tests cover:
- Device self-enrollment with Ed25519/RSA public key and SHA-256 fingerprint generation
- Separation of duties (RBAC): Owner vs Branch Manager vs Cashier
- Cross-branch violation protections (ERR_CROSS_BRANCH_VIOLATION)
- Dual-control approvals and quota synchronization with DeviceSeat
- Rejection and revocation workflows requiring mandatory reasons
- Cryptographic verification challenge endpoint
- Device label modification
- Multi-tenant boundary isolation (ERR_CROSS_TENANT_VIOLATION)
- Suspended tenant subscription protection
- OpenAPI and JSON schema compliance
"""

import hashlib
import json
import random
import uuid
from pathlib import Path

from django.test import TestCase
from jsonschema import validate
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    BranchStaffMembership,
    DeviceRegistrationAuditEvent,
    DeviceRegistrationStatus,
    DeviceSeat,
    DeviceSeatStatus,
    LoginDeviceRegistration,
    PlanTier,
    StaffMember,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "login_device_registration.schema.json"


class LoginDeviceRegistrationIntegrationTests(TestCase):
    """End-to-end integration tests for P111 Login Device Registration."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Provision Tenant A (Active, Pro Tier)
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p111-a-{self.suffix}",
            name=f"Lazz Pharma Dhaka {self.suffix}",
            subdomain=f"lazz-a-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
        )

        # 2. Provision Tenant B (Foreign Tenant)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p111-b-{self.suffix}",
            name=f"Popular Pharma B {self.suffix}",
            subdomain=f"pop-b-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status="ACTIVE",
        )

        # 3. Provision Branches for Tenant A
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Dhanmondi Branch {self.suffix}",
            license_number=f"DGDA-DL-A1-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=True,
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Gulshan Branch {self.suffix}",
            license_number=f"DGDA-DL-A2-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 4. Provision Branch for Tenant B
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name=f"Chittagong Branch {self.suffix}",
            license_number=f"DGDA-DL-B-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 5. Provision Users for Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Pharmacy Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        self.manager_a1 = PharmacyUser.objects.create_user(
            email=f"mgr1_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Branch Manager Dhanmondi",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.staff_mgr = StaffMember.objects.create(
            staff_id=f"stf-mgr-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=self.manager_a1.email,
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            mobile_number=f"01700{random.randint(100000, 999999)}",
            is_active=True,
        )
        BranchStaffMembership.objects.create(
            membership_id=f"bsm-{self.suffix}",
            tenant=self.tenant_a,
            staff_member=self.staff_mgr,
            branch=self.branch_a1,
            role=UserRole.BRANCH_MANAGER,
            is_active=True,
        )

        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"csh1_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Cashier Dhanmondi",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 6. Provision User for Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Foreign Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        # Sample PEM keys
        self.sample_pem_1 = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MCowBQYDK2VwAyEA9fVbK1b9Lz4N6pQ8rX1yA3s5u7w9y1z3b5d7f9h1j3k=\n"
            "-----END PUBLIC KEY-----"
        )
        self.sample_pem_2 = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MCowBQYDK2VwAyEAMN9bK1b9Lz4N6pQ8rX1yA3s5u7w9y1z3b5d7f9h1j99=\n"
            "-----END PUBLIC KEY-----"
        )

    def _expected_fp(self, pem_str: str) -> str:
        return hashlib.sha256(pem_str.strip().encode("utf-8")).hexdigest()

    # -------------------------------------------------------------------------
    # 1. Device Registration Creation & Fingerprinting
    # -------------------------------------------------------------------------

    def test_register_device_creates_pending_approval(self):
        """POS terminal submits registration request, entering PENDING_APPROVAL with 64-char fingerprint."""
        self.client.force_authenticate(user=self.cashier_a1)

        payload = {
            "branch_id": self.branch_a1.branch_id,
            "device_id": f"pos-counter-01-{self.suffix}",
            "device_label": "Counter 1 - Fast Dispense",
            "public_key_pem": self.sample_pem_1,
            "key_algorithm": "ED25519",
            "device_type": "DESKTOP_POS",
            "hardware_fingerprint": f"HW-DESK-99-{self.suffix}",
            "mac_address": "00:1A:2B:3C:4D:5E",
        }

        resp = self.client.post("/api/v2/devices/registrations/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

        data = resp.data
        self.assertTrue(data["registration_id"].startswith("dreg-"))
        self.assertEqual(data["status"], "PENDING_APPROVAL")
        self.assertEqual(data["device_id"], payload["device_id"])
        self.assertEqual(data["key_fingerprint"], self._expected_fp(self.sample_pem_1))

        # Check DB and Audit Trail
        reg = LoginDeviceRegistration.objects.get(registration_id=data["registration_id"])
        self.assertEqual(reg.status, DeviceRegistrationStatus.PENDING_APPROVAL)
        self.assertEqual(reg.requested_by_username, self.cashier_a1.email)

        audit = DeviceRegistrationAuditEvent.objects.filter(device_registration=reg, action="REGISTER_REQUESTED").first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_username, self.cashier_a1.email)

    # -------------------------------------------------------------------------
    # 2. Owner Approval & Quota Sync
    # -------------------------------------------------------------------------

    def test_owner_approves_device_and_syncs_device_seat(self):
        """Owner approval activates device cryptographic trust and syncs with DeviceSeat model."""
        self.client.force_authenticate(user=self.cashier_a1)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-counter-02-{self.suffix}",
                "device_label": "Counter 2 - Main POS",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Owner approves
        self.client.force_authenticate(user=self.owner_a)
        approve_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/approve/",
            data={"approval_notes": "Terminal physically verified by owner."},
            format="json",
        )
        self.assertEqual(approve_resp.status_code, status.HTTP_200_OK, approve_resp.data)
        self.assertEqual(approve_resp.data["status"], "APPROVED")
        self.assertEqual(approve_resp.data["approved_by_username"], self.owner_a.email)

        # Check DeviceSeat synchronization
        seat = DeviceSeat.objects.filter(tenant=self.tenant_a, device_id=f"pos-counter-02-{self.suffix}").first()
        self.assertIsNotNone(seat)
        self.assertEqual(seat.status, DeviceSeatStatus.ACTIVE)
        self.assertEqual(seat.device_name, "Counter 2 - Main POS")
        self.assertEqual(seat.enrolled_by, self.owner_a.email)

        # Verify audit trail
        audit = DeviceRegistrationAuditEvent.objects.filter(
            device_registration__registration_id=reg_id, action="APPROVED"
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_username, self.owner_a.email)
        self.assertEqual(audit.actor_role, "PHARMACY_OWNER")

    # -------------------------------------------------------------------------
    # 3. Branch Manager Approvals & Cross-Branch Isolation
    # -------------------------------------------------------------------------

    def test_branch_manager_approves_assigned_branch_device(self):
        """Branch Manager can approve devices belonging strictly to their assigned branch."""
        self.client.force_authenticate(user=self.cashier_a1)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-counter-03-{self.suffix}",
                "device_label": "Counter 3 - Dhanmondi Desk",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Manager of branch_a1 approves
        self.client.force_authenticate(user=self.manager_a1)
        approve_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/approve/",
            data={"approval_notes": "Manager Dhanmondi verified workstation."},
            format="json",
        )
        self.assertEqual(approve_resp.status_code, status.HTTP_200_OK, approve_resp.data)
        self.assertEqual(approve_resp.data["status"], "APPROVED")
        self.assertEqual(approve_resp.data["approved_by_username"], self.manager_a1.email)

    def test_branch_manager_cross_branch_violation_blocked(self):
        """Branch Manager attempting to approve device in another branch is blocked with ERR_CROSS_BRANCH_VIOLATION."""
        # Register device under branch_a2 (Gulshan)
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a2.branch_id,
                "device_id": f"pos-counter-gulshan-{self.suffix}",
                "device_label": "Counter Gulshan 1",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Manager of branch_a1 attempts to approve branch_a2 device
        self.client.force_authenticate(user=self.manager_a1)
        approve_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/approve/",
            data={"approval_notes": "Cross-branch attempt"},
            format="json",
        )
        self.assertEqual(approve_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(approve_resp.data.get("error_code"), "ERR_CROSS_BRANCH_VIOLATION")

    # -------------------------------------------------------------------------
    # 4. Cashier Denied Approval / Rejection / Revocation
    # -------------------------------------------------------------------------

    def test_cashier_denied_approval_authority(self):
        """Cashier role is strictly denied approval, rejection, and revocation authority."""
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-cashier-test-{self.suffix}",
                "device_label": "Cashier Test POS",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Cashier tries to approve
        self.client.force_authenticate(user=self.cashier_a1)
        approve_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/approve/",
            data={},
            format="json",
        )
        self.assertEqual(approve_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(approve_resp.data.get("error_code"), "ERR_PERMISSION_DENIED")

        # Cashier tries to reject
        reject_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/reject/",
            data={"rejection_reason": "Cashier unauthorized rejection"},
            format="json",
        )
        self.assertEqual(reject_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(reject_resp.data.get("error_code"), "ERR_PERMISSION_DENIED")

        # Cashier tries to revoke
        revoke_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/revoke/",
            data={"revocation_reason": "Cashier unauthorized revocation"},
            format="json",
        )
        self.assertEqual(revoke_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(revoke_resp.data.get("error_code"), "ERR_PERMISSION_DENIED")

    # -------------------------------------------------------------------------
    # 5. Rejection & Revocation Workflows
    # -------------------------------------------------------------------------

    def test_rejection_requires_mandatory_reason(self):
        """Rejecting device requires non-empty rejection_reason and transitions to REJECTED."""
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-rogue-{self.suffix}",
                "device_label": "Rogue Laptop",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Blank reason fails with ERR_REASON_REQUIRED
        empty_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/reject/",
            data={"rejection_reason": "  "},
            format="json",
        )
        self.assertEqual(empty_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(empty_resp.data.get("error_code"), "ERR_REASON_REQUIRED")

        # Valid reason succeeds
        valid_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/reject/",
            data={"rejection_reason": "Rogue MAC address not recognized by IT team."},
            format="json",
        )
        self.assertEqual(valid_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(valid_resp.data["status"], "REJECTED")
        self.assertEqual(valid_resp.data["rejection_reason"], "Rogue MAC address not recognized by IT team.")

    def test_revocation_requires_reason_and_updates_device_seat(self):
        """Revocation transitions device to REVOKED and updates DeviceSeat to REVOKED."""
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-stolen-{self.suffix}",
                "device_label": "Compromised Terminal",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Approve first
        self.client.post(f"/api/v2/devices/registrations/{reg_id}/approve/", data={}, format="json")

        # Revoke without reason fails
        empty_revoke = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/revoke/",
            data={"revocation_reason": ""},
            format="json",
        )
        self.assertEqual(empty_revoke.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(empty_revoke.data.get("error_code"), "ERR_REASON_REQUIRED")

        # Revoke with reason succeeds
        revoke_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/revoke/",
            data={"revocation_reason": "Motherboard stolen from retail counter."},
            format="json",
        )
        self.assertEqual(revoke_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(revoke_resp.data["status"], "REVOKED")
        self.assertEqual(revoke_resp.data["revocation_reason"], "Motherboard stolen from retail counter.")

        # Check DeviceSeat status updated
        seat = DeviceSeat.objects.get(tenant=self.tenant_a, device_id=f"pos-stolen-{self.suffix}")
        self.assertEqual(seat.status, DeviceSeatStatus.REVOKED)

    # -------------------------------------------------------------------------
    # 6. Verification Challenge Gate
    # -------------------------------------------------------------------------

    def test_verification_challenge_endpoint(self):
        """Verification endpoint accurately evaluates trust, approval status, and key mismatch."""
        device_id = f"pos-challenge-{self.suffix}"
        self.client.force_authenticate(user=self.owner_a)

        # 1. Unknown device -> ERR_DEVICE_NOT_FOUND
        unreg_resp = self.client.post(
            "/api/v2/devices/registrations/verify/",
            data={"device_id": "pos-nonexistent-terminal"},
            format="json",
        )
        self.assertEqual(unreg_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(unreg_resp.data.get("is_trusted"))
        self.assertEqual(unreg_resp.data.get("error_code"), "ERR_DEVICE_NOT_FOUND")

        # 2. Register device (PENDING_APPROVAL)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": device_id,
                "device_label": "Challenge Terminal",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Verify pending device -> ERR_DEVICE_NOT_APPROVED
        pending_resp = self.client.post(
            "/api/v2/devices/registrations/verify/",
            data={"device_id": device_id, "public_key_pem": self.sample_pem_1},
            format="json",
        )
        self.assertEqual(pending_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(pending_resp.data.get("error_code"), "ERR_DEVICE_NOT_APPROVED")

        # 3. Approve device -> Verify with matching key -> 200 OK & is_trusted=True
        self.client.post(f"/api/v2/devices/registrations/{reg_id}/approve/", data={}, format="json")
        approved_resp = self.client.post(
            "/api/v2/devices/registrations/verify/",
            data={"device_id": device_id, "public_key_pem": self.sample_pem_1},
            format="json",
        )
        self.assertEqual(approved_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(approved_resp.data.get("is_trusted"))
        self.assertEqual(approved_resp.data.get("status"), "APPROVED")
        self.assertEqual(approved_resp.data.get("device_id"), device_id)

        # 4. Approved device with different public key -> ERR_DEVICE_KEY_MISMATCH
        mismatch_resp = self.client.post(
            "/api/v2/devices/registrations/verify/",
            data={"device_id": device_id, "public_key_pem": self.sample_pem_2},
            format="json",
        )
        self.assertEqual(mismatch_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(mismatch_resp.data.get("error_code"), "ERR_DEVICE_KEY_MISMATCH")

        # 5. Revoke device -> ERR_DEVICE_REVOKED
        self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/revoke/",
            data={"revocation_reason": "Decommissioned"},
            format="json",
        )
        revoked_resp = self.client.post(
            "/api/v2/devices/registrations/verify/",
            data={"device_id": device_id, "public_key_pem": self.sample_pem_1},
            format="json",
        )
        self.assertEqual(revoked_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(revoked_resp.data.get("error_code"), "ERR_DEVICE_REVOKED")

    # -------------------------------------------------------------------------
    # 7. Device Label Modification
    # -------------------------------------------------------------------------

    def test_update_device_label(self):
        """Updating device label synchronizes LoginDeviceRegistration and DeviceSeat."""
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-label-{self.suffix}",
                "device_label": "Old Label Desk",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]
        self.client.post(f"/api/v2/devices/registrations/{reg_id}/approve/", data={}, format="json")

        patch_resp = self.client.patch(
            f"/api/v2/devices/registrations/{reg_id}/",
            data={"device_label": "Counter 1 - Express Checkout"},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_resp.data["device_label"], "Counter 1 - Express Checkout")

        seat = DeviceSeat.objects.get(tenant=self.tenant_a, device_id=f"pos-label-{self.suffix}")
        self.assertEqual(seat.device_name, "Counter 1 - Express Checkout")

    # -------------------------------------------------------------------------
    # 8. Cross-Tenant Isolation Barrier
    # -------------------------------------------------------------------------

    def test_cross_tenant_isolation_barrier(self):
        """User from Tenant B cannot access or approve devices of Tenant A."""
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-iso-{self.suffix}",
                "device_label": "Tenant A Secret Station",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        reg_id = reg_resp.data["registration_id"]

        # Authenticate as Owner B (Tenant B)
        self.client.force_authenticate(user=self.owner_b)

        # 1. Retrieve across tenants -> 404 Not Found (or 403 Forbidden)
        get_resp = self.client.get(f"/api/v2/devices/registrations/{reg_id}/")
        self.assertIn(get_resp.status_code, [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN])

        # 2. Approve across tenants -> 403 / 400
        approve_resp = self.client.post(
            f"/api/v2/devices/registrations/{reg_id}/approve/",
            data={"approval_notes": "Cross-tenant intrusion attempt"},
            format="json",
        )
        self.assertIn(approve_resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN])

    # -------------------------------------------------------------------------
    # 9. Suspended Tenant Subscription Protection
    # -------------------------------------------------------------------------

    def test_suspended_tenant_subscription_blocks_operations(self):
        """Device registration operations are blocked when tenant subscription is suspended."""
        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-suspended-{self.suffix}",
                "device_label": "Suspended Counter",
                "public_key_pem": self.sample_pem_1,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp_data = resp.json() if hasattr(resp, "json") else getattr(resp, "data", {})
        self.assertEqual(resp_data.get("error_code"), "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    # -------------------------------------------------------------------------
    # 10. Contract Schema Compliance
    # -------------------------------------------------------------------------

    def test_serialized_registration_conforms_to_json_schema(self):
        """Serialized registration payload conforms strictly to login_device_registration.schema.json."""
        self.client.force_authenticate(user=self.owner_a)
        reg_resp = self.client.post(
            "/api/v2/devices/registrations/",
            data={
                "branch_id": self.branch_a1.branch_id,
                "device_id": f"pos-schema-{self.suffix}",
                "device_label": "Schema Verified Terminal",
                "public_key_pem": self.sample_pem_1,
                "hardware_fingerprint": "HW-SCHEMA-12345",
            },
            format="json",
        )
        self.assertEqual(reg_resp.status_code, status.HTTP_201_CREATED)

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)

        # Remove extra serializer fields not in basic schema before validation
        payload = {
            "registration_id": reg_resp.data["registration_id"],
            "tenant_id": reg_resp.data["tenant_id"],
            "branch_id": reg_resp.data["branch_id"],
            "device_id": reg_resp.data["device_id"],
            "device_label": reg_resp.data["device_label"],
            "device_type": reg_resp.data["device_type"],
            "public_key_pem": reg_resp.data["public_key_pem"],
            "key_algorithm": reg_resp.data["key_algorithm"],
            "key_fingerprint": reg_resp.data["key_fingerprint"],
            "hardware_fingerprint": reg_resp.data.get("hardware_fingerprint") or "",
            "status": reg_resp.data["status"],
            "requested_by_username": reg_resp.data["requested_by_username"],
            "approved_by_username": reg_resp.data.get("approved_by_username"),
            "approved_at": reg_resp.data.get("approved_at"),
            "created_at": reg_resp.data["created_at"],
        }
        validate(instance=payload, schema=schema)

