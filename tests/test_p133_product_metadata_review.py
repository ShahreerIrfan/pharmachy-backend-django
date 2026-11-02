"""
backend/tests/test_p133_product_metadata_review.py
Architecture Decision: ADR-0133 (P133).
Integration Tests for Pharmacist/Editor Review of Medicine Metadata Changes:
Diff Inspection, Dual Control (Four-Eyes Principle), Master Publication Lifecycle,
Rejection Justification, RBAC and Tenant Isolation.
"""

from __future__ import annotations

import json
import random
import uuid
from decimal import Decimal
from pathlib import Path

import jsonschema
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineCurationStatus,
    MedicineProduct,
    MedicineReviewStatus,
    PlanTier,
    ReviewChangeType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "product_metadata_review.schema.json"
)


class ProductMetadataReviewIntegrationTests(TestCase):
    """End-to-end integration tests for P133 Product Metadata Review."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Platform Superadmin User
        self.superadmin = PharmacyUser.objects.create_user(
            email=f"superadmin_{self.suffix}@platform.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform Superadmin",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

        # 2. Pharmacy Tenant & Staff
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p133-{self.suffix}",
            name=f"Square Pharma Center {self.suffix}",
            subdomain=f"square-p133-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p133-{self.suffix}",
            tenant=self.tenant,
            name="Main Center Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.pharmacist1 = PharmacyUser.objects.create_user(
            email=f"pharmacist1_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Lead Pharmacist 1",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.pharmacist2 = PharmacyUser.objects.create_user(
            email=f"pharmacist2_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Reviewer Pharmacist 2",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Counter Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p133-susp-{self.suffix}",
            name=f"Suspended Dispensary {self.suffix}",
            subdomain=f"susp-p133-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_owner = PharmacyUser.objects.create_user(
            email=f"susp_owner_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 4. Target Medicine Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-p133-{self.suffix}",
            brand_name=f"Ace Plus {self.suffix}",
            brand_name_bn="এস প্লাস",
            generic_name="Paracetamol + Caffeine",
            generic_name_bn="প্যারাসিটামল + ক্যাফেইন",
            dosage_form="Tablet",
            strength="500mg+65mg",
            strength_numeric=Decimal("565.0000"),
            strength_unit="mg",
            manufacturer="Square Pharmaceuticals PLC",
            route="Oral",
            barcode=f"890123{random.randint(1000, 9999)}",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
            requires_cold_chain=False,
            is_prescription_only=False,
            curation_status=MedicineCurationStatus.DRAFT,
            version_number=1,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def _validate_schema(self, instance, definition_key: str):
        sub_schema = dict(self.schema_doc["definitions"][definition_key])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=instance, schema=sub_schema)

    def test_01_submit_review_proposal_and_validate_json_schema(self):
        """P133 Acceptance: Pharmacist submits metadata review proposal with diff tracking."""
        self.client.force_authenticate(user=self.pharmacist1)

        payload = {
            "product_id": self.product.product_id,
            "change_type": ReviewChangeType.METADATA_UPDATE,
            "proposed_changes": {
                "mrp_unit": "3.50",
                "dosage_form": "Extended Release Tablet",
                "is_prescription_only": True,
            },
            "submission_notes": "Clinical update per DGDA notification",
        }

        response = self.client.post("/api/v2/catalog/reviews/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        # Schema Validation
        self._validate_schema(data, "ReviewRecordItem")

        self.assertEqual(data["product_id"], self.product.product_id)
        self.assertEqual(data["status"], MedicineReviewStatus.PENDING_REVIEW)
        self.assertEqual(data["submitter_email"], self.pharmacist1.email)
        self.assertIn("mrp_unit", data["proposed_diff"])
        self.assertEqual(data["proposed_diff"]["mrp_unit"]["old_value"], "2.50")
        self.assertEqual(data["proposed_diff"]["mrp_unit"]["new_value"], "3.50")
        self.assertEqual(data["proposed_diff"]["dosage_form"]["old_value"], "Tablet")
        self.assertEqual(data["proposed_diff"]["dosage_form"]["new_value"], "Extended Release Tablet")

        # Target product transitions to UNDER_REVIEW
        self.product.refresh_from_db()
        self.assertEqual(self.product.curation_status, MedicineCurationStatus.UNDER_REVIEW)

    def test_02_pharmacist_approves_review_and_publishes_changes_to_master(self):
        """P133 Acceptance: Independent pharmacist approves proposal and diff merges into master catalog."""
        # 1. Propose by pharmacist 1
        self.client.force_authenticate(user=self.pharmacist1)
        propose_resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.PRICE_REVISION,
                "proposed_changes": {"mrp_unit": "4.00", "cost_unit": "3.20"},
                "submission_notes": "Wholesale price escalation",
            },
            format="json",
        )
        self.assertEqual(propose_resp.status_code, status.HTTP_201_CREATED)
        review_id = propose_resp.json()["review_id"]

        # 2. Independent Pharmacist 2 Approves
        self.client.force_authenticate(user=self.pharmacist2)
        approve_resp = self.client.post(
            f"/api/v2/catalog/reviews/{review_id}/approve/",
            data={"review_notes": "Verified manufacturer price schedule gazette."},
            format="json",
        )
        self.assertEqual(approve_resp.status_code, status.HTTP_200_OK)
        data = approve_resp.json()

        self._validate_schema(data, "ReviewRecordItem")
        self.assertEqual(data["status"], MedicineReviewStatus.APPROVED)
        self.assertEqual(data["reviewer_email"], self.pharmacist2.email)
        self.assertTrue(len(data["review_hash"]) == 64)

        # Verify Master MedicineProduct attributes
        self.product.refresh_from_db()
        self.assertEqual(self.product.mrp_unit, Decimal("4.00"))
        self.assertEqual(self.product.cost_unit, Decimal("3.20"))
        self.assertEqual(self.product.curation_status, MedicineCurationStatus.APPROVED)
        self.assertEqual(self.product.version_number, 2)
        self.assertEqual(self.product.reviewed_by_id, self.pharmacist2.user_id)
        self.assertTrue(len(self.product.immutable_version_hash) == 64)

    def test_03_pharmacist_rejects_review_with_mandatory_notes(self):
        """P133 Acceptance: Pharmacist rejects proposal with justification notes; product remains unchanged."""
        self.client.force_authenticate(user=self.pharmacist1)
        propose_resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.SAFETY_REASSESSMENT,
                "proposed_changes": {"is_prescription_only": True},
            },
            format="json",
        )
        review_id = propose_resp.json()["review_id"]

        self.client.force_authenticate(user=self.pharmacist2)
        reject_resp = self.client.post(
            f"/api/v2/catalog/reviews/{review_id}/reject/",
            data={"review_notes": "Rejected: Drug remains classified as OTC per current DGDA list."},
            format="json",
        )
        self.assertEqual(reject_resp.status_code, status.HTTP_200_OK)
        data = reject_resp.json()

        self.assertEqual(data["status"], MedicineReviewStatus.REJECTED)
        self.assertEqual(data["review_notes"], "Rejected: Drug remains classified as OTC per current DGDA list.")

        self.product.refresh_from_db()
        self.assertFalse(self.product.is_prescription_only)

    def test_04_four_eyes_principle_blocks_submitter_self_approval(self):
        """P133 Acceptance: Dual control prevents author from approving their own proposed changes."""
        self.client.force_authenticate(user=self.pharmacist1)
        propose_resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.METADATA_UPDATE,
                "proposed_changes": {"dosage_form": "Syrup"},
            },
            format="json",
        )
        review_id = propose_resp.json()["review_id"]

        # Submitter attempts self-approval
        self_approve_resp = self.client.post(
            f"/api/v2/catalog/reviews/{review_id}/approve/",
            data={"review_notes": "Self approval attempt by author"},
            format="json",
        )
        self.assertEqual(self_approve_resp.status_code, status.HTTP_403_FORBIDDEN)
        err = self_approve_resp.json()
        self.assertIn("ERR_FOUR_EYES_PRINCIPLE_VIOLATION", str(err))

    def test_05_superadmin_can_override_and_self_approve(self):
        """P133 Acceptance: Platform Superadmin possesses emergency self-review authority."""
        self.client.force_authenticate(user=self.superadmin)
        propose_resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.METADATA_UPDATE,
                "proposed_changes": {"barcode": "8909998887771"},
            },
            format="json",
        )
        review_id = propose_resp.json()["review_id"]

        admin_approve_resp = self.client.post(
            f"/api/v2/catalog/reviews/{review_id}/approve/",
            data={"review_notes": "Platform superadmin emergency barcode update signoff."},
            format="json",
        )
        self.assertEqual(admin_approve_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_approve_resp.json()["status"], MedicineReviewStatus.APPROVED)

    def test_06_pending_reviews_queue_action(self):
        """P133 Acceptance: Pending endpoint filters only records awaiting review sign-off."""
        # Create pending review
        self.client.force_authenticate(user=self.pharmacist1)
        resp1 = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.METADATA_UPDATE,
                "proposed_changes": {"route": "Topical"},
            },
            format="json",
        )
        rev1_id = resp1.json()["review_id"]

        # Create approved review
        resp2 = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.PRICE_REVISION,
                "proposed_changes": {"mrp_unit": "5.00"},
            },
            format="json",
        )
        rev2_id = resp2.json()["review_id"]
        self.client.force_authenticate(user=self.pharmacist2)
        self.client.post(
            f"/api/v2/catalog/reviews/{rev2_id}/approve/",
            data={"review_notes": "Approved price revision."},
            format="json",
        )

        # Query pending queue
        queue_resp = self.client.get("/api/v2/catalog/reviews/pending/")
        self.assertEqual(queue_resp.status_code, status.HTTP_200_OK)
        results = queue_resp.json()["results"]
        result_ids = [r["review_id"] for r in results]

        self.assertIn(rev1_id, result_ids)
        self.assertNotIn(rev2_id, result_ids)
        for r in results:
            self.assertEqual(r["status"], MedicineReviewStatus.PENDING_REVIEW)

    def test_07_cashier_role_denied_from_review_workflow(self):
        """P133 Acceptance: Cashier role is strictly denied from proposing or approving reviews."""
        self.client.force_authenticate(user=self.cashier)

        # Attempt create
        create_resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.METADATA_UPDATE,
                "proposed_changes": {"mrp_unit": "10.00"},
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_STAFF_ROLE_DENIED", str(create_resp.json()))

    def test_08_suspended_tenant_blocked_with_403(self):
        """P133 Acceptance: Suspended tenant staff cannot propose or mutate reviews."""
        self.client.force_authenticate(user=self.suspended_owner)

        resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.METADATA_UPDATE,
                "proposed_changes": {"mrp_unit": "10.00"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        err_str = str(resp.json())
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in err_str or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in err_str,
            f"Expected tenant suspended error code but got: {err_str}",
        )

    def test_09_cannot_decide_already_decided_review(self):
        """P133 Acceptance: Already approved/rejected reviews cannot be re-decided."""
        self.client.force_authenticate(user=self.pharmacist1)
        resp = self.client.post(
            "/api/v2/catalog/reviews/",
            data={
                "product_id": self.product.product_id,
                "change_type": ReviewChangeType.METADATA_UPDATE,
                "proposed_changes": {"mrp_unit": "6.00"},
            },
            format="json",
        )
        review_id = resp.json()["review_id"]

        self.client.force_authenticate(user=self.pharmacist2)
        # First decision: Approve
        self.client.post(
            f"/api/v2/catalog/reviews/{review_id}/approve/",
            data={"review_notes": "First valid approval signoff."},
            format="json",
        )

        # Second attempt: Reject already approved review
        re_reject_resp = self.client.post(
            f"/api/v2/catalog/reviews/{review_id}/reject/",
            data={"review_notes": "Attempting to reject already approved review."},
            format="json",
        )
        self.assertIn(
            re_reject_resp.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )
        self.assertIn("ERR_REVIEW_ALREADY_DECIDED", str(re_reject_resp.json()))

    def test_10_unauthenticated_request_rejected_with_401(self):
        """P133 Acceptance: Unauthenticated requests are rejected with HTTP 401."""
        self.client.logout()
        resp = self.client.get("/api/v2/catalog/reviews/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
