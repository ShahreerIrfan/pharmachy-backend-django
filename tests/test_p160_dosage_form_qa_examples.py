"""
backend/tests/test_p160_dosage_form_qa_examples.py
Architecture Decision: ADR-0160 (P160).
Phase 08: Specialized Item & Multi-Tier Packaging Formulation Editors.

Integration test suite for comprehensive positive and negative QA fixtures,
automated validation harness, and catalog seeding across all 11 dosage forms.
"""

import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestDosageFormQaExamplesIntegration:
    """Integration test suite for P160 Dosage Form QA Examples and Fixture Harness."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p160-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p160-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p160-{self.suffix}",
            tenant=self.tenant,
            name="Main Pharmacy Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p160-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"susp-p160-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p160-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Users with RBAC roles
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharma_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Suspended Staff {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    def test_list_all_qa_fixtures(self):
        """1. GET /api/v2/catalog/qa-fixtures/ lists all positive and negative canonical fixtures."""
        self.client.force_authenticate(user=self.user_pharmacist)

        response = self.client.get("/api/v2/catalog/qa-fixtures/")
        assert response.status_code == status.HTTP_200_OK
        data = response.data

        assert data["positive_count"] >= 11
        assert data["negative_count"] >= 10
        assert len(data["positive_fixtures"]) >= 11
        assert len(data["negative_fixtures"]) >= 10

        # Filter by dosage form family
        filtered_form = self.client.get("/api/v2/catalog/qa-fixtures/?dosage_form_family=TABLET_CAPSULE")
        assert filtered_form.status_code == status.HTTP_200_OK
        for f in filtered_form.data["positive_fixtures"]:
            assert f["dosage_form_family"] == "TABLET_CAPSULE"

        # Filter negative by target error code
        filtered_err = self.client.get("/api/v2/catalog/qa-fixtures/?target_error_code=ERR_NEGATIVE_COST")
        assert filtered_err.status_code == status.HTTP_200_OK
        for f in filtered_err.data["negative_fixtures"]:
            assert f["target_error_code"] == "ERR_NEGATIVE_COST"

    def test_validate_single_positive_fixture(self):
        """2. Valid scenario: Validating POS-001-TABLET-CAPSULE returns passed=True with 0 errors."""
        self.client.force_authenticate(user=self.user_pharmacist)

        response = self.client.post(
            "/api/v2/catalog/qa-fixtures/validate-fixture/",
            {"fixture_id": "POS-001-TABLET-CAPSULE"},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert data["fixture_id"] == "POS-001-TABLET-CAPSULE"
        assert data["fixture_type"] == "POSITIVE"
        assert data["passed"] is True
        assert data["validation_result"]["is_valid"] is True
        assert data["validation_result"]["error_count"] == 0

    def test_validate_all_eleven_dosage_forms(self):
        """3. Positive scenario: Every canonical dosage form fixture passes validation with zero errors."""
        self.client.force_authenticate(user=self.user_pharmacist)

        list_resp = self.client.get("/api/v2/catalog/qa-fixtures/")
        positives = list_resp.data["positive_fixtures"]
        assert len(positives) >= 11

        for fixture in positives:
            fid = fixture["fixture_id"]
            val_resp = self.client.post(
                "/api/v2/catalog/qa-fixtures/validate-fixture/",
                {"fixture_id": fid},
                format="json",
            )
            assert val_resp.status_code == status.HTTP_200_OK, f"Validation endpoint failed for {fid}"
            assert val_resp.data["passed"] is True, f"Fixture {fid} unexpectedly failed validation: {val_resp.data}"

    def test_validate_negative_fixtures_detects_expected_errors(self):
        """4. Invalid scenario: Negative fixtures are strictly rejected with expected error codes."""
        self.client.force_authenticate(user=self.user_pharmacist)

        test_cases = [
            ("NEG-001-NEGATIVE-COST", "ERR_NEGATIVE_COST"),
            ("NEG-002-INVALID-MRP", "ERR_INVALID_MRP"),
            ("NEG-003-MRP-BELOW-COST", "ERR_MRP_BELOW_COST"),
            ("NEG-004-DIMENSION-MISMATCH", "ERR_DIMENSION_MISMATCH"),
            ("NEG-005-TEMP-INVERSION", "ERR_INVALID_TEMPERATURE_RANGE"),
            ("NEG-006-INCOMPATIBLE-FIELD", "ERR_INCOMPATIBLE_FIELD"),
            ("NEG-007-UNKNOWN-FIELD", "ERR_UNKNOWN_FIELD"),
            ("NEG-008-UNSUPPORTED-VERSION", "ERR_PAYLOAD_VERSION_UNSUPPORTED"),
            ("NEG-009-MISSING-MANDATORY-COMMON", "ERR_MANDATORY_FIELD_MISSING"),
            ("NEG-010-MISSING-MANDATORY-FORM", "ERR_MANDATORY_FIELD_MISSING"),
        ]

        for fid, expected_err in test_cases:
            val_resp = self.client.post(
                "/api/v2/catalog/qa-fixtures/validate-fixture/",
                {"fixture_id": fid},
                format="json",
            )
            assert val_resp.status_code == status.HTTP_200_OK
            data = val_resp.data
            assert data["fixture_type"] == "NEGATIVE"
            assert data["passed"] is True, f"Negative fixture {fid} did not pass assertion. Data: {data}"
            assert expected_err in data["observed_error_codes"]

    def test_validate_nonexistent_fixture_returns_404(self):
        """5. Nonexistent fixture ID returns 404 NOT FOUND with ERR_FIXTURE_NOT_FOUND."""
        self.client.force_authenticate(user=self.user_pharmacist)

        response = self.client.post(
            "/api/v2/catalog/qa-fixtures/validate-fixture/",
            {"fixture_id": "NON-EXISTENT-FIXTURE-XYZ"},
            format="json",
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data["error_code"] == "ERR_FIXTURE_NOT_FOUND"

    def test_seed_qa_fixtures_success(self):
        """6. Seeding canonical fixtures creates products in catalog and emits audit logs."""
        self.client.force_authenticate(user=self.user_pharmacist)

        response = self.client.post("/api/v2/catalog/qa-fixtures/seed/", {}, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        data = response.data

        assert data["seeded_count"] == 11
        assert data["total_fixtures"] == 11

        # Check that products and audit logs were recorded
        first_product_id = data["products"][0]["product_id"]
        assert MedicineProduct.objects.filter(product_id=first_product_id).exists()

        audit_exists = AuditLogEntry.objects.filter(
            action="SUBMIT_FLEXIBLE_MEDICINE_PAYLOAD",
            resource_id=first_product_id,
        ).exists()
        assert audit_exists is True

    def test_seed_fixtures_cashier_denied(self):
        """7. Unauthorized scenario: Cashier role is blocked from seeding catalog with 403."""
        self.client.force_authenticate(user=self.user_cashier)

        response = self.client.post("/api/v2/catalog/qa-fixtures/seed/", {}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_seed_fixtures_suspended_tenant_blocked(self):
        """8. Unauthorized scenario: Suspended tenant staff is blocked from seeding catalog."""
        self.client.force_authenticate(user=self.user_suspended)

        response = self.client.post("/api/v2/catalog/qa-fixtures/seed/", {}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]
