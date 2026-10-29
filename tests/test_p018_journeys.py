"""
Unit and Contract Tests for ADR-0018: UX User Journeys & Interaction Paths
Verifies:
1. End-to-end execution of all 6 canonical UX journeys (Owner Onboarding, Cashier Checkout, Pharmacist Verification, GRN Receipt, Expense Approval, Super-Admin Support).
2. Online vs Offline Cashier Checkout variants (offline receipt watermark banner).
3. Prescription-required checkout validation gate.
4. Float payment rejection (Strict Decimal rule).
5. Offline attempt rejection for cloud-mandatory journeys.
6. Role authorization at initiation and per-step level (SoD enforcement).
7. Cross-tenant isolation enforcement.
"""

import unittest
from decimal import Decimal

from apps.core.errors import FieldValidationError
from apps.core.journeys import (
    JourneyId,
    UserJourneyEngine,
)


class TestUXUserJourneys(unittest.TestCase):
    def setUp(self):
        self.engine = UserJourneyEngine()
        self.tenant_id = "tenant-apollo-dhaka"

    def test_all_six_journeys_execute_end_to_end(self):
        # 1. Owner Onboarding
        j1 = self.engine.start_journey(
            JourneyId.OWNER_ONBOARDING,
            self.tenant_id,
            "TENANT_OWNER",
            "user-owner-01",
        )
        j1 = self.engine.execute_step(j1, {"phone_number": "+8801711000000", "otp_code": "123456"})
        j1 = self.engine.execute_step(j1, {"drug_license_no": "DL-DHK-2026-001", "tin_number": "1234567890"})
        j1 = self.engine.execute_step(j1, {"branch_name": "Main Dhanmondi Branch", "address": "House 1, Road 2, Dhaka"})
        j1 = self.engine.execute_step(j1, {"plan_tier": "STANDARD"})
        self.assertEqual(j1.status, "COMPLETED")
        self.assertEqual(len(j1.completed_steps), 4)

        # 2. Cashier POS Checkout (Online)
        j2 = self.engine.start_journey(
            JourneyId.CASHIER_CHECKOUT,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
            is_offline=False,
        )
        j2 = self.engine.execute_step(j2, {"product_id": "prod-napa-500", "quantity": 10, "unit_type": "Tablet"})
        j2 = self.engine.execute_step(j2, {"is_rx_required": False})
        j2 = self.engine.execute_step(j2, {"tender_type": "CASH", "amount_paid": Decimal("25.00")})
        j2 = self.engine.execute_step(j2, {"receipt_format": "THERMAL_80MM"})
        self.assertEqual(j2.status, "COMPLETED")
        self.assertNotIn("offline_watermark", j2.step_data["STEP-04-PRINT-RECEIPT"])

        # 3. Pharmacist Clinical Verification
        j3 = self.engine.start_journey(
            JourneyId.PHARMACIST_VERIFICATION,
            self.tenant_id,
            "PHARMACIST",
            "user-pharm-01",
        )
        j3 = self.engine.execute_step(j3, {"rx_image_ref": "s3://rx/rx-992.jpg", "patient_name": "Mrs. Ayesha"})
        j3 = self.engine.execute_step(j3, {"doctor_bmdc_no": "BMDC-A-9812", "doctor_name": "Dr. Kamal"})
        j3 = self.engine.execute_step(j3, {"pharmacist_pcb_no": "PCB-1082", "clinical_decision": "VERIFIED_VALID"})
        self.assertEqual(j3.status, "COMPLETED")

        # 4. GRN Receipt
        j4 = self.engine.start_journey(
            JourneyId.GRN_RECEIPT,
            self.tenant_id,
            "INVENTORY_STAFF",
            "user-inv-01",
        )
        j4 = self.engine.execute_step(j4, {"po_number": "PO-100", "challan_number": "CH-550"})
        j4 = self.engine.execute_step(
            j4, {"batch_number": "SQ-B12", "expiry_date": "2028-12-31", "received_cost": Decimal("5000.00")}
        )
        j4 = self.engine.execute_step(j4, {"storage_location": "SHELF-A2"})
        self.assertEqual(j4.status, "COMPLETED")

        # 5. Shift Expense Approval (SoD: Cashier requests, Manager approves)
        j5 = self.engine.start_journey(
            JourneyId.EXPENSE_APPROVAL,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
        )
        j5 = self.engine.execute_step(
            j5, {"category": "Tea & Coffee", "amount": Decimal("150.00"), "reason": "Staff shift snacks"}
        )
        # Branch manager approves
        j5 = self.engine.execute_step(
            j5, {"approved": True, "manager_auth_code": "MGR-PIN-99"}, actor_role="BRANCH_MANAGER"
        )
        self.assertEqual(j5.status, "COMPLETED")

        # 6. Super Admin Support Session
        j6 = self.engine.start_journey(
            JourneyId.SUPER_ADMIN_SUPPORT,
            self.tenant_id,
            "PLATFORM_SUPER_ADMIN",
            "super-user-01",
        )
        j6 = self.engine.execute_step(j6, {"support_ticket_id": "TICKET-771", "tenant_id": self.tenant_id})
        j6 = self.engine.execute_step(j6, {"action_scope": "SYNC_TELEMETRY_DIAGNOSTICS"})
        j6 = self.engine.execute_step(j6, {"resolution_notes": "Outbox replay backlog cleared."})
        self.assertEqual(j6.status, "COMPLETED")

    def test_offline_cashier_checkout_watermark(self):
        j_off = self.engine.start_journey(
            JourneyId.CASHIER_CHECKOUT,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
            is_offline=True,
        )
        j_off = self.engine.execute_step(j_off, {"product_id": "prod-napa-500", "quantity": 10, "unit_type": "Tablet"})
        j_off = self.engine.execute_step(j_off, {"is_rx_required": False})
        j_off = self.engine.execute_step(j_off, {"tender_type": "CASH", "amount_paid": Decimal("25.00")})
        j_off = self.engine.execute_step(j_off, {"receipt_format": "THERMAL_80MM"})
        self.assertEqual(j_off.status, "COMPLETED")
        receipt_data = j_off.step_data["STEP-04-PRINT-RECEIPT"]
        self.assertIn("offline_watermark", receipt_data)
        self.assertEqual(receipt_data["offline_watermark"], "*** OFFLINE RECEIPT - PENDING CLOUD CONFIRMATION ***")

    def test_prescription_required_checkout_gate(self):
        j_rx = self.engine.start_journey(
            JourneyId.CASHIER_CHECKOUT,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
        )
        j_rx = self.engine.execute_step(j_rx, {"product_id": "prod-cipro-500", "quantity": 10, "unit_type": "Tablet"})
        # Attempting checkout with is_rx_required=True but missing rx_verification_id
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.execute_step(j_rx, {"is_rx_required": True})
        self.assertIn("rx_verification_id", ctx.exception.field_errors)

    def test_float_money_rejected_in_checkout(self):
        j = self.engine.start_journey(
            JourneyId.CASHIER_CHECKOUT,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
        )
        j = self.engine.execute_step(j, {"product_id": "prod-napa-500", "quantity": 10, "unit_type": "Tablet"})
        j = self.engine.execute_step(j, {"is_rx_required": False})
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.execute_step(j, {"tender_type": "CASH", "amount_paid": 25.50})  # Float rejected!
        self.assertIn("amount_paid", ctx.exception.field_errors)

    def test_cloud_mandatory_journey_rejects_offline_start(self):
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.start_journey(
                JourneyId.OWNER_ONBOARDING,
                self.tenant_id,
                "TENANT_OWNER",
                "user-owner-01",
                is_offline=True,  # Owner onboarding cannot run offline
            )
        self.assertIn("is_offline", ctx.exception.field_errors)

    def test_role_authorization_and_sod_enforcement(self):
        # 1. Cashier trying to start Owner Onboarding
        with self.assertRaises(PermissionError) as ctx:
            self.engine.start_journey(
                JourneyId.OWNER_ONBOARDING,
                self.tenant_id,
                "CASHIER",
                "user-cashier-01",
            )
        self.assertIn("not authorized to start journey", str(ctx.exception))

        # 2. Cashier trying to approve their own expense in Step 2 (SoD violation)
        j_exp = self.engine.start_journey(
            JourneyId.EXPENSE_APPROVAL,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
        )
        j_exp = self.engine.execute_step(j_exp, {"category": "Supplies", "amount": Decimal("50.00"), "reason": "Pens"})
        with self.assertRaises(PermissionError) as ctx:
            self.engine.execute_step(j_exp, {"approved": True, "manager_auth_code": "FAKE"}, actor_role="CASHIER")
        self.assertIn("not authorized to execute step", str(ctx.exception))

    def test_cross_tenant_isolation(self):
        j = self.engine.start_journey(
            JourneyId.CASHIER_CHECKOUT,
            "tenant-alpha",
            "CASHIER",
            "cashier-1",
        )
        with self.assertRaises(PermissionError) as ctx:
            self.engine.validate_tenant_boundary(j, "tenant-beta")
        self.assertIn("Cross-tenant journey operation blocked", str(ctx.exception))

    def test_owner_onboarding_invalid_formats_rejection(self):
        j = self.engine.start_journey(
            JourneyId.OWNER_ONBOARDING,
            self.tenant_id,
            "TENANT_OWNER",
            "user-owner-01",
        )
        # Invalid phone format
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.execute_step(j, {"phone_number": "12345", "otp_code": "123456"})
        self.assertIn("phone_number", ctx.exception.field_errors)

        # Valid phone, advance to step 2
        j = self.engine.execute_step(j, {"phone_number": "+8801711000000", "otp_code": "123456"})

        # Invalid drug license format (missing DL- prefix)
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.execute_step(j, {"drug_license_no": "INVALID-1234", "tin_number": "99999"})
        self.assertIn("drug_license_no", ctx.exception.field_errors)

    def test_grn_short_dated_batch_waiver_logic(self):
        j = self.engine.start_journey(
            JourneyId.GRN_RECEIPT,
            self.tenant_id,
            "INVENTORY_STAFF",
            "user-inv-01",
        )
        j = self.engine.execute_step(j, {"po_number": "PO-100", "challan_number": "CH-550"})

        # Short-dated batch without manager waiver
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.execute_step(
                j,
                {
                    "batch_number": "SQ-B12",
                    "expiry_date": "2026-06-30",
                    "received_cost": Decimal("5000.00"),
                    "is_short_dated": True,
                    "manager_short_dated_waiver": False,
                },
            )
        self.assertIn("manager_short_dated_waiver", ctx.exception.field_errors)

        # Short-dated batch WITH manager waiver
        j = self.engine.execute_step(
            j,
            {
                "batch_number": "SQ-B12",
                "expiry_date": "2026-06-30",
                "received_cost": Decimal("5000.00"),
                "is_short_dated": True,
                "manager_short_dated_waiver": True,
            },
        )
        self.assertEqual(len(j.completed_steps), 2)

    def test_expense_exceeds_cash_drawer_float_rejection(self):
        j = self.engine.start_journey(
            JourneyId.EXPENSE_APPROVAL,
            self.tenant_id,
            "CASHIER",
            "user-cashier-01",
        )
        # Cashier requests 500 BDT
        j = self.engine.execute_step(
            j, {"category": "Postage", "amount": Decimal("500.00"), "reason": "Urgent courier"}
        )

        # Manager attempts approval with drawer float having only 200 BDT
        with self.assertRaises(FieldValidationError) as ctx:
            self.engine.execute_step(
                j,
                {
                    "approved": True,
                    "manager_auth_code": "MGR-01",
                    "cash_drawer_balance": Decimal("200.00"),  # Less than 500!
                },
                actor_role="BRANCH_MANAGER",
            )
        self.assertIn("amount", ctx.exception.field_errors)

    def test_pharmacist_clinical_refusal_audit_flow(self):
        j = self.engine.start_journey(
            JourneyId.PHARMACIST_VERIFICATION,
            self.tenant_id,
            "PHARMACIST",
            "user-pharm-01",
        )
        j = self.engine.execute_step(j, {"rx_image_ref": "s3://rx/rx-fraud.jpg", "patient_name": "Suspicious Rx"})
        j = self.engine.execute_step(j, {"doctor_bmdc_no": "BMDC-INVALID", "doctor_name": "Unverified Doctor"})
        # Pharmacist records clinical refusal
        j = self.engine.execute_step(
            j,
            {
                "pharmacist_pcb_no": "PCB-1082",
                "clinical_decision": "REJECTED_UNSAFE",
            },
        )
        self.assertEqual(j.status, "COMPLETED")
        self.assertEqual(j.step_data["STEP-03-CLINICAL-SIGNOFF"]["clinical_decision"], "REJECTED_UNSAFE")


if __name__ == "__main__":
    unittest.main()
