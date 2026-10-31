"""
backend/tests/test_p052_data_privacy_operations.py
Architecture Decision: ADR-0052
Comprehensive Test Suite for Data Privacy Operations, Statutory Retention Assessments, and DSR Portability.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import (
    Branch,
    MedicineProduct,
    PrescriptionItem,
    PrescriptionRecord,
    Tenant,
)
from apps.data_privacy.models import (
    PrivacyRequest,
    PrivacyRequestStatus,
    PrivacyRequestType,
)
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole


class TestP052DataPrivacyOperations(unittest.TestCase):
    """
    Validates Data Subject Requests (DSR), DGDA statutory retention schedule evaluations,
    deletion conflict blocking (HTTP 409), audited anonymization, and RBAC isolation under ADR-0052.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = APIClient()
        cls.t_id = "tenant-dhanmondi-test-52"
        cls.t_foreign_id = "tenant-foreign-test-52"
        cls.b_id = "b-dhanmondi-52-01"
        cls.patient_token = "PT-52099"

    def setUp(self):
        Branch.objects.filter(branch_id=self.b_id).delete()
        PharmacyUser.objects.filter(
            email__in=["owner52t@dhanmondi.com", "cashier52t@dhanmondi.com"]
        ).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_id, self.t_foreign_id]).delete()

        self.tenant = Tenant.objects.create(
            tenant_id=self.t_id,
            name="Dhanmondi Test Care 52",
            subdomain="dhanmondi-test-52",
            plan_tier="PRO",
            is_active=True,
        )
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=self.t_foreign_id,
            name="Foreign Test Care 52",
            subdomain="foreign-test-52",
            plan_tier="STANDARD",
            is_active=True,
        )

        self.branch = Branch.objects.create(
            branch_id=self.b_id,
            tenant=self.tenant,
            name="Main Branch",
            license_number="DGDA-52-01",
            is_active=True,
        )

        self.owner = PharmacyUser.objects.create_user(
            email="owner52t@dhanmondi.com",
            phone_number="+8801719996601",
            password="OwnerPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Pharmacy Owner 52",
            role=UserRole.PHARMACY_OWNER,
            activation_status=ActivationStatus.ACTIVE,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email="cashier52t@dhanmondi.com",
            phone_number="+8801719996602",
            password="CashierPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Pharmacy Cashier 52",
            role=UserRole.CASHIER,
            activation_status=ActivationStatus.ACTIVE,
        )

        # Tokens
        r_owner = RefreshToken.for_user(self.owner)
        r_owner["tenant_id"] = self.t_id
        r_owner["branch_id"] = self.b_id
        r_owner["role"] = UserRole.PHARMACY_OWNER
        self.token_owner = str(r_owner.access_token)

        r_cashier = RefreshToken.for_user(self.cashier)
        r_cashier["tenant_id"] = self.t_id
        r_cashier["branch_id"] = self.b_id
        r_cashier["role"] = UserRole.CASHIER
        self.token_cashier = str(r_cashier.access_token)

        # Clinical prescription fixture
        med, _ = MedicineProduct.objects.get_or_create(
            product_id="MED-PRV-520",
            defaults={
                "brand_name": "Ace Plus 500mg",
                "generic_name": "Paracetamol + Caffeine",
                "dosage_form": "TABLET",
                "strength": "500mg+65mg",
                "manufacturer": "Square Pharmaceuticals PLC",
                "mrp_unit": Decimal("3.00"),
                "cost_unit": Decimal("2.40"),
            },
        )
        self.rx = PrescriptionRecord.objects.create(
            prescription_id="RX-TEST-5201",
            tenant=self.tenant,
            branch=self.branch,
            doctor_name="Dr. Nasim Ahmed",
            doctor_bmdc_no="BMDC-5299",
            patient_token=self.patient_token,
            patient_age=32,
            patient_gender="FEMALE",
            diagnosis="Acute Bronchitis & Pharyngitis",
            prescription_date=timezone.now().date(),
        )
        PrescriptionItem.objects.create(
            prescription=self.rx,
            product=med,
            dosage_instructions="1 tablet twice daily",
            quantity_prescribed=10,
        )

    def tearDown(self):
        Branch.objects.filter(branch_id=self.b_id).delete()
        PharmacyUser.objects.filter(
            email__in=["owner52t@dhanmondi.com", "cashier52t@dhanmondi.com"]
        ).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_id, self.t_foreign_id]).delete()

    def test_01_valid_data_export_request_lifecycle(self):
        """Valid Scenario: Submitting and approving DATA_EXPORT completes and provides bundle link."""
        res_sub = self.client.post(
            "/api/v2/privacy/requests/",
            data={
                "subject_type": "PATIENT",
                "subject_identifier": self.patient_token,
                "request_type": "DATA_EXPORT",
                "branch_id": self.b_id,
            },
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res_sub.status_code, 201)
        req_id = res_sub.json()["data"]["request_id"]

        # Assess as APPROVE
        res_assess = self.client.post(
            f"/api/v2/privacy/requests/{req_id}/assess/",
            data={"action": "APPROVE", "assessment_notes": "Portability approved"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res_assess.status_code, 200)
        data = res_assess.json()["data"]
        self.assertEqual(data["status"], "COMPLETED")
        self.assertIn("export-bundle", data["export_artifact_url"])

    def test_02_export_bundle_payload_structure(self):
        """Export Scenario: Calling export-bundle endpoint returns structured clinical data bundle."""
        req = PrivacyRequest.objects.create(
            tenant=self.tenant,
            subject_identifier=self.patient_token,
            request_type=PrivacyRequestType.DATA_EXPORT,
            status=PrivacyRequestStatus.COMPLETED,
        )
        res = self.client.get(
            f"/api/v2/privacy/requests/{req.request_id}/export-bundle/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 200)
        bundle = res.json()["data"]
        self.assertEqual(bundle["subject_identifier"], self.patient_token)
        self.assertGreaterEqual(bundle["clinical_prescriptions_count"], 1)
        self.assertIn("legal_retention_notice", bundle)

    def test_03_invalid_input_payload_returns_422(self):
        """Input Validation Scenario: Empty or short subject_identifier returns 422 ERR_VALIDATION_FAILED."""
        res = self.client.post(
            "/api/v2/privacy/requests/",
            data={"subject_identifier": "A", "request_type": "DATA_EXPORT"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error_code"], "ERR_VALIDATION_FAILED")

    def test_04_deletion_request_detects_730_day_statutory_retention_hold(self):
        """Retention Conflict Scenario: Subject with prescription active within 730 days is flagged."""
        res = self.client.post(
            "/api/v2/privacy/requests/",
            data={
                "subject_identifier": self.patient_token,
                "request_type": "DATA_DELETION",
            },
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertTrue(data["retention_conflict_detected"])
        self.assertEqual(data["retention_horizon_days"], 730)

    def test_05_direct_physical_deletion_blocked_with_409_err_privacy_retention_conflict(self):
        """Statutory Rule Scenario: Attempting direct APPROVE on deletion request under active hold raises 409."""
        req = PrivacyRequest.objects.create(
            tenant=self.tenant,
            subject_identifier=self.patient_token,
            request_type=PrivacyRequestType.DATA_DELETION,
            retention_conflict_detected=True,
            retention_horizon_days=730,
        )
        res = self.client.post(
            f"/api/v2/privacy/requests/{req.request_id}/assess/",
            data={"action": "APPROVE"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error_code"], "ERR_PRIVACY_RETENTION_CONFLICT")

    def test_06_anonymization_override_scrubs_pii_under_statutory_retention(self):
        """Anonymization Scenario: Assessing with ANONYMIZE scrubs clinical diagnosis and updates status."""
        req = PrivacyRequest.objects.create(
            tenant=self.tenant,
            subject_identifier=self.patient_token,
            request_type=PrivacyRequestType.DATA_DELETION,
            retention_conflict_detected=True,
            retention_horizon_days=730,
        )
        res = self.client.post(
            f"/api/v2/privacy/requests/{req.request_id}/assess/",
            data={"action": "ANONYMIZE", "assessment_notes": "Statutory anonymization executed"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["status"], "ANONYMIZED")

        # Verify database record updated
        rx_db = PrescriptionRecord.objects.get(prescription_id=self.rx.prescription_id)
        self.assertEqual(rx_db.diagnosis, "[REDACTED_PHI]")

    def test_07_rbac_cashier_forbidden_from_assessing_privacy_requests(self):
        """RBAC Scenario: Cashier is blocked with 403 ERR_PERMISSION_DENIED from evaluating requests."""
        req = PrivacyRequest.objects.create(
            tenant=self.tenant,
            subject_identifier="PT-UNKNOWN",
            request_type=PrivacyRequestType.DATA_EXPORT,
        )
        res = self.client.post(
            f"/api/v2/privacy/requests/{req.request_id}/assess/",
            data={"action": "REJECT"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_08_cross_tenant_isolation_blocked_with_403_err_cross_tenant_violation(self):
        """Cross-Tenant Scenario: Querying foreign tenant privacy requests returns 403 ERR_CROSS_TENANT_VIOLATION."""
        res = self.client.get(
            f"/api/v2/privacy/requests/?tenant_id={self.t_foreign_id}",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")
