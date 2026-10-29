"""
Unit tests for P013 - Canonical Status Vocabulary and Finite State Machine Engine
Validates:
1. One valid scenario: Legitimate state transitions and audited reversals for sales, batches, prescriptions, POs, and sync ops.
2. One invalid/incomplete input case: Illegal state transitions rejected with InvalidStateTransitionError and immutability violation.
3. One unauthorized/cross-tenant case: Role-restricted actions (e.g. cashier reversal) and cross-tenant isolation.
4. Source-specific business rule: Posted documents are immutable; audit log records every transition.
"""

import unittest

from apps.core.lifecycle import (
    BatchStatus,
    ImmutableRecordModificationError,
    InvalidStateTransitionError,
    LifecycleEngine,
    PrescriptionStatus,
    PurchaseOrderStatus,
    SaleStatus,
    SyncOpStatus,
)


class TestCanonicalStatusVocabulary(unittest.TestCase):
    """Verifies state machine transitions, immutability, and security bounds."""

    def setUp(self):
        self.engine = LifecycleEngine()

    def test_valid_scenario_sale_and_batch_lifecycle(self):
        """Acceptance Criteria: One valid scenario. Demonstrates happy-path transitions and audit trail."""
        # 1. Cashier creates draft sale and moves to PENDING_SYNC
        audit_1 = self.engine.transition_sale(
            entity_id="INV-2026-0001",
            current_state=SaleStatus.DRAFT,
            target_state=SaleStatus.PENDING_SYNC,
            actor_role="CASHIER",
            actor_id="user-cashier-101",
            actor_tenant_id="tenant-dhaka-001",
            entity_tenant_id="tenant-dhaka-001",
            reason="Cash sale finalized offline",
        )
        self.assertEqual(audit_1.to_state, "PENDING_SYNC")
        self.assertEqual(audit_1.actor_role, "CASHIER")

        # 2. Signed device syncs to cloud and receives SERVER_ACCEPTED
        audit_2 = self.engine.transition_sale(
            entity_id="INV-2026-0001",
            current_state=SaleStatus.PENDING_SYNC,
            target_state=SaleStatus.SERVER_ACCEPTED,
            actor_role="SIGNED_DEVICE",
            actor_id="device-pos-01",
            actor_tenant_id="tenant-dhaka-001",
            entity_tenant_id="tenant-dhaka-001",
            reason="Server batch sync ACK verified",
        )
        self.assertEqual(audit_2.to_state, "SERVER_ACCEPTED")

        # 3. Branch Manager executes audited reversal
        audit_3 = self.engine.transition_sale(
            entity_id="INV-2026-0001",
            current_state=SaleStatus.SERVER_ACCEPTED,
            target_state=SaleStatus.REVERSED,
            actor_role="BRANCH_MANAGER",
            actor_id="user-mgr-201",
            actor_tenant_id="tenant-dhaka-001",
            entity_tenant_id="tenant-dhaka-001",
            reason="Customer returned sealed item with receipt within 24h",
        )
        self.assertEqual(audit_3.to_state, "REVERSED")

        # 4. Batch clinical quarantine by pharmacist
        audit_4 = self.engine.transition_batch(
            entity_id="BATCH-NAPA-2026",
            current_state=BatchStatus.ACTIVE,
            target_state=BatchStatus.QUARANTINED,
            actor_role="PHARMACIST",
            actor_id="user-pharm-301",
            actor_tenant_id="tenant-dhaka-001",
            entity_tenant_id="tenant-dhaka-001",
            reason="DGDA batch recall notice alert",
        )
        self.assertEqual(audit_4.to_state, "QUARANTINED")

        # 5. Purchase Order workflow: SUBMITTED -> APPROVED -> RECEIVED
        po_audit = self.engine.transition_purchase_order(
            entity_id="PO-2026-001",
            current_state=PurchaseOrderStatus.DRAFT,
            target_state=PurchaseOrderStatus.SUBMITTED,
            actor_role="PURCHASER",
            actor_id="user-purchaser-401",
            actor_tenant_id="tenant-dhaka-001",
            entity_tenant_id="tenant-dhaka-001",
            reason="Submitted for manager approval",
        )
        self.assertEqual(po_audit.to_state, "SUBMITTED")

        # 6. Outbox sync operation: QUEUED -> SENDING -> SERVER_ACCEPTED
        sync_audit = self.engine.transition_sync_op(
            entity_id="OP-UUID-001",
            current_state=SyncOpStatus.QUEUED,
            target_state=SyncOpStatus.SENDING,
            actor_role="SIGNED_DEVICE",
            actor_id="device-pos-01",
            actor_tenant_id="tenant-dhaka-001",
            entity_tenant_id="tenant-dhaka-001",
            reason="Network active, dispatching payload",
        )
        self.assertEqual(sync_audit.to_state, "SENDING")

        # Verify audit log length
        self.assertEqual(len(self.engine.audit_log), 6)

    def test_invalid_state_transition_rejection(self):
        """Acceptance Criteria: One invalid or incomplete input case."""
        # 1. Illegal transition: Cannot revert SERVER_ACCEPTED invoice back to DRAFT
        with self.assertRaises(InvalidStateTransitionError) as ctx:
            self.engine.transition_sale(
                entity_id="INV-2026-0002",
                current_state=SaleStatus.SERVER_ACCEPTED,
                target_state=SaleStatus.DRAFT,
                actor_role="BRANCH_MANAGER",
                actor_id="user-mgr-201",
                actor_tenant_id="tenant-dhaka-001",
                entity_tenant_id="tenant-dhaka-001",
                reason="Attempting illegal modification",
            )
        self.assertIn("Illegal sale transition", str(ctx.exception))

        # 2. Illegal transition: Cannot transition EXPIRED batch back to ACTIVE
        with self.assertRaises(InvalidStateTransitionError) as ctx:
            self.engine.transition_batch(
                entity_id="BATCH-OLD-999",
                current_state=BatchStatus.EXPIRED,
                target_state=BatchStatus.ACTIVE,
                actor_role="PHARMACIST",
                actor_id="user-pharm-301",
                actor_tenant_id="tenant-dhaka-001",
                entity_tenant_id="tenant-dhaka-001",
                reason="Attempting to resell expired medicine",
            )
        self.assertIn("Illegal batch transition", str(ctx.exception))

        # 3. Illegal transition: Cannot dispense prescription directly from NEW (must be VERIFIED first)
        with self.assertRaises(InvalidStateTransitionError) as ctx:
            self.engine.transition_prescription(
                entity_id="RX-2026-0001",
                current_state=PrescriptionStatus.NEW,
                target_state=PrescriptionStatus.DISPENSED,
                actor_role="PHARMACIST",
                actor_id="user-pharm-301",
                actor_tenant_id="tenant-dhaka-001",
                entity_tenant_id="tenant-dhaka-001",
                reason="Skipping pharmacist verification step",
            )
        self.assertIn("Illegal prescription transition", str(ctx.exception))

        # 4. Immutability Violation: Cannot directly update or delete SERVER_ACCEPTED record
        with self.assertRaises(ImmutableRecordModificationError) as ctx:
            self.engine.assert_record_mutable("SaleInvoice", "SERVER_ACCEPTED")
        self.assertIn("is immutable and cannot be directly updated or deleted", str(ctx.exception))

    def test_unauthorized_and_cross_tenant_state_transition_blocked(self):
        """Acceptance Criteria: One unauthorized or cross-tenant case."""
        # 1. Unauthorized: Cashier tries to reverse a SERVER_ACCEPTED invoice -> Blocked!
        with self.assertRaises(PermissionError) as ctx:
            self.engine.transition_sale(
                entity_id="INV-2026-0003",
                current_state=SaleStatus.SERVER_ACCEPTED,
                target_state=SaleStatus.REVERSED,
                actor_role="CASHIER",  # Cashier lacks reversal permission
                actor_id="user-cashier-101",
                actor_tenant_id="tenant-dhaka-001",
                entity_tenant_id="tenant-dhaka-001",
                reason="Self-approved refund",
            )
        self.assertIn("Role 'CASHIER' is not authorized", str(ctx.exception))

        # 2. Cross-tenant: Dhaka staff tries to quarantine Chittagong batch -> Blocked!
        with self.assertRaises(PermissionError) as ctx:
            self.engine.transition_batch(
                entity_id="BATCH-CTG-001",
                current_state=BatchStatus.ACTIVE,
                target_state=BatchStatus.QUARANTINED,
                actor_role="PHARMACIST",
                actor_id="user-pharm-dhaka",
                actor_tenant_id="tenant-dhaka-001",
                entity_tenant_id="tenant-ctg-002",  # Different tenant!
                reason="Malicious cross-tenant quarantine",
            )
        self.assertIn("Cross-tenant operation rejected", str(ctx.exception))

        # 3. Platform Super Admin can override tenant boundaries
        admin_audit = self.engine.transition_sale(
            entity_id="INV-2026-9999",
            current_state=SaleStatus.PENDING_SYNC,
            target_state=SaleStatus.SERVER_ACCEPTED,
            actor_role="PLATFORM_SUPER_ADMIN",
            actor_id="super-admin-01",
            actor_tenant_id="platform",
            entity_tenant_id="tenant-ctg-002",
            reason="Emergency reconciliation force sync",
        )
        self.assertEqual(admin_audit.to_state, "SERVER_ACCEPTED")


if __name__ == "__main__":
    unittest.main()
