"""
backend/tests/test_p159_flexible_medicine_api_payloads.py
Architecture Decision: ADR-0159 (P159).
Integration test suite for Typed, Versioned Dynamic Medicine Payloads in DRF.
"""

import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

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
class TestFlexibleMedicinePayloadsIntegration:
    """Integration test suite for P159 Flexible Medicine Payloads in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p159-{self.suffix}",
            name=f"Apollo Pharmacy {self.suffix}",
            subdomain=f"apollo-p159-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p159-{self.suffix}",
            tenant=self.tenant,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p159-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"susp-p159-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p159-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharma_{self.suffix}@apollo.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@apollo.test",
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

    def test_valid_inhaler_payload_validation_and_submission(self):
        """1. Valid scenario: Typed, versioned INHALER payload validates and creates master product."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "schema_version": "2026.01",
            "dosage_form_family": "INHALER",
            "attributes": {
                "brand_name": f"Ventolin Inhaler {self.suffix}",
                "brand_name_bn": "ভেনটোলিন ইনহেলার",
                "generic_name": "Salbutamol",
                "manufacturer": "GSK Bangladesh",
                "dar_number": f"DAR-P159-{self.suffix}",
                "cost_unit": "175.50",
                "mrp_unit": "220.00",
                "vat_percentage": "2.5",
                "dose_per_actuation": "100mcg",
                "actuations_per_device": 200,
                "canister_type": "MDI_AEROSOL",
            },
        }

        # 1a. Validate endpoint
        val_resp = self.client.post("/api/v2/catalog/flexible-payloads/validate/", payload, format="json")
        assert val_resp.status_code == status.HTTP_200_OK
        assert val_resp.data["is_valid"] is True
        assert val_resp.data["error_count"] == 0

        # 1b. Submit endpoint
        sub_resp = self.client.post("/api/v2/catalog/flexible-payloads/submit/", payload, format="json")
        assert sub_resp.status_code == status.HTTP_201_CREATED
        assert sub_resp.data["status"] == "CREATED"
        assert sub_resp.data["dgda_dar_number"] == f"DAR-P159-{self.suffix}"

        # Verify database record
        prod = MedicineProduct.objects.get(product_id=sub_resp.data["product_id"])
        assert prod.brand_name == f"Ventolin Inhaler {self.suffix}"
        assert prod.dosage_form == "INHALER"
        assert prod.source_metadata["schema_version"] == "2026.01"
        assert prod.source_metadata["form_specific_parameters"]["dose_per_actuation"] == "100mcg"

    def test_incompatible_field_rejected_with_field_code(self):
        """2. Incompatible field rejected: Tablet packaging parameter injected into INHALER payload."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "schema_version": "2026.01",
            "dosage_form_family": "INHALER",
            "attributes": {
                "brand_name": "Seretide",
                "generic_name": "Salmeterol",
                "dar_number": "123-456",
                "cost_unit": "400.00",
                "mrp_unit": "500.00",
                "dose_per_actuation": "125mcg",
                "actuations_per_device": 120,
                # Incompatible parameter: pieces_per_strip belongs to TABLET_CAPSULE
                "pieces_per_strip": 10,
            },
        }

        resp = self.client.post("/api/v2/catalog/flexible-payloads/validate/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["is_valid"] is False

        incomp_errors = [e for e in resp.data["errors"] if e["field"] == "pieces_per_strip"]
        assert len(incomp_errors) == 1
        assert incomp_errors[0]["error_code"] == "ERR_INCOMPATIBLE_FIELD"
        assert "INHALER" in incomp_errors[0]["message_en"]

    def test_unknown_field_rejected_with_field_code(self):
        """3. Unknown field rejected: Arbitrary unpermitted field rejected with ERR_UNKNOWN_FIELD."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "schema_version": "2026.01",
            "dosage_form_family": "TABLET_CAPSULE",
            "attributes": {
                "brand_name": "Napa",
                "generic_name": "Paracetamol",
                "dar_number": "001-002",
                "cost_unit": "1.00",
                "mrp_unit": "1.50",
                # Completely unknown key
                "unrecognized_custom_field": "injected_data",
            },
        }

        resp = self.client.post("/api/v2/catalog/flexible-payloads/validate/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["is_valid"] is False

        unknown_errors = [e for e in resp.data["errors"] if e["field"] == "unrecognized_custom_field"]
        assert len(unknown_errors) == 1
        assert unknown_errors[0]["error_code"] == "ERR_UNKNOWN_FIELD"

    def test_unsupported_version_rejected(self):
        """4. Unsupported version rejected with ERR_PAYLOAD_VERSION_UNSUPPORTED."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "schema_version": "v999.0",
            "dosage_form_family": "TABLET_CAPSULE",
            "attributes": {
                "brand_name": "Napa",
            },
        }

        resp = self.client.post("/api/v2/catalog/flexible-payloads/validate/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["is_valid"] is False
        assert resp.data["errors"][0]["error_code"] == "ERR_PAYLOAD_VERSION_UNSUPPORTED"

    def test_specifications_introspection_endpoint(self):
        """5. Specifications endpoint returns allowed, mandatory, and incompatible field lists."""
        self.client.force_authenticate(user=self.user_pharmacist)

        resp = self.client.get("/api/v2/catalog/flexible-payloads/specifications/?dosage_form_family=INHALER")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["dosage_form_family"] == "INHALER"
        assert "dose_per_actuation" in data["allowed_form_specific_attributes"]
        assert "pieces_per_strip" in data["incompatible_form_specific_attributes"]

    def test_cashier_denied_payload_submission(self):
        """6. Unauthorized access: Cashier role blocked from submitting master payloads."""
        self.client.force_authenticate(user=self.user_cashier)

        payload = {
            "schema_version": "2026.01",
            "dosage_form_family": "TABLET_CAPSULE",
            "attributes": {
                "brand_name": "Cashier Drug",
                "generic_name": "Paracetamol",
                "dar_number": "111-222",
                "cost_unit": "1.00",
                "mrp_unit": "2.00",
            },
        }

        resp = self.client.post("/api/v2/catalog/flexible-payloads/submit/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_suspended_tenant_blocked(self):
        """7. Suspended tenant blocked from submitting master payloads."""
        self.client.force_authenticate(user=self.user_suspended)

        payload = {
            "schema_version": "2026.01",
            "dosage_form_family": "TABLET_CAPSULE",
            "attributes": {
                "brand_name": "Suspended Drug",
            },
        }

        resp = self.client.post("/api/v2/catalog/flexible-payloads/submit/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

