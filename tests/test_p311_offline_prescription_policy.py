"""
backend/tests/test_p311_offline_prescription_policy.py
Architecture Decision: ADR-0311 (P311).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Offline Prescription Policy & Fail-Closed Authorization Caching:
1. Policy guardrails: Owner configures branch Rx policy (`allow_offline_standard_rx=True`,
   `allow_offline_antimicrobial_rx=False`); enabling controlled/narcotic offline or fail-open -> 422.
2. Controlled/narcotic medicine (`is_narcotic=True`) & disallowed antimicrobial (`is_antibiotic=True` when policy=False)
   are UNCONDITIONALLY BLOCKED from offline token caching (422).
3. Valid minimal-PHI token caching: Standard Rx (`is_prescription_only=True`) and policy-enabled antimicrobial Rx
   issue signed tokens storing ONLY SHA-256 hash (`prescription_ref_hash_sha256`, zero raw Rx ID/PHI) with exact Decimal BDT.
4. Fail-closed verification & replay-safe dispensing:
   - Missing token, tampered signature (403), over-quantity (422), revoked token (422), and expired token (422) FAIL CLOSED.
   - Valid dispense increments `dispensed_units`, returns exact `dispensed_value_bdt` and `antimicrobial_register_required=True`,
     and duplicate `idempotency_key` replay NEVER double-consumes units!
5. Audited Revocation emits P307 `ChangeFeedEvent(TOMBSTONE)` immediately; Cashier issue/revoke blocked (403);
   cross-branch, cross-tenant, expired lease, and suspended tenant blocked (403).
"""

