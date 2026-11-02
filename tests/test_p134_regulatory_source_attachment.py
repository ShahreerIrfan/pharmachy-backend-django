"""
backend/tests/test_p134_regulatory_source_attachment.py
Architecture Decision: ADR-0134 (P134).
Integration Tests for Legal and Manufacturer Regulatory Source Attachment:
Classification Rules, Storage Boundaries (Cold Chain), Periodic Review Cycles,
Historical Immutability, RBAC, and Audit History.
"""

from __future__ import annotations

import datetime
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
    PlanTier,
    RegulatoryRuleTarget,
    RegulatorySourceAttachment,
    RegulatorySourceType,
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
    / "regulatory_source_attachment.schema.json"
)


class RegulatorySourceAttachmentIntegrationTests(TestCase):
    """End-to-end integration tests for P134 Regulatory Source Attachment."""

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
            tenant_id=f"tn-p134-{self.suffix}",
            name=f"Incepta Care {self.suffix}",
            subdomain=f"incepta-p134-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p134-{self.suffix}",
            tenant=self.tenant,
            name="Main Incepta Dispensary",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@incepta.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Lead Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@incepta.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Counter Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p134-susp-{self.suffix}",
            name=f"Suspended Pharmacy {self.suffix}",
            subdomain=f"susp-p134-{self.suffix}",
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
            product_id=f"med-p134-{self.suffix}",
            brand_name=f"Insulin Glargine {self.suffix}",
            generic_name="Insulin Glargine",
            dosage_form="Injection",
            strength="100 IU/mL",
            manufacturer="Incepta Pharmaceuticals Ltd.",
            mrp_unit=Decimal("450.00"),
            cost_unit=Decimal("380.00"),
            requires_cold_chain=False,
            is_prescription_only=False,
            curation_status=MedicineCurationStatus.APPROVED,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def _validate_schema(self, instance, definition_key: str):
        sub_schema = dict(self.schema_doc["definitions"][definition_key])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=instance, schema=sub_schema)

    def test_01_attach_valid_regulatory_source_validates_json_schema(self):
        """P134 Acceptance: Valid regulatory source attachment, storage rules synchronization, and JSON schema validation."""
        self.client.force_authenticate(user=self.pharmacist)

        payload = {
            "product_id": self.product.product_id,
            "source_type": RegulatorySourceType.DGDA_GAZETTE,
            "rule_target": RegulatoryRuleTarget.CLASSIFICATION_AND_STORAGE,
            "document_reference_number": f"DGDA/DL/Gazette-2026-{self.suffix}",
            "issuing_authority": "Directorate General of Drug Administration (DGDA)",
            "publication_date": "2026-05-15",
            "effective_date": "2026-06-01",
            "review_cycle_months": 24,
            "next_review_due_date": "2028-06-01",
            "source_url": "https://dgda.gov.bd/gazettes/2026/glargine-storage.pdf",
            "storage_rule_specs": {
                "requires_cold_chain": True,
                "min_temp_celsius": "2.00",
                "max_temp_celsius": "8.00",
                "protect_from_light": True,
                "max_humidity_percent": 60,
                "handling_instructions": "Store between 2°C and 8°C in a refrigerator. Protect from freezing.",
            },
            "classification_specs": {
                "classification_type": "PRESCRIPTION_ONLY",
                "is_antibiotic": False,
                "is_narcotic": False,
                "special_dispensing_conditions": "Cold chain pack out required upon dispensing.",
            },
            "review_notes": "Statutory DGDA gazette attachment verifying strict 2-8 C biological storage.",
        }

        response = self.client.post("/api/v2/catalog/regulatory-sources/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        # Schema Validation
        self._validate_schema(data, "RegulatorySourceAttachmentItem")

        self.assertEqual(data["product_id"], self.product.product_id)
        self.assertEqual(data["source_type"], RegulatorySourceType.DGDA_GAZETTE)
        self.assertEqual(data["rule_target"], RegulatoryRuleTarget.CLASSIFICATION_AND_STORAGE)
        self.assertEqual(data["version_number"], 1)
        self.assertTrue(data["is_active"])
        self.assertEqual(len(data["immutable_source_hash"]), 64)

        # Verify parent MedicineProduct synchronized attributes
        self.product.refresh_from_db()
        self.assertTrue(self.product.requires_cold_chain)
        self.assertEqual(self.product.min_temp_celsius, Decimal("2.00"))
        self.assertEqual(self.product.max_temp_celsius, Decimal("8.00"))
        self.assertTrue(self.product.is_prescription_only)
        self.assertEqual(self.product.regulatory_source, data["document_reference_number"])

    def test_02_cold_chain_rule_violation_rejected_with_validation_error(self):
        """P134 Acceptance: Invalid cold chain specification (min_temp >= max_temp) rejected."""
        self.client.force_authenticate(user=self.pharmacist)

        payload = {
            "product_id": self.product.product_id,
            "source_type": RegulatorySourceType.MANUFACTURER_MONOGRAPH,
            "rule_target": RegulatoryRuleTarget.STORAGE_CONDITION,
            "document_reference_number": f"INCEPTA-SMPC-{self.suffix}",
            "issuing_authority": "Incepta Regulatory Affairs",
            "effective_date": "2026-01-01",
            "storage_rule_specs": {
                "requires_cold_chain": True,
                "min_temp_celsius": "15.00",
                "max_temp_celsius": "8.00",  # Inverted boundary!
            },
        }

        response = self.client.post("/api/v2/catalog/regulatory-sources/", data=payload, format="json")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        err = response.json()
        self.assertTrue("min_temp_celsius must be strictly less than max_temp_celsius" in str(err))

    def test_03_invalid_review_date_order_rejected(self):
        """P134 Acceptance: next_review_due_date earlier than effective_date rejected."""
        self.client.force_authenticate(user=self.pharmacist)

        payload = {
            "product_id": self.product.product_id,
            "source_type": RegulatorySourceType.PHARMACOPOEIA,
            "rule_target": RegulatoryRuleTarget.CLASSIFICATION,
            "document_reference_number": f"BP-2026-{self.suffix}",
            "issuing_authority": "British Pharmacopoeia Commission",
            "effective_date": "2026-06-01",
            "next_review_due_date": "2025-01-01",  # Prior to effective date!
        }

        response = self.client.post("/api/v2/catalog/regulatory-sources/", data=payload, format="json")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertTrue("next_review_due_date cannot be earlier than effective_date" in str(response.json()))

    def test_04_revision_creates_incremented_version_and_new_hash(self):
        """P134 Acceptance: Multiple revisions of same document reference number increment version and compute distinct hashes."""
        self.client.force_authenticate(user=self.pharmacist)
        doc_ref = f"DGDA-CIRCULAR-2026-{self.suffix}"

        # 1. Version 1
        resp1 = self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.DGDA_CIRCULAR,
                "rule_target": RegulatoryRuleTarget.CLASSIFICATION,
                "document_reference_number": doc_ref,
                "issuing_authority": "DGDA Drug Control Committee",
                "effective_date": "2026-01-01",
            },
            format="json",
        )
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        v1_data = resp1.json()
        self.assertEqual(v1_data["version_number"], 1)

        # 2. Version 2 (Revised Effective Date / Re-filing)
        resp2 = self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.DGDA_CIRCULAR,
                "rule_target": RegulatoryRuleTarget.CLASSIFICATION,
                "document_reference_number": doc_ref,
                "issuing_authority": "DGDA Drug Control Committee",
                "effective_date": "2026-07-01",
            },
            format="json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)
        v2_data = resp2.json()
        self.assertEqual(v2_data["version_number"], 2)
        self.assertNotEqual(v1_data["immutable_source_hash"], v2_data["immutable_source_hash"])

    def test_05_cashier_role_denied_from_attaching_or_mutating_sources(self):
        """P134 Acceptance: Cashier role strictly denied with ERR_STAFF_ROLE_DENIED."""
        self.client.force_authenticate(user=self.cashier)

        resp = self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.DGDA_GAZETTE,
                "rule_target": RegulatoryRuleTarget.STORAGE_CONDITION,
                "document_reference_number": f"DGDA-CASHIER-{self.suffix}",
                "issuing_authority": "DGDA",
                "effective_date": "2026-01-01",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_STAFF_ROLE_DENIED", str(resp.json()))

    def test_06_suspended_tenant_blocked_with_403(self):
        """P134 Acceptance: Suspended tenant staff blocked from mutating regulatory sources."""
        self.client.force_authenticate(user=self.suspended_owner)

        resp = self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.DGDA_GAZETTE,
                "rule_target": RegulatoryRuleTarget.STORAGE_CONDITION,
                "document_reference_number": f"DGDA-SUSP-{self.suffix}",
                "issuing_authority": "DGDA",
                "effective_date": "2026-01-01",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        err_str = str(resp.json())
        self.assertTrue("ERR_TENANT_SUSPENDED" in err_str or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in err_str)

    def test_07_search_and_filter_regulatory_sources(self):
        """P134 Acceptance: Listing and filtering regulatory sources by search and rule target."""
        self.client.force_authenticate(user=self.pharmacist)
        doc_ref = f"WHO-AWARE-GUIDE-{self.suffix}"

        self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.WHO_AWARE,
                "rule_target": RegulatoryRuleTarget.CLASSIFICATION,
                "document_reference_number": doc_ref,
                "issuing_authority": "World Health Organization AWaRe",
                "effective_date": "2026-03-01",
            },
            format="json",
        )

        # Search query
        list_resp = self.client.get(
            f"/api/v2/catalog/regulatory-sources/?search={doc_ref}&rule_target={RegulatoryRuleTarget.CLASSIFICATION}"
        )
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        results = list_resp.json()["results"]
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["document_reference_number"], doc_ref)

    def test_08_due_reviews_compliance_endpoint(self):
        """P134 Acceptance: Querying attachments requiring periodic compliance re-review."""
        self.client.force_authenticate(user=self.pharmacist)
        past_due = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()

        resp = self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.CLINICAL_GUIDELINE,
                "rule_target": RegulatoryRuleTarget.STORAGE_CONDITION,
                "document_reference_number": f"CLIN-OVERDUE-{self.suffix}",
                "issuing_authority": "National Treatment Committee",
                "effective_date": "2025-01-01",
                "next_review_due_date": past_due,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        att_id = resp.json()["attachment_id"]

        # Call due-reviews endpoint
        due_resp = self.client.get("/api/v2/catalog/regulatory-sources/due-reviews/")
        self.assertEqual(due_resp.status_code, status.HTTP_200_OK)
        due_results = due_resp.json()["results"]
        due_ids = [item["attachment_id"] for item in due_results]
        self.assertIn(att_id, due_ids)

    def test_09_archive_attachment_soft_deactivates_record(self):
        """P134 Acceptance: Soft-archiving deactivates source attachment."""
        self.client.force_authenticate(user=self.pharmacist)
        resp = self.client.post(
            "/api/v2/catalog/regulatory-sources/",
            data={
                "product_id": self.product.product_id,
                "source_type": RegulatorySourceType.DGDA_GAZETTE,
                "rule_target": RegulatoryRuleTarget.STORAGE_CONDITION,
                "document_reference_number": f"TO-ARCHIVE-{self.suffix}",
                "issuing_authority": "DGDA",
                "effective_date": "2026-01-01",
            },
            format="json",
        )
        att_id = resp.json()["attachment_id"]

        archive_resp = self.client.post(
            f"/api/v2/catalog/regulatory-sources/{att_id}/archive/",
            data={"reason": "Replaced by revised 2026 circular."},
            format="json",
        )
        self.assertEqual(archive_resp.status_code, status.HTTP_200_OK)
        self.assertFalse(archive_resp.json()["is_active"])

        # Check DB
        att = RegulatorySourceAttachment.objects.get(attachment_id=att_id)
        self.assertFalse(att.is_active)

    def test_10_unauthenticated_request_rejected_with_401(self):
        """P134 Acceptance: Unauthenticated requests rejected with HTTP 401."""
        self.client.logout()
        resp = self.client.get("/api/v2/catalog/regulatory-sources/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
