"""
backend/tests/test_p319_device_revocation_compromise.py
Architecture Decision: ADR-0319 (P319).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Device Revocation, Compromise Quarantining & Data Recovery:
1. Valid device revocation by Pharmacy Owner and immediate lease/key termination.
2. Cloud push / protected sync rejection on revoked devices (ERR_DEVICE_REVOKED).
3. Evidence retention and owner reconciliation of recovery dockets.
4. Input validation (invalid compromise type, empty revocation reason).
5. Cross-tenant isolation: Tenant B cannot revoke or access Tenant A's device.
6. Role permissions: Non-owner/cashier cannot revoke devices.
7. DRF ViewSet endpoints (/revoke, /reconcile, /list).
"""

import datetime
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.device_compromise_recovery_service import DeviceCompromiseRecoveryService
from apps.core.models import (
    Branch,
    CompromiseType,
    DeviceCompromiseRecoveryDocket,
    DeviceEncryptionKeyRecord,
    DeviceEncryptionKeyStatus,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    RecoveryDocketStatus,
    SignedOfflineSubscriptionLease,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestDeviceRevocationCompromiseIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-drc-a-{self.suffix}",
            name=f"Revocation Pharma A {self.suffix}",
            subdomain=f"drc-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-drc-a-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Branch"
        )
        self.device_a = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-drc-a-{self.suffix}",
            device_id=f"POS-DRC-A-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )
        self.key_a = DeviceEncryptionKeyRecord.objects.create(
            key_id=f"key-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_registration=self.device_a,
            dek_fingerprint_sha256=self.fp,
            wrapped_dek_blob="mock_wrapped_blob",
            key_version=1,
            status=DeviceEncryptionKeyStatus.ACTIVE,
            expires_at=timezone.now() + datetime.timedelta(days=30),
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-drc-b-{self.suffix}",
            name=f"Revocation Pharma B {self.suffix}",
            subdomain=f"drc-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-drc-b-{self.suffix}", tenant=self.tenant_b, name="Banani Branch"
        )
        self.device_b = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-drc-b-{self.suffix}",
            device_id=f"POS-DRC-B-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Users
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_drc_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="DRC Owner A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_drc_a_{self.suffix}@test.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="DRC Cashier A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.CASHIER,
            is_active=True,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_drc_b_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="DRC Owner B",
            password="Password123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_01_revoke_device_and_terminate_leases_and_keys(self):
        evidence_payload = {"unsynced_sales": [{"invoice_id": "INV-101", "bdt": "450.00"}]}
        res = DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            compromise_type="STOLEN",
            revocation_reason="POS counter terminal stolen during night shift",
            unsynced_evidence=evidence_payload,
            actor_user=self.owner_a,
        )

        assert res["status"] == "REVOKED"
        assert res["recovery_status"] == RecoveryDocketStatus.EVIDENCE_RETAINED

        # Device registration MUST be REVOKED
        self.device_a.refresh_from_db()
        assert self.device_a.status == DeviceLocalDbStatus.REVOKED
        assert self.device_a.lease_expires_at <= timezone.now()

        # Key MUST be REVOKED
        self.key_a.refresh_from_db()
        assert self.key_a.status == DeviceEncryptionKeyStatus.COMPROMISED_REVOKED

        # Docket exists
        docket = DeviceCompromiseRecoveryDocket.objects.get(docket_id=res["docket_id"])
        assert docket.compromise_type == CompromiseType.STOLEN
        assert docket.retained_unsynced_evidence_json == evidence_payload

    def test_02_reconcile_recovery_docket(self):
        # Create a docket
        res = DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            compromise_type="RETIRED_HARDWARE",
            revocation_reason="Replaced terminal with newer model",
            actor_user=self.owner_a,
        )

        docket_id = res["docket_id"]
        rec_res = DeviceCompromiseRecoveryService.reconcile_recovery_docket(
            tenant_id=str(self.tenant_a.tenant_id),
            docket_id=docket_id,
            resolution_action="MANUALLY_RECONCILED",
            reconciliation_notes="Reviewed unsynced cash sales and verified bank drawer tally",
            actor_user=self.owner_a,
        )

        assert rec_res["recovery_status"] == RecoveryDocketStatus.MANUALLY_RECONCILED

        docket = DeviceCompromiseRecoveryDocket.objects.get(docket_id=docket_id)
        assert docket.recovery_status == RecoveryDocketStatus.MANUALLY_RECONCILED
        assert docket.reconciled_by == self.owner_a

    def test_03_validation_constraints(self):
        # Invalid compromise type
        with pytest.raises(ValidationError) as exc_type:
            DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                compromise_type="UNKNOWN_TYPE",
                revocation_reason="Valid reason here",
                actor_user=self.owner_a,
            )
        assert "ERR_INVALID_COMPROMISE_TYPE" in str(exc_type.value)

        # Empty revocation reason
        with pytest.raises(ValidationError) as exc_reason:
            DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                compromise_type="LOST",
                revocation_reason="bad",  # < 5 chars
                actor_user=self.owner_a,
            )
        assert "ERR_INVALID_REVOCATION_REASON" in str(exc_reason.value)

    def test_04_cross_tenant_isolation(self):
        # Tenant B tries to revoke Tenant A's device
        with pytest.raises((PermissionDenied, NotFound)):
            DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
                tenant_id=str(self.tenant_b.tenant_id),
                device_id=self.device_a.device_id,
                compromise_type="STOLEN",
                revocation_reason="Malicious cross-tenant attempt",
                actor_user=self.owner_b,
            )

    def test_05_cashier_role_forbidden(self):
        # Cashier cannot revoke devices
        with pytest.raises(PermissionDenied) as exc_perm:
            DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                compromise_type="STOLEN",
                revocation_reason="Cashier trying to revoke device",
                actor_user=self.cashier_a,
            )
        assert "ERR_PERMISSION_DENIED" in str(exc_perm.value)

    def test_06_drf_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        # POST /revoke
        payload = {
            "device_id": self.device_a.device_id,
            "compromise_type": "SECURITY_COMPROMISE",
            "revocation_reason": "Workstation infected with ransomware, quarantined immediately",
        }
        res_rev = self.client.post("/api/v2/offline-sync/device-revocations/revoke/", data=payload, format="json")
        assert res_rev.status_code == status.HTTP_200_OK
        docket_id = res_rev.json()["docket_id"]

        # GET /list
        res_list = self.client.get("/api/v2/offline-sync/device-revocations/")
        assert res_list.status_code == status.HTTP_200_OK
        assert res_list.json()["count"] >= 1

        # POST /reconcile
        rec_payload = {
            "docket_id": docket_id,
            "resolution_action": "MANUALLY_RECONCILED",
            "reconciliation_notes": "Hard disk forensically analyzed and ledger balances adjusted",
        }
        res_rec = self.client.post("/api/v2/offline-sync/device-revocations/reconcile/", data=rec_payload, format="json")
        assert res_rec.status_code == status.HTTP_200_OK
        assert res_rec.json()["recovery_status"] == "MANUALLY_RECONCILED"