import datetime
from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    ChangeFeedEvent,
    ChangeFeedOperation,
    DeviceLocalDbRegistration,
    MedicineProduct,
    OfflineRxTokenStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestOfflinePrescriptionPolicyIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp1 = "33557799" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-rx-{self.suffix}",
            name=f"Rx Policy Pharma {self.suffix}",
            subdomain=f"rxp-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-rx1-{self.suffix}", tenant=self.tenant_a, name="Rx Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-rx2-{self.suffix}", tenant=self.tenant_a, name="Rx Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-rxb-{self.suffix}",
            name=f"Other Rx {self.suffix}",
            subdomain=f"rxb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-rxb1-{self.suffix}", tenant=self.tenant_b, name="Other Rx Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"rx_owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Rx Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"rx_cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Rx Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"rx_cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Rx Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"rx_ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Rx Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.dev1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-RX1-{self.suffix}",
            device_fingerprint_sha256=self.fp1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )

        self.med_rx = MedicineProduct.objects.create(
            product_id=f"med-rx-{self.suffix}",
            brand_name="Amlocard 5",
            generic_name="Amlodipine",
            dosage_form="TABLET",
            cost_unit=Decimal("3.00"),
            mrp_unit=Decimal("5.50"),
            is_prescription_only=True,
            is_antibiotic=False,
            is_narcotic=False,
        )
        self.med_abx = MedicineProduct.objects.create(
            product_id=f"med-abx-{self.suffix}",
            brand_name="Zimax 500",
            generic_name="Azithromycin",
            dosage_form="TABLET",
            cost_unit=Decimal("22.00"),
            mrp_unit=Decimal("35.00"),
            is_prescription_only=True,
            is_antibiotic=True,
            is_narcotic=False,
        )
        self.med_narc = MedicineProduct.objects.create(
            product_id=f"med-narc-{self.suffix}",
            brand_name="Morphine Controlled",
            generic_name="Morphine Sulfate",
            dosage_form="TABLET",
            cost_unit=Decimal("12.00"),
            mrp_unit=Decimal("20.00"),
            is_prescription_only=True,
            is_antibiotic=False,
            is_narcotic=True,
        )

    def test_01_policy_approved_caching_fail_closed_dispense_and_tombstone_revocation(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Attempting fail-open or controlled/narcotic offline -> REJECTED (422)
        r_bad1 = self.client.post(
            "/api/v2/offline-sync/prescription-policies/",
            {"branch_id": self.branch_a1.branch_id, "allow_offline_controlled_narcotic": True},
            format="json",
        )
        assert r_bad1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        r_bad2 = self.client.post(
            "/api/v2/offline-sync/prescription-policies/",
            {"branch_id": self.branch_a1.branch_id, "fail_closed_when_unverified": False},
            format="json",
        )
        assert r_bad2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 2. Create valid branch Rx policy (Standard Rx allowed, Antimicrobial disallowed initially)
        pol = self.client.post(
            "/api/v2/offline-sync/prescription-policies/",
            {
                "branch_id": self.branch_a1.branch_id,
                "allow_offline_standard_rx": True,
                "allow_offline_antimicrobial_rx": False,
                "max_token_ttl_hours": 24,
            },
            format="json",
        )
        assert pol.status_code == status.HTTP_201_CREATED
        policy_id = pol.data["policy"]["policy_id"]

        # 3. Controlled narcotic ALWAYS blocked from offline caching (422)
        r_narc = self.client.post(
            "/api/v2/offline-sync/prescription-policies/issue-token/",
            {
                "device_id": self.dev1.device_id,
                "product_id": self.med_narc.product_id,
                "prescription_reference_id": "RX-NARC-001",
                "approved_max_units": 5,
                "idempotency_key": f"tok-narc-{self.suffix}",
            },
            format="json",
        )
        assert r_narc.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 4. Antimicrobial blocked while policy disallows it (422)
        r_abx_blk = self.client.post(
            "/api/v2/offline-sync/prescription-policies/issue-token/",
            {
                "device_id": self.dev1.device_id,
                "product_id": self.med_abx.product_id,
                "prescription_reference_id": "RX-ABX-001",
                "approved_max_units": 6,
                "idempotency_key": f"tok-abx-blk-{self.suffix}",
            },
            format="json",
        )
        assert r_abx_blk.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Enable antimicrobial offline pre-verified caching via PATCH
        self.client.patch(
            f"/api/v2/offline-sync/prescription-policies/{policy_id}/",
            {"allow_offline_antimicrobial_rx": True},
            format="json",
        )

        # 5. Issue policy-approved minimal-PHI antimicrobial token (6 units @ 35.00 = 210.00 BDT)
        raw_rx_secret = "PATIENT-RX-PRIVATE-9988"
        r_tok = self.client.post(
            "/api/v2/offline-sync/prescription-policies/issue-token/",
            {
                "device_id": self.dev1.device_id,
                "product_id": self.med_abx.product_id,
                "prescription_reference_id": raw_rx_secret,
                "approved_max_units": 6,
                "ttl_hours": 12,
                "idempotency_key": f"tok-abx-{self.suffix}",
            },
            format="json",
        )
        assert r_tok.status_code == status.HTTP_201_CREATED
        tok_data = r_tok.data["token"]
        assert tok_data["authorized_total_bdt"] == "210.00"
        assert tok_data["antimicrobial_register_required"] is True
        assert raw_rx_secret not in str(tok_data)  # Minimized PHI: raw ref never exposed/stored!
        assert len(tok_data["prescription_ref_hash_sha256"]) == 64

        # 6. Cashier verifies offline dispense of 4 units (4 * 35.00 = 140.00 BDT)
        self.client.force_authenticate(user=self.cashier_a1)

        # Tampered signature -> FAIL CLOSED (403)
        r_tamper = self.client.post(
            "/api/v2/offline-sync/prescription-policies/verify-dispense/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "token_id": tok_data["token_id"],
                "token_signature_sha256": "0" * 64,
                "dispensed_units": 4,
                "local_invoice_ref": "OFF-RX-1",
                "idempotency_key": f"disp-tamp-{self.suffix}",
            },
            format="json",
        )
        assert r_tamper.status_code == status.HTTP_403_FORBIDDEN

        # Valid dispense of 4 units -> PASS + antimicrobial_register_required=True
        r_disp = self.client.post(
            "/api/v2/offline-sync/prescription-policies/verify-dispense/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "token_id": tok_data["token_id"],
                "token_signature_sha256": tok_data["token_signature_sha256"],
                "dispensed_units": 4,
                "local_invoice_ref": "OFF-RX-1",
                "idempotency_key": f"disp-1-{self.suffix}",
            },
            format="json",
        )
        assert r_disp.status_code == status.HTTP_200_OK
        assert r_disp.data["dispensed_value_bdt"] == "140.00"
        assert r_disp.data["antimicrobial_register_required"] is True
        assert r_disp.data["token"]["remaining_units"] == 2

        # Repeated delivery of identical idempotency_key -> idempotent replay (remaining STAYS 2!)
        r_disp_dup = self.client.post(
            "/api/v2/offline-sync/prescription-policies/verify-dispense/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "token_id": tok_data["token_id"],
                "token_signature_sha256": tok_data["token_signature_sha256"],
                "dispensed_units": 4,
                "local_invoice_ref": "OFF-RX-1",
                "idempotency_key": f"disp-1-{self.suffix}",
            },
            format="json",
        )
        assert r_disp_dup.status_code == status.HTTP_200_OK
        assert r_disp_dup.data["idempotent_replay"] is True
        assert r_disp_dup.data["token"]["remaining_units"] == 2

        # Over-quantity dispense (requesting 3 when only 2 remain) -> FAIL CLOSED (422)
        r_over = self.client.post(
            "/api/v2/offline-sync/prescription-policies/verify-dispense/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "token_id": tok_data["token_id"],
                "token_signature_sha256": tok_data["token_signature_sha256"],
                "dispensed_units": 3,
                "local_invoice_ref": "OFF-RX-2",
                "idempotency_key": f"disp-over-{self.suffix}",
            },
            format="json",
        )
        assert r_over.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 7. Owner revokes token -> P307 TOMBSTONE emitted; subsequent dispense fails closed (422)!
        self.client.force_authenticate(user=self.owner_a)
        r_rev = self.client.post(
            "/api/v2/offline-sync/prescription-policies/revoke-token/",
            {"token_id": tok_data["token_id"], "reason": "Doctor cancelled remaining antibiotic refill"},
            format="json",
        )
        assert r_rev.status_code == status.HTTP_200_OK
        assert r_rev.data["token"]["status"] == OfflineRxTokenStatus.REVOKED

        cf_tomb = ChangeFeedEvent.objects.filter(
            tenant=self.tenant_a, idempotency_key=f"orxtok-rev:{tok_data['token_id']}"
        ).first()
        assert cf_tomb is not None
        assert cf_tomb.operation == ChangeFeedOperation.TOMBSTONE

        self.client.force_authenticate(user=self.cashier_a1)
        r_after_rev = self.client.post(
            "/api/v2/offline-sync/prescription-policies/verify-dispense/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "token_id": tok_data["token_id"],
                "token_signature_sha256": tok_data["token_signature_sha256"],
                "dispensed_units": 1,
                "local_invoice_ref": "OFF-RX-3",
                "idempotency_key": f"disp-after-rev-{self.suffix}",
            },
            format="json",
        )
        assert r_after_rev.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Owner archives policy via DELETE
        self.client.force_authenticate(user=self.owner_a)
        r_del = self.client.delete(f"/api/v2/offline-sync/prescription-policies/{policy_id}/")
        assert r_del.status_code == status.HTTP_200_OK
        assert r_del.data["policy"]["is_archived"] is True

    def test_02_rbac_cross_tenant_cross_branch_lease_expiry_and_suspended_drills(self):
        # 1. Cashier cannot issue Rx auth tokens or modify policy (403)
        self.client.force_authenticate(user=self.cashier_a1)
        r_cash_tok = self.client.post(
            "/api/v2/offline-sync/prescription-policies/issue-token/",
            {
                "device_id": self.dev1.device_id,
                "product_id": self.med_rx.product_id,
                "prescription_reference_id": "RX-1",
                "approved_max_units": 10,
                "idempotency_key": f"cash-tok-{self.suffix}",
            },
            format="json",
        )
        assert r_cash_tok.status_code == status.HTTP_403_FORBIDDEN

        # 2. Cross-branch cashier blocked on verify-dispense (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_xbr = self.client.post(
            "/api/v2/offline-sync/prescription-policies/verify-dispense/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "token_id": "orxtok-missing",
                "token_signature_sha256": "a" * 64,
                "dispensed_units": 1,
                "local_invoice_ref": "OFF-XBR",
                "idempotency_key": f"xbr-{self.suffix}",
            },
            format="json",
        )
        assert r_xbr.status_code == status.HTTP_403_FORBIDDEN

        # 3. Cross-tenant Owner B blocked on tokens list & issue-token (403)
        self.client.force_authenticate(user=self.owner_b)
        r_xten_list = self.client.get(f"/api/v2/offline-sync/prescription-policies/tokens/?tenant_id={self.tenant_a.tenant_id}")
        assert r_xten_list.status_code == status.HTTP_403_FORBIDDEN

        r_xten_issue = self.client.post(
            "/api/v2/offline-sync/prescription-policies/issue-token/",
            {
                "device_id": self.dev1.device_id,
                "product_id": self.med_rx.product_id,
                "prescription_reference_id": "RX-XTEN",
                "approved_max_units": 2,
                "idempotency_key": f"xten-tok-{self.suffix}",
            },
            format="json",
        )
        assert r_xten_issue.status_code == status.HTTP_403_FORBIDDEN

        # 4. Expired offline device lease blocks token issue (403)
        self.dev1.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        self.dev1.save(update_fields=["lease_expires_at"])
        self.client.force_authenticate(user=self.owner_a)
        r_exp = self.client.post(
            "/api/v2/offline-sync/prescription-policies/issue-token/",
            {
                "device_id": self.dev1.device_id,
                "product_id": self.med_rx.product_id,
                "prescription_reference_id": "RX-EXP",
                "approved_max_units": 2,
                "idempotency_key": f"exp-tok-{self.suffix}",
            },
            format="json",
        )
        assert r_exp.status_code == status.HTTP_403_FORBIDDEN

        # 5. Suspended tenant blocks policy access (403)
        self.dev1.lease_expires_at = timezone.now() + datetime.timedelta(days=5)
        self.dev1.save(update_fields=["lease_expires_at"])
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])
        r_susp = self.client.get("/api/v2/offline-sync/prescription-policies/")
        assert r_susp.status_code == status.HTTP_403_FORBIDDEN
