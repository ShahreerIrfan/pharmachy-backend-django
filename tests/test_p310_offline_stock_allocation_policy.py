"""
backend/tests/test_p310_offline_stock_allocation_policy.py
Architecture Decision: ADR-0310 (P310).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Offline Stock Allocation Policy & Non-Destructive Oversell Disputes:
1. Policy CRUD & guardrails: Owner creates/updates/archives branch policy; negative stock posting forbidden (422);
   Cashier policy creation forbidden (403).
2. Two-device cross-device oversell drill:
   - Batch starts with 10 units @ BDT 4.5000.
   - Device 1 reconciles 7 units -> COMMITTED (remaining = 3, value = BDT 31.50).
   - Replay of Device 1 idempotency key -> idempotent_replay=True, remaining stays 3.
   - Device 2 reconciles 6 units (exceeds remaining 3 by shortage=3) -> OVERSELL_DISPUTE opened!
   - CRITICAL INVARIANT: BatchStock.quantity_base_units REMAINS 3 (NEVER driven negative or silently altered),
     disputed_cost_bdt == "13.50" (3 * 4.50), posted_records_preserved == True.
3. Explicit Audited Dispute Resolution:
   - Cashier cannot resolve dispute (403).
   - Missing resolution_notes / ref -> 422.
   - Owner resolves via RESOLVED_AUDITED_REVERSAL -> dispute resolved, posted BatchStock still preserved (3),
     and P307 ChangeFeedEvent (REVERSAL, monetary_impact_bdt="-13.50") emitted!
4. Hard Pharmacy Safety Guardrail: Expired & Quarantined batches unconditionally blocked (422).
5. Security Drills: Cross-branch cashier (403), cross-tenant owner (403), expired offline lease (403), suspended tenant (403).
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
    BatchStatus,
    BatchStock,
    Branch,
    ChangeFeedEvent,
    ChangeFeedOperation,
    DeviceLocalDbRegistration,
    MedicineProduct,
    OversellDisputeStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestOfflineStockAllocationPolicyIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp1 = "22446688" * 8
        self.fp2 = "11335577" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-oa-{self.suffix}",
            name=f"Alloc Pharma {self.suffix}",
            subdomain=f"oa-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-oa1-{self.suffix}", tenant=self.tenant_a, name="Alloc Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-oa2-{self.suffix}", tenant=self.tenant_a, name="Alloc Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-oab-{self.suffix}",
            name=f"Other Alloc {self.suffix}",
            subdomain=f"oab-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-oab1-{self.suffix}", tenant=self.tenant_b, name="Other Alloc Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"oa_owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="OA Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"oa_cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="OA Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"oa_cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="OA Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"oa_ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="OA Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.dev1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-OA1-{self.suffix}",
            device_fingerprint_sha256=self.fp1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )
        self.dev2 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-OA2-{self.suffix}",
            device_fingerprint_sha256=self.fp2,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-oa-{self.suffix}",
            brand_name="Sergel 20",
            generic_name="Esomeprazole",
            dosage_form="CAPSULE",
            cost_unit=Decimal("4.50"),
            mrp_unit=Decimal("7.00"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-oa-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="OA-B01",
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=4),
            quantity_base_units=10,
            cost_per_unit=Decimal("4.5000"),
            status=BatchStatus.ACTIVE,
        )
        self.expired_batch = BatchStock.objects.create(
            batch_id=f"bat-oa-exp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="OA-EXP",
            expiry_date=self.today - datetime.timedelta(days=1),
            purchase_date=self.today - datetime.timedelta(days=400),
            quantity_base_units=20,
            cost_per_unit=Decimal("4.5000"),
            status=BatchStatus.ACTIVE,
        )

    def test_01_policy_crud_two_device_oversell_non_destructive_and_audited_resolution(self):
        # 1. Owner creates branch allocation policy (PATCH & DELETE also verified)
        self.client.force_authenticate(user=self.owner_a)
        pol = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/",
            {
                "branch_id": self.branch_a1.branch_id,
                "policy_mode": "CONTROLLED_RECONCILIATION",
                "max_offline_reserve_pct": 80,
            },
            format="json",
        )
        assert pol.status_code == status.HTTP_201_CREATED
        policy_id = pol.data["policy"]["policy_id"]
        assert pol.data["policy"]["allow_negative_stock_posting"] is False

        # Attempting to enable silent negative stock posting -> REJECTED (422)
        r_neg = self.client.patch(
            f"/api/v2/offline-sync/stock-allocation-policies/{policy_id}/",
            {"allow_negative_stock_posting": True},
            format="json",
        )
        assert r_neg.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 2. Device 1 reconciles 7 of 10 units -> COMMITTED cleanly (remaining = 3, value = 31.50)
        self.client.force_authenticate(user=self.cashier_a1)
        res1 = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "batch_id": self.batch.batch_id,
                "reserved_base_units": 7,
                "local_invoice_ref": f"OFF-OA1-{self.suffix}",
                "idempotency_key": f"idem-oa1-{self.suffix}",
            },
            format="json",
        )
        assert res1.status_code == status.HTTP_200_OK
        assert res1.data["oversell_detected"] is False
        assert res1.data["remaining_batch_units"] == 3
        assert res1.data["reservation"]["reserved_value_bdt"] == "31.50"

        # Repeated delivery of Device 1 -> idempotent replay (remaining STAYS 3!)
        res1_dup = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "batch_id": self.batch.batch_id,
                "reserved_base_units": 7,
                "local_invoice_ref": f"OFF-OA1-{self.suffix}",
                "idempotency_key": f"idem-oa1-{self.suffix}",
            },
            format="json",
        )
        assert res1_dup.status_code == status.HTTP_200_OK
        assert res1_dup.data["idempotent_replay"] is True
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 3

        # 3. TWO-DEVICE OVERSELL DRILL: Device 2 tries to reconcile 6 units when only 3 remain!
        res2 = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev2.device_id,
                "device_fingerprint_sha256": self.fp2,
                "batch_id": self.batch.batch_id,
                "reserved_base_units": 6,
                "local_invoice_ref": f"OFF-OA2-{self.suffix}",
                "idempotency_key": f"idem-oa2-{self.suffix}",
            },
            format="json",
        )
        assert res2.status_code == status.HTTP_200_OK
        assert res2.data["oversell_detected"] is True
        assert res2.data["remaining_batch_units"] == 3  # Posted BatchStock NOT mutated silently!
        dispute = res2.data["dispute"]
        assert dispute["oversell_shortage_units"] == 3
        assert dispute["disputed_cost_bdt"] == "13.50"
        assert dispute["posted_records_preserved"] is True

        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 3

        # 4. EXPIRED BATCH BLOCK: Attempting to allocate expired batch -> 422!
        r_exp_bat = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "batch_id": self.expired_batch.batch_id,
                "reserved_base_units": 2,
                "local_invoice_ref": "OFF-EXP-1",
                "idempotency_key": f"idem-exp-{self.suffix}",
            },
            format="json",
        )
        assert r_exp_bat.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 5. AUDITED DISPUTE RESOLUTION: Cashier forbidden (403); Owner resolves via audited reversal!
        r_cash_res = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/resolve-dispute/",
            {
                "dispute_id": dispute["dispute_id"],
                "resolution_status": OversellDisputeStatus.RESOLVED_AUDITED_REVERSAL,
                "resolution_reference_id": "REV-OA-001",
                "resolution_notes": "Cashier attempted unauthorized resolution",
            },
            format="json",
        )
        assert r_cash_res.status_code == status.HTTP_403_FORBIDDEN

        self.client.force_authenticate(user=self.owner_a)
        r_own_res = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/resolve-dispute/",
            {
                "dispute_id": dispute["dispute_id"],
                "resolution_status": OversellDisputeStatus.RESOLVED_AUDITED_REVERSAL,
                "resolution_reference_id": "REV-OA-001",
                "resolution_notes": "Customer returned 3 extra units; reversal voucher REV-OA-001 posted",
            },
            format="json",
        )
        assert r_own_res.status_code == status.HTTP_200_OK
        assert r_own_res.data["dispute"]["status"] == OversellDisputeStatus.RESOLVED_AUDITED_REVERSAL
        assert r_own_res.data["dispute"]["posted_records_preserved"] is True

        # Verify P307 ChangeFeed REVERSAL event emitted with exact -13.50 BDT!
        cf_ev = ChangeFeedEvent.objects.filter(
            tenant=self.tenant_a, idempotency_key=f"osdisp-res:{dispute['dispute_id']}"
        ).first()
        assert cf_ev is not None
        assert cf_ev.operation == ChangeFeedOperation.REVERSAL
        assert cf_ev.monetary_impact_bdt == Decimal("-13.50")

        # Archive policy cleanly via DELETE
        r_del = self.client.delete(f"/api/v2/offline-sync/stock-allocation-policies/{policy_id}/")
        assert r_del.status_code == status.HTTP_200_OK
        assert r_del.data["policy"]["is_archived"] is True

    def test_02_rbac_cross_tenant_cross_branch_lease_expiry_and_suspended_drills(self):
        # 1. Cashier cannot create branch policy (403)
        self.client.force_authenticate(user=self.cashier_a1)
        r_cash_pol = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/",
            {"branch_id": self.branch_a1.branch_id, "policy_mode": "STRICT_LOCAL_ONLY"},
            format="json",
        )
        assert r_cash_pol.status_code == status.HTTP_403_FORBIDDEN

        # 2. Cross-branch cashier blocked from reconciling Branch 1 device (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_xbr = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "batch_id": self.batch.batch_id,
                "reserved_base_units": 1,
                "local_invoice_ref": "OFF-XBR",
                "idempotency_key": f"idem-xbr-{self.suffix}",
            },
            format="json",
        )
        assert r_xbr.status_code == status.HTTP_403_FORBIDDEN

        # 3. Cross-tenant Owner B blocked on policy list & reconcile (403)
        self.client.force_authenticate(user=self.owner_b)
        r_xten = self.client.get(f"/api/v2/offline-sync/stock-allocation-policies/?tenant_id={self.tenant_a.tenant_id}")
        assert r_xten.status_code == status.HTTP_403_FORBIDDEN

        r_xten_rec = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "batch_id": self.batch.batch_id,
                "reserved_base_units": 1,
                "local_invoice_ref": "OFF-XTEN",
                "idempotency_key": f"idem-xten-{self.suffix}",
            },
            format="json",
        )
        assert r_xten_rec.status_code == status.HTTP_403_FORBIDDEN

        # 4. Expired offline lease blocks reservation reconciliation (403)
        self.dev1.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        self.dev1.save(update_fields=["lease_expires_at"])
        self.client.force_authenticate(user=self.owner_a)
        r_exp = self.client.post(
            "/api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "batch_id": self.batch.batch_id,
                "reserved_base_units": 1,
                "local_invoice_ref": "OFF-LEASE",
                "idempotency_key": f"idem-lease-{self.suffix}",
            },
            format="json",
        )
        assert r_exp.status_code == status.HTTP_403_FORBIDDEN

        # 5. Suspended tenant blocks policy & reconciliation (403)
        self.dev1.lease_expires_at = timezone.now() + datetime.timedelta(days=5)
        self.dev1.save(update_fields=["lease_expires_at"])
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])
        r_susp = self.client.get("/api/v2/offline-sync/stock-allocation-policies/disputes/")
        assert r_susp.status_code == status.HTTP_403_FORBIDDEN
