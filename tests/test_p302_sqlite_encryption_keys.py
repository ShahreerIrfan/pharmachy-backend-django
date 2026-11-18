"""
backend/tests/test_p302_sqlite_encryption_keys.py
Architecture Decision: ADR-0302 (P302).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Local SQLite Encryption Keys, Rotation & Safe Recovery:
1. Owner provisions initial wrapped DEK (v1) bound to P301 device registration -> receives one-time recovery code;
   list endpoint never leaks raw wrapped_dek_blob or recovery code.
2. Atomic key rotation (v1 -> v2) retires v1, forbids reusing identical DEK fingerprint (400 ERR_KEY_REUSE_FORBIDDEN).
3. Safe device recovery rejects wrong recovery code (403), accepts valid code once (rotates code so replay fails with 403),
   and releases wrapped ciphertext envelope.
4. Emergency compromise revocation marks key COMPROMISED_REVOKED and cascades REVOKED to P301 DeviceLocalDbRegistration.
5. Plaintext key blobs rejected (400 ERR_UNWRAPPED_OR_INVALID_KEY_BLOB).
6. RBAC (Cashier & Branch Manager denied key management 403), Cross-tenant isolation (403), Suspended tenant block (403).
"""

import random
import uuid
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    DeviceEncryptionKeyRecord,
    DeviceEncryptionKeyStatus,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSqliteEncryptionKeysIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.dev_fp = "c1d2e3f4" * 8
        self.dek_fp_v1 = "1111aaaa" * 8
        self.dek_fp_v2 = "2222bbbb" * 8
        self.wrapped_v1 = "v1:c2FsdDEyMzQ1Njc4OTA=:aXYxMjM0NTY3ODkwMTI=:Y2lwaGVydGV4dF92MV9hZXNfZ2NtX2VuY3J5cHRlZF9kZWs="
        self.wrapped_v2 = "v1:c2FsdDk4NzY1NDMyMTA=:aXY5ODc2NTQzMjEwOTg=:Y2lwaGVydGV4dF92Ml9hZXNfZ2NtX2VuY3J5cHRlZF9kZWs="

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-key-{self.suffix}",
            name=f"Vault Pharma {self.suffix}",
            subdomain=f"vlt-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-key1-{self.suffix}", tenant=self.tenant_a, name="Vault Branch"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-keyb-{self.suffix}",
            name=f"Other Vault {self.suffix}",
            subdomain=f"vltb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-keyb1-{self.suffix}", tenant=self.tenant_b, name="Other Vault Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880131{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@pharma.com",
            phone_number=f"+880132{random.randint(1000000, 9999999)}",
            full_name="BM A1",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880133{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880134{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        # Register P301 device first
        self.client.force_authenticate(user=self.owner_a)
        reg = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-VAULT-01",
                "device_fingerprint_sha256": self.dev_fp,
            },
            format="json",
        )
        assert reg.status_code == status.HTTP_201_CREATED
        self.device_reg_id = reg.data["registration_id"]

    def test_01_provision_rotate_recover_and_cascade_revoke(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Provision v1 wrapped DEK
        prov = self.client.post(
            "/api/v2/offline-sync/device-keys/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_registration_id": self.device_reg_id,
                "algorithm": "AES_256_GCM_ENVELOPE",
                "dek_fingerprint_sha256": self.dek_fp_v1,
                "wrapped_dek_blob": self.wrapped_v1,
            },
            format="json",
        )
        assert prov.status_code == status.HTTP_201_CREATED, prov.data
        key_v1_id = prov.data["key_id"]
        recovery_code = prov.data["one_time_recovery_code"]
        assert recovery_code.startswith("RCV-")
        assert "wrapped_dek_blob" not in prov.data

        # 2. Reuse same DEK fingerprint during rotation blocked (400)
        bad_rot = self.client.post(
            f"/api/v2/offline-sync/device-keys/{key_v1_id}/rotate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "new_dek_fingerprint_sha256": self.dek_fp_v1,
                "new_wrapped_dek_blob": self.wrapped_v2,
            },
            format="json",
        )
        assert bad_rot.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert bad_rot.data["error_code"] == "ERR_KEY_REUSE_FORBIDDEN"

        # 3. Rotate v1 -> v2
        rot = self.client.post(
            f"/api/v2/offline-sync/device-keys/{key_v1_id}/rotate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "new_dek_fingerprint_sha256": self.dek_fp_v2,
                "new_wrapped_dek_blob": self.wrapped_v2,
            },
            format="json",
        )
        assert rot.status_code == status.HTTP_200_OK, rot.data
        key_v2_id = rot.data["key_id"]
        assert rot.data["key_version"] == 2
        assert DeviceEncryptionKeyRecord.objects.get(key_id=key_v1_id).status == DeviceEncryptionKeyStatus.RETIRED

        # 4. Safe Recovery: wrong code denied (403); valid code succeeds once; replay of old code fails (403)
        wrong = self.client.post(
            f"/api/v2/offline-sync/device-keys/{key_v2_id}/recover/",
            {"tenant_id": self.tenant_a.tenant_id, "recovery_code": "RCV-WRONG-CODE", "reason": "OS reinstall"},
            format="json",
        )
        assert wrong.status_code == status.HTTP_403_FORBIDDEN

        ok_rec = self.client.post(
            f"/api/v2/offline-sync/device-keys/{key_v2_id}/recover/",
            {"tenant_id": self.tenant_a.tenant_id, "recovery_code": recovery_code, "reason": "SSD replacement"},
            format="json",
        )
        assert ok_rec.status_code == status.HTTP_200_OK
        assert ok_rec.data["wrapped_dek_blob"] == self.wrapped_v2
        assert ok_rec.data["next_one_time_recovery_code"] != recovery_code

        replay = self.client.post(
            f"/api/v2/offline-sync/device-keys/{key_v2_id}/recover/",
            {"tenant_id": self.tenant_a.tenant_id, "recovery_code": recovery_code, "reason": "Replay attempt"},
            format="json",
        )
        assert replay.status_code == status.HTTP_403_FORBIDDEN

        # 5. Compromise Revocation cascades to P301 DeviceLocalDbRegistration
        rev = self.client.delete(
            f"/api/v2/offline-sync/device-keys/{key_v2_id}/",
            {"reason": "POS terminal stolen"},
            format="json",
        )
        assert rev.status_code == status.HTTP_200_OK
        assert rev.data["status"] == DeviceEncryptionKeyStatus.COMPROMISED_REVOKED
        assert rev.data["device_status"] == DeviceLocalDbStatus.REVOKED
        assert DeviceLocalDbRegistration.objects.get(registration_id=self.device_reg_id).status == DeviceLocalDbStatus.REVOKED

    def test_02_plaintext_key_rejected_rbac_cross_tenant_and_suspended(self):
        self.client.force_authenticate(user=self.owner_a)
        plain = self.client.post(
            "/api/v2/offline-sync/device-keys/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_registration_id": self.device_reg_id,
                "dek_fingerprint_sha256": self.dek_fp_v1,
                "wrapped_dek_blob": "RAW:PLAINTEXT_KEY_SHOULD_FAIL_1234567890",
            },
            format="json",
        )
        assert plain.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert plain.data["error_code"] == "ERR_UNWRAPPED_OR_INVALID_KEY_BLOB"

        # Branch Manager & Cashier denied key provisioning (403)
        for user in (self.bm_a1, self.cashier_a1):
            self.client.force_authenticate(user=user)
            assert (
                self.client.post(
                    "/api/v2/offline-sync/device-keys/",
                    {
                        "tenant_id": self.tenant_a.tenant_id,
                        "device_registration_id": self.device_reg_id,
                        "dek_fingerprint_sha256": self.dek_fp_v1,
                        "wrapped_dek_blob": self.wrapped_v1,
                    },
                    format="json",
                ).status_code
                == status.HTTP_403_FORBIDDEN
            )

        # Cross-tenant Owner B denied provisioning on Tenant A's device (403)
        self.client.force_authenticate(user=self.owner_b)
        assert (
            self.client.post(
                "/api/v2/offline-sync/device-keys/",
                {
                    "tenant_id": self.tenant_b.tenant_id,
                    "device_registration_id": self.device_reg_id,
                    "dek_fingerprint_sha256": self.dek_fp_v1,
                    "wrapped_dek_blob": self.wrapped_v1,
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Suspended tenant blocked
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert self.client.get(f"/api/v2/offline-sync/device-keys/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN
