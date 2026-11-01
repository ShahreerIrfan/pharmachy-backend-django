"""
backend/tests/test_p094_offline_plan_lapse.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0033, ADR-0072, ADR-0082, ADR-0092 & ADR-0094
Phase 05: Basic/Standard/Pro Subscription & Billing (P094)
Requirement Trace: FR-072 (Do not silently discard valid pending events if plan lapses)

Comprehensive Integration Tests for Offline Plan Lapse Handling & Reconciliation Queue:
1. Valid Scenario: Safely ingests offline transaction during plan lapse into hold queue (HTTP 202 Accepted).
2. Idempotency on Replay: Re-pushing existing offline_event_id returns existing record without duplication.
3. Incomplete / Invalid Input: Rejects short offline_event_id and invalid branch_id (HTTP 400).
4. Cross-Tenant Barrier: Blocks Tenant B from pushing or reconciling Tenant A transactions (HTTP 403).
5. Role-Based Access Control (RBAC): Allows cashiers to push events; blocks cashiers from reconciling (HTTP 403).
6. Authorized Single Reconciliation: Pharmacy owner reconciles held event into PostgreSQL SaleInvoice.
7. Bulk Auto-Reconciliation on Renewal: Settle payment auto-reconciles all pending held transactions in order.
8. Queue Summary & Bilingual Messaging: Accurate count, Decimal totals, and EN/BN banners.
9. Zero Data Loss Invariant: Confirms 0 historical records, branches, or audit logs are deleted.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    HoldQueueStatus,
    LapsedSubscriptionHoldQueue,
    PlanTier,
    SaleInvoice,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.offline_lapse_service import OfflinePlanLapseReconciliationService
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP094OfflinePlanLapseIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p094_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (Primary subject with lapsed plan)
        cls.subdomain_a = f"lapse-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-lapse-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P094-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@lapsea.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Lapse Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Branch Mirpur",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)
        cls.branch_a = Branch.objects.filter(tenant=cls.tenant_a).first()

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_a,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

        # Cashier user for Tenant A
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email=f"cashier_a_{cls.suffix}@lapsea.local",
            defaults={
                "full_name": f"Cashier A {cls.suffix}",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_id_a,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
            },
        )

        # 3. Provision Tenant B (Isolation boundary)
        cls.subdomain_b = f"lapse-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-lapse-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P094-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@lapseb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Isolation Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Branch Uttara",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)
        cls.branch_b = Branch.objects.filter(tenant=cls.tenant_b).first()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        # Set tenant A subscription to EXPIRED to test offline plan lapse intake
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        if sub:
            sub.status = SubscriptionStatus.EXPIRED
            sub.save()
        self.tenant_a.status = TenantLifecycleStatus.ACTIVE
        self.tenant_a.save()
        TenantAppliedEntitlement.objects.filter(tenant=self.tenant_a).update(is_active=True)

    def test_01_valid_offline_sale_held_during_plan_lapse(self):
        """Valid Scenario: Terminal pushes offline sale during plan lapse; safely held without data drop."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/offline-reconciliation/push/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "branch_id": self.branch_a.branch_id,
            "device_id": "pos-workstation-counter-01",
            "offline_event_id": f"evt-offline-{uuid.uuid4().hex[:8]}",
            "event_type": "OFFLINE_SALE",
            "occurred_at": "2026-11-01T14:30:00Z",
            "payload": {
                "invoice_number": f"INV-OFF-{uuid.uuid4().hex[:6].upper()}",
                "customer_name": "Rahim Ahmed",
                "customer_phone": "+8801711223344",  # synthetic-fixture
                "total_amount": "2750.50",
                "items": [
                    {"product_name": "Paracetamol 500mg", "qty": 20, "unit_price": "2.50"},
                    {"product_name": "Amoxicillin 250mg", "qty": 14, "unit_price": "15.00"},
                ],
            },
        }

        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_202_ACCEPTED)
        data = res.json()

        # Invariants Check
        self.assertTrue(data["hold_id"].startswith("hld-"))
        self.assertEqual(data["hold_status"], HoldQueueStatus.HELD_PENDING_RECONCILIATION)
        self.assertEqual(data["financial_total_bdt"], "2750.50")
        self.assertEqual(data["subscription_status_at_intake"], "EXPIRED")
        self.assertTrue(data["invariants"]["never_silently_discarded"])
        self.assertTrue(data["invariants"]["zero_data_deleted"])
        self.assertIn("রিকনসিলিয়েশনের অপেক্ষায় জমা", data["client_messaging"]["headline_bn"])

        # Audit Log Check
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action="OFFLINE_TRANSACTION_HELD_FOR_RECONCILIATION",
                resource_id=data["hold_id"],
                tenant_id=self.tenant_id_a,
            ).exists()
        )

    def test_02_idempotency_on_replay(self):
        """Replaying identical (tenant, offline_event_id) returns existing record without duplicates."""
        event_id = f"evt-idempotent-{uuid.uuid4().hex[:8]}"

        # First intake
        res_1 = OfflinePlanLapseReconciliationService.intake_offline_event(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-dev-01",
            offline_event_id=event_id,
            payload={"total_amount": "1000.00"},
            actor_user=self.owner_a,
        )
        self.assertFalse(res_1["is_duplicate"])
        hold_id_1 = res_1["hold_id"]

        # Second intake (simulated network retry)
        res_2 = OfflinePlanLapseReconciliationService.intake_offline_event(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-dev-01",
            offline_event_id=event_id,
            payload={"total_amount": "1000.00"},
            actor_user=self.owner_a,
        )
        self.assertTrue(res_2["is_duplicate"])
        self.assertEqual(res_2["hold_id"], hold_id_1)

        # Database count remains strictly 1
        count = LapsedSubscriptionHoldQueue.objects.filter(
            tenant=self.tenant_a,
            offline_event_id=event_id,
        ).count()
        self.assertEqual(count, 1)

    def test_03_invalid_or_incomplete_inputs_rejected(self):
        """Rejects short offline_event_id (<3 chars) and invalid branch ID."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/offline-reconciliation/push/"

        # Short offline event ID
        res_short = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "branch_id": self.branch_a.branch_id, "offline_event_id": "ev"},
            format="json",
        )
        self.assertEqual(res_short.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_short.json()["error"], "VALIDATION_ERROR")

        # Invalid branch ID
        res_bad_br = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "branch_id": "br-nonexistent", "offline_event_id": "evt-valid-01"},
            format="json",
        )
        self.assertEqual(res_bad_br.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_br.json()["error"], "VALIDATION_ERROR")

    def test_04_cross_tenant_isolation_barrier(self):
        """Tenant B user is blocked from pushing or reconciling Tenant A transactions."""
        self.client.force_authenticate(user=self.owner_b)

        # 1. Push attempt for Tenant A branch
        res_push = self.client.post(
            "/api/v2/billing/offline-reconciliation/push/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "rogue-dev",
                "offline_event_id": "evt-cross-01",
            },
            format="json",
        )
        self.assertEqual(res_push.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_push.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. Reconcile attempt for Tenant A record
        record_a = OfflinePlanLapseReconciliationService.intake_offline_event(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-dev-01",
            offline_event_id=f"evt-a-{uuid.uuid4().hex[:6]}",
            payload={"total_amount": "500.00"},
            actor_user=self.owner_a,
        )
        res_rec = self.client.post(
            f"/api/v2/billing/offline-reconciliation/{record_a['hold_id']}/reconcile/",
            {"resolution_notes": "Rogue cross-tenant reconcile attempt"},
            format="json",
        )
        self.assertEqual(res_rec.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_rec.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_05_rbac_cashier_can_push_cannot_reconcile(self):
        """Cashier can push offline outbox events, but cannot reconcile queue records."""
        self.client.force_authenticate(user=self.cashier_a)

        # 1. Cashier pushes offline event -> 202 Accepted
        res_push = self.client.post(
            "/api/v2/billing/offline-reconciliation/push/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "pos-cashier-lane-01",
                "offline_event_id": f"evt-cashier-{uuid.uuid4().hex[:6]}",
                "payload": {"total_amount": "850.00"},
            },
            format="json",
        )
        self.assertEqual(res_push.status_code, status.HTTP_202_ACCEPTED)
        hold_id = res_push.json()["hold_id"]

        # 2. Cashier attempts to reconcile -> 403 Forbidden
        res_rec = self.client.post(
            f"/api/v2/billing/offline-reconciliation/{hold_id}/reconcile/",
            {"resolution_notes": "Cashier unauthorized reconcile"},
            format="json",
        )
        self.assertEqual(res_rec.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_rec.json()["error"], "ERR_PERMISSION_DENIED")

    def test_06_authorized_maker_checker_reconciliation(self):
        """Pharmacy owner reconciles a held offline event into authoritative PostgreSQL SaleInvoice."""
        record = OfflinePlanLapseReconciliationService.intake_offline_event(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-dev-01",
            offline_event_id=f"evt-rec-{uuid.uuid4().hex[:6]}",
            payload={
                "invoice_number": f"INV-AUTH-{uuid.uuid4().hex[:6].upper()}",
                "customer_name": "Tariqul Islam",
                "total_amount": "3200.00",
            },
            actor_user=self.owner_a,
        )
        hold_id = record["hold_id"]

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            f"/api/v2/billing/offline-reconciliation/{hold_id}/reconcile/",
            {"resolution_notes": "Verified against physical paper receipt and settled."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["hold_status"], HoldQueueStatus.RECONCILED)
        self.assertIsNotNone(data["reconciliation_target_id"])
        self.assertIsNotNone(data["reconciled_at"])
        self.assertEqual(data["reconciled_by"], self.owner_a.email)

        # Confirm PostgreSQL SaleInvoice created
        inv = SaleInvoice.objects.get(invoice_id=data["reconciliation_target_id"])
        self.assertEqual(inv.total_amount, Decimal("3200.00"))
        self.assertEqual(inv.tenant_id, self.tenant_id_a)

    def test_07_bulk_auto_reconciliation_on_renewal(self):
        """Bulk auto-reconciliation successfully reconciles all pending held events for tenant."""
        for i in range(3):
            OfflinePlanLapseReconciliationService.intake_offline_event(
                tenant_id=self.tenant_id_a,
                branch_id=self.branch_a.branch_id,
                device_id="pos-dev-bulk",
                offline_event_id=f"evt-bulk-{i}-{uuid.uuid4().hex[:4]}",
                payload={"total_amount": "500.00"},
                actor_user=self.owner_a,
            )

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/offline-reconciliation/reconcile-all/",
            {"tenant_id": self.tenant_id_a},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertGreaterEqual(data["reconciled_count"], 3)
        self.assertGreaterEqual(Decimal(data["total_reconciled_bdt"]), Decimal("1500.00"))

        # Queue check
        summary = OfflinePlanLapseReconciliationService.get_queue_summary(self.tenant_id_a)
        self.assertEqual(summary["pending_count"], 0)

    def test_08_queue_summary_and_bilingual_messaging(self):
        """Validates queue summary, pending count, financial sum, and bilingual text."""
        OfflinePlanLapseReconciliationService.intake_offline_event(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-dev-summary",
            offline_event_id=f"evt-sum-{uuid.uuid4().hex[:6]}",
            payload={"total_amount": "1450.00"},
            actor_user=self.owner_a,
        )

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get(f"/api/v2/billing/offline-reconciliation/queue/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertGreaterEqual(data["pending_count"], 1)
        self.assertGreater(Decimal(data["total_pending_amount_bdt"]), Decimal("0.00"))
        self.assertIn("রিকনসিলিয়েশনের জন্য জমা রয়েছে", data["client_messaging"]["headline_bn"])
        self.assertTrue(data["invariants"]["never_silently_discarded"])

    def test_09_zero_data_loss_invariant(self):
        """Confirms offline plan lapse handling never deletes branches, invoices, or audit records."""
        branch_count_before = Branch.objects.filter(tenant=self.tenant_a).count()
        audit_count_before = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        rec = OfflinePlanLapseReconciliationService.intake_offline_event(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-retention-dev",
            offline_event_id=f"evt-retention-{uuid.uuid4().hex[:6]}",
            payload={"total_amount": "700.00"},
            actor_user=self.owner_a,
        )
        OfflinePlanLapseReconciliationService.reconcile_single_event(
            hold_id=rec["hold_id"],
            actor_user=self.owner_a,
        )

        branch_count_after = Branch.objects.filter(tenant=self.tenant_a).count()
        audit_count_after = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        self.assertEqual(branch_count_before, branch_count_after, "Branches must never be deleted")
        self.assertGreater(audit_count_after, audit_count_before, "Audits must be created, never deleted")


if __name__ == "__main__":
    unittest.main()
