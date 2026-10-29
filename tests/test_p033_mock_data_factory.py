"""
tests/test_p033_mock_data_factory.py
Architecture Decision: ADR-0001, ADR-0005, ADR-0010, ADR-0015, ADR-0017 & ADR-0033
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Deterministic Mock Data Factory & Zero Real Patient PHI Guardrail:
1. Valid Scenario: Generates and persists all 7 core domain pillars (Tenants, Medicines, Batches,
   Subscriptions, Payments, Staff, Prescriptions) with exact Decimal BDT math and reproducible seed.
2. Invalid / Incomplete Inputs: Rejects unmasked Bangladesh National IDs (NID), invalid patient tokens,
   real personal phone numbers, and malformed PCB registration numbers with SyntheticDataSafetyViolation.
3. Unauthorized / Cross-Tenant: Prohibits foreign tenant entity linkage (CrossTenantFactoryViolation) and
   enforces that Cashiers cannot sign off on antibiotic prescriptions (HTTP 403 vs Pharmacist HTTP 200).
4. Source-Specific Business Rule: Zero Real Patient PHI invariant enforced across all generated fixtures.
5. REST API Parity: Validates DRF endpoints for Subscriptions, Staff, Prescriptions, and Payments.
6. CLI Execution: shared/tools/mock_data_factory_cli.py runs successfully with exit code 0.
7. ADR-0033 Decision Record Parity: Verifies existence and architectural coverage of ADR-0033.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

# Add project root and backend to sys.path
_CANDIDATE = Path(__file__).resolve().parent.parent
_ROOT_DIR = _CANDIDATE.parent if (_CANDIDATE.parent / "backend").exists() else _CANDIDATE
_BACKEND_DIR = _ROOT_DIR / "backend"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "shared"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.mock_factory import (
    CrossTenantFactoryViolation,
    DeterministicMockDataFactory,
    SyntheticDataSafetyValidator,
    SyntheticDataSafetyViolation,
)
from apps.core.models import (
    PrescriptionRecord,
    PrescriptionStatus,
    StaffMember,
    StaffRole,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
)


class MockDataFactoryTestSuite(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.seed = 20261029
        self.factory = DeterministicMockDataFactory(seed=self.seed)

    def tearDown(self):
        from apps.core.models import (
            BatchStock,
            Branch,
            MedicineProduct,
            PaymentTransaction,
            PrescriptionItem,
            PrescriptionRecord,
            StaffMember,
            SubscriptionRecord,
        )

        PrescriptionItem.objects.all().delete()
        PrescriptionRecord.objects.all().delete()
        PaymentTransaction.objects.all().delete()
        StaffMember.objects.all().delete()
        SubscriptionRecord.objects.all().delete()
        BatchStock.objects.filter(batch_id__startswith="batch-").delete()
        MedicineProduct.objects.filter(product_id__startswith="med-").delete()
        Branch.objects.filter(branch_id__contains="-br-").delete()

    def test_valid_scenario_deterministic_generation_and_reproducibility(self):
        """Valid Scenario: Generates all 7 domain pillars with reproducible seed and exact Decimal math."""
        bundle1 = self.factory.generate_bundle()
        factory2 = DeterministicMockDataFactory(seed=self.seed)
        bundle2 = factory2.generate_bundle()

        # 1. Exact reproducibility
        self.assertEqual(bundle1, bundle2)

        # 2. Check pillar counts
        self.assertEqual(len(bundle1["tenants"]), 2)
        self.assertEqual(len(bundle1["branches"]), 4)
        self.assertGreaterEqual(len(bundle1["medicines"]), 6)
        self.assertEqual(len(bundle1["subscriptions"]), 2)
        self.assertEqual(len(bundle1["staff"]), 12)
        self.assertGreaterEqual(len(bundle1["batches"]), 24)
        self.assertGreaterEqual(len(bundle1["prescriptions"]), 4)
        self.assertGreaterEqual(len(bundle1["payments"]), 4)

        # 3. Strict Decimal monetary precision (Zero float drift)
        for med in bundle1["medicines"]:
            self.assertIsInstance(med["mrp_unit"], Decimal)
            self.assertIsInstance(med["cost_unit"], Decimal)
            self.assertGreater(med["mrp_unit"], Decimal("0.00"))
            self.assertGreater(med["cost_unit"], Decimal("0.00"))

        for sub in bundle1["subscriptions"]:
            self.assertIsInstance(sub["amount_bdt"], Decimal)
            self.assertGreater(sub["amount_bdt"], Decimal("0.00"))

        for pay in bundle1["payments"]:
            self.assertIsInstance(pay["amount_bdt"], Decimal)
            self.assertGreater(pay["amount_bdt"], Decimal("0.00"))

        # 4. Zero Real Patient PHI compliance
        SyntheticDataSafetyValidator.assert_zero_phi(bundle1)

    def test_database_persistence_and_orm_relationships(self):
        """Database Persistence: Verifies persist_all creates valid ORM records with foreign key integrity."""
        counts = DeterministicMockDataFactory.persist_all(seed=self.seed)
        self.assertGreater(counts["tenants"], 0)
        self.assertGreater(counts["branches"], 0)
        self.assertGreater(counts["medicines"], 0)
        self.assertGreater(counts["subscriptions"], 0)
        self.assertGreater(counts["staff"], 0)
        self.assertGreater(counts["batches"], 0)
        self.assertGreater(counts["prescriptions"], 0)
        self.assertGreater(counts["payments"], 0)

        # Verify specific ORM constraints
        tenant = Tenant.objects.get(tenant_id="tenant-dhaka-central")
        self.assertEqual(tenant.plan_tier, "PRO")
        self.assertGreaterEqual(tenant.branches.count(), 2)

        # Verify prescription item relationship
        rx = PrescriptionRecord.objects.filter(tenant=tenant).first()
        self.assertIsNotNone(rx)
        self.assertGreater(rx.items.count(), 0)
        self.assertTrue(rx.patient_token.startswith("PT-"))

        # Verify subscription record
        sub = SubscriptionRecord.objects.get(tenant=tenant)
        self.assertEqual(sub.status, SubscriptionStatus.ACTIVE)
        self.assertIsInstance(sub.amount_bdt, Decimal)

    def test_invalid_and_unsafe_inputs_rejection(self):
        """Invalid Inputs: Rejects real Bangladesh citizen NID, unmasked phone, and invalid patient token."""
        # 1. 10-digit real citizen smart NID
        sample_10_digit_nid = "12345" + "67890"
        with self.assertRaises(SyntheticDataSafetyViolation) as ctx:
            SyntheticDataSafetyValidator.assert_zero_phi(f"Patient NID {sample_10_digit_nid} recorded at counter")
        self.assertIn("Potential Bangladesh National ID (NID)", str(ctx.exception))

        # 2. 17-digit birth-year prefixed NID
        sample_17_digit_nid = "1985" + "1234567890123"
        with self.assertRaises(SyntheticDataSafetyViolation) as ctx:
            SyntheticDataSafetyValidator.assert_zero_phi({"patient": {"nid": sample_17_digit_nid}})
        self.assertIn("prohibited in synthetic fixtures", str(ctx.exception))

        # 3. Real citizen mobile phone number
        with self.assertRaises(SyntheticDataSafetyViolation) as ctx:
            SyntheticDataSafetyValidator.assert_zero_phi("+8801712345678")
        self.assertIn("Real mobile telephone number", str(ctx.exception))

        # 4. Invalid patient token not using synthetic prefix
        with self.assertRaises(SyntheticDataSafetyViolation) as ctx:
            SyntheticDataSafetyValidator.assert_patient_token_safe("Md. Rafiqul Islam (Real Patient)")
        self.assertIn("Must start with an approved synthetic prefix", str(ctx.exception))

    def test_unauthorized_and_cross_tenant_boundaries(self):
        """Security: Blocks cross-tenant entity linkage and unauthorized cashier clinical sign-off."""
        # 1. Multi-Tenant isolation breach
        with self.assertRaises(CrossTenantFactoryViolation) as ctx:
            DeterministicMockDataFactory.assert_tenant_isolation(
                parent_tenant_id="tenant-dhaka-central",
                child_tenant_id="tenant-chittagong-care",
                entity_name="PrescriptionRecord",
            )
        self.assertIn("Multi-Tenant Isolation Breach", str(ctx.exception))

        # 2. Setup DB entities for verification test
        DeterministicMockDataFactory.persist_all(seed=self.seed)
        tenant = Tenant.objects.get(tenant_id="tenant-dhaka-central")

        cashier = StaffMember.objects.filter(tenant=tenant, role=StaffRole.CASHIER).first()
        pharmacist = StaffMember.objects.filter(tenant=tenant, role=StaffRole.PHARMACIST).first()
        self.assertIsNotNone(cashier)
        self.assertIsNotNone(pharmacist)

        rx = PrescriptionRecord.objects.filter(tenant=tenant, status=PrescriptionStatus.PENDING).first()
        self.assertIsNotNone(rx)

        # 3. Cashier attempting to verify prescription returns HTTP 403 Forbidden
        cashier_verify_payload = {
            "pharmacist_user_id": cashier.staff_id,
            "pharmacist_pcb_reg_no": "PCB-C12345",
            "decision": "APPROVED",
            "notes": "Attempted authorization by cashier",
        }
        resp = self.client.post(
            f"/api/v2/prescriptions/{rx.prescription_id}/verify/",
            cashier_verify_payload,
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data.get("code"), "UNAUTHORIZED_CLINICAL_SIGN_OFF")

        # 4. Registered Pharmacist successfully verifies prescription (HTTP 200 OK)
        pharmacist_verify_payload = {
            "pharmacist_user_id": pharmacist.staff_id,
            "pharmacist_pcb_reg_no": pharmacist.pcb_registration_no,
            "decision": "APPROVED",
            "notes": "Clinically reviewed dosage and indication.",
        }
        resp_ok = self.client.post(
            f"/api/v2/prescriptions/{rx.prescription_id}/verify/",
            pharmacist_verify_payload,
            format="json",
        )
        self.assertEqual(resp_ok.status_code, status.HTTP_200_OK)
        rx.refresh_from_db()
        self.assertEqual(rx.status, PrescriptionStatus.VERIFIED)
        self.assertEqual(rx.verified_by, pharmacist)
        self.assertEqual(rx.pharmacist_pcb_no, pharmacist.pcb_registration_no)

    def test_rest_api_endpoints_integration(self):
        """REST API: Validates GET/POST for Subscriptions, Staff, and Payments."""
        DeterministicMockDataFactory.persist_all(seed=self.seed)

        # 1. Subscriptions API
        resp_sub = self.client.get("/api/v2/subscriptions/?tenant_id=tenant-dhaka-central")
        self.assertEqual(resp_sub.status_code, status.HTTP_200_OK)
        self.assertGreater(len(resp_sub.data), 0)
        self.assertEqual(resp_sub.data[0]["plan_tier"], "PRO")

        # 2. Staff API
        resp_staff = self.client.get("/api/v2/staff/?tenant_id=tenant-dhaka-central&role=PHARMACIST")
        self.assertEqual(resp_staff.status_code, status.HTTP_200_OK)
        self.assertGreater(len(resp_staff.data), 0)
        self.assertTrue(resp_staff.data[0]["pcb_registration_no"].startswith("PCB-"))

        # 3. Payments API
        resp_pay = self.client.get("/api/v2/payments/?tenant_id=tenant-dhaka-central")
        self.assertEqual(resp_pay.status_code, status.HTTP_200_OK)
        self.assertGreater(len(resp_pay.data), 0)

    def test_cli_tool_execution(self):
        """CLI Toolchain: Verifies shared/tools/mock_data_factory_cli.py executes with exit code 0."""
        cli_script = _ROOT_DIR / "shared" / "tools" / "mock_data_factory_cli.py"
        self.assertTrue(cli_script.exists())

        cmd = [sys.executable, str(cli_script), "--generate", "--audit-phi"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, f"CLI execution failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[GENERATE]", proc.stdout)
        self.assertIn("[PHI AUDIT PASS]", proc.stdout)

    def test_adr_0033_decision_record_parity(self):
        """Documentation: Ensures ADR-0033 decision record exists and contains required sections."""
        adr_file = (
            _ROOT_DIR
            / "shared"
            / "docs"
            / "architecture"
            / "decisions"
            / "ADR-0033-deterministic-mock-data-factories-and-phi-protection.md"
        )
        self.assertTrue(adr_file.exists(), f"ADR-0033 not found at {adr_file}")

        content = adr_file.read_text(encoding="utf-8")
        self.assertIn("ADR-0033", content)
        self.assertIn("Deterministic Mock Data Factories", content)
        self.assertIn("Zero Real Patient PHI Invariant", content)
        self.assertIn("SyntheticDataSafetyViolation", content)
        self.assertIn("CrossTenantFactoryViolation", content)
