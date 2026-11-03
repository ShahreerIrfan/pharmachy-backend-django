"""
backend/tests/test_p151_device_consumable_editor.py
Architecture Decision: ADR-0151 (P151).
Integration test suite for Medical Device, Needle, Test Strip, and Consumables Editor.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    DeviceCategory,
    DeviceConsumableConfig,
    MedicineProduct,
    NeedleGaugeChoice,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestDeviceConsumableEditorIntegration:
    """Integration test suite for P151 Medical Device & Consumable Editor."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p151-a-{self.suffix}",
            name=f"Popular Medical Devices {self.suffix}",
            subdomain=f"popular-p151-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p151-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p151-b-{self.suffix}",
            name=f"Labaid Surgical {self.suffix}",
            subdomain=f"labaid-p151-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p151-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p151-susp-{self.suffix}",
            name=f"Suspended Supplies {self.suffix}",
            subdomain=f"suspended-p151-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p151-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharma_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_tenant_b = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@labaid.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Labaid Owner {self.suffix}",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Susp Owner {self.suffix}",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 3. Products
        self.product_needle = MedicineProduct.objects.create(
            product_id=f"prod-needle-{self.suffix}",
            brand_name="BD Ultra-Fine Pen Needles",
            generic_name="Insulin Pen Needles",
            dosage_form="DEVICE",
            strength="31G 5mm",
            manufacturer="Becton Dickinson",
            mrp_unit=Decimal("30.00"),
            cost_unit=Decimal("25.00"),
        )
        self.product_strips = MedicineProduct.objects.create(
            product_id=f"prod-strips-{self.suffix}",
            brand_name="Accu-Chek Instant Test Strips",
            generic_name="Blood Glucose Test Strips",
            dosage_form="STRIP",
            strength="50s Pack",
            manufacturer="Roche Diabetes Care",
            mrp_unit=Decimal("1350.00"),
            cost_unit=Decimal("1200.00"),
        )
        self.product_tenant_b = MedicineProduct.objects.create(
            product_id=f"prod-b-{self.suffix}",
            brand_name="OneTouch Select Plus Strips",
            generic_name="Blood Glucose Strips",
            dosage_form="STRIP",
            strength="25s",
            manufacturer="Lifescan",
            mrp_unit=Decimal("800.00"),
            cost_unit=Decimal("700.00"),
        )

    def test_schema_endpoint(self):
        """Validates schema endpoint returns categories, gauges, and forbidden medicine fields."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/devices/schema/")
        assert res.status_code == 200
        data = res.json()
        assert data["editor_type"] == "DEVICE_AND_CONSUMABLE_EDITOR"
        category_codes = [c["code"] for c in data["supported_categories"]]
        assert "NEEDLE_OR_SYRINGE" in category_codes
        assert "DIAGNOSTIC_TEST_STRIP" in category_codes
        assert "MONITORING_DEVICE" in category_codes
        assert "dosage_form" in data["forbidden_medicine_fields"]
        assert "is_antibiotic" in data["forbidden_medicine_fields"]

    def test_validate_valid_needle_payload(self):
        """Verifies validation succeeds for needle with gauge and length."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "BD Ultra-Fine",
            "model_number": "320140",
            "needle_gauge": "31G",
            "needle_length_mm": 5.0,
            "is_sterile": True,
            "is_single_use": True,
            "storage_temperature_celsius_min": 2.0,
            "storage_temperature_celsius_max": 30.0,
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 200
        assert res.json()["valid"] is True
        assert res.json()["validated_payload"]["needle_gauge"] == "31G"
        assert res.json()["validated_payload"]["needle_length_mm"] == 5.0

    def test_validate_valid_diagnostic_strip_payload(self):
        """Verifies validation succeeds for test strips with meter compatibility."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_strips.product_id,
            "device_category": "DIAGNOSTIC_TEST_STRIP",
            "brand_name": "Accu-Chek Instant",
            "meter_compatibility": "Accu-Chek Instant Meter",
            "strip_count_per_pack": 50,
            "sample_volume_microliter": 0.6,
            "test_time_seconds": 4,
            "is_sterile": False,
            "is_single_use": True,
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 200
        assert res.json()["valid"] is True
        assert res.json()["validated_payload"]["strip_count_per_pack"] == 50

    def test_validation_rejects_medicine_only_fields(self):
        """Rejects active antibiotic / dosage form pharmaceutical fields with ERR_MEDICINE_FIELD_REJECTED."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "BD Ultra-Fine",
            "needle_gauge": "31G",
            "needle_length_mm": 5.0,
            "dosage_form": "TABLET",  # Illegal
            "is_antibiotic": True,   # Illegal
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 422
        assert res.json().get("error_code") == "ERR_MEDICINE_FIELD_REJECTED"

    def test_validation_rejects_needle_without_gauge(self):
        """Needles must specify a valid gauge."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "BD Ultra-Fine",
            "needle_gauge": "NA",
            "needle_length_mm": 5.0,
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 422
        assert res.json().get("error_code") == "ERR_INVALID_NEEDLE_GAUGE"

    def test_validation_rejects_zero_needle_length(self):
        """Needles must have positive length in mm."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "BD Ultra-Fine",
            "needle_gauge": "31G",
            "needle_length_mm": 0.0,
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 422
        assert res.json().get("error_code") == "ERR_INVALID_NEEDLE_LENGTH"

    def test_validation_rejects_strip_missing_meter_compatibility(self):
        """Test strips must specify compatible glucometer."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_strips.product_id,
            "device_category": "DIAGNOSTIC_TEST_STRIP",
            "brand_name": "Generic Strips",
            "meter_compatibility": "",
            "strip_count_per_pack": 50,
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 422
        assert res.json().get("error_code") == "ERR_MISSING_METER_COMPATIBILITY"

    def test_validation_rejects_invalid_temperature_range(self):
        """Storage min temperature cannot exceed max temperature."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_strips.product_id,
            "device_category": "DIAGNOSTIC_TEST_STRIP",
            "brand_name": "Accu-Chek",
            "meter_compatibility": "Accu-Chek Instant",
            "strip_count_per_pack": 50,
            "storage_temperature_celsius_min": 35.0,
            "storage_temperature_celsius_max": 25.0,
        }
        res = self.client.post("/api/v2/catalog/devices/validate/", payload, format="json")
        assert res.status_code == 422
        assert res.json().get("error_code") == "ERR_INVALID_TEMPERATURE_RANGE"

    def test_create_device_config_by_pharmacist(self):
        """Pharmacist successfully creates needle configuration with audit logging."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "BD Ultra-Fine PRO",
            "model_number": "UF-31G-4MM",
            "needle_gauge": "31G",
            "needle_length_mm": 4.0,
            "is_sterile": True,
            "is_single_use": True,
            "regulatory_device_class": "CLASS_B_LOW_MODERATE",
            "storage_temperature_celsius_min": 2.0,
            "storage_temperature_celsius_max": 30.0,
        }
        res = self.client.post("/api/v2/catalog/devices/", payload, format="json")
        assert res.status_code == 201
        data = res.json()
        assert data["brand_name"] == "BD Ultra-Fine PRO"
        assert data["needle_gauge"] == "31G"
        assert data["needle_length_mm"] == 4.0
        assert data["is_sterile"] is True
        assert data["device_category_display_bn"] == "সূঁচ বা সিরিঞ্জ"

        # Verify DB persistence
        config = DeviceConsumableConfig.objects.get(pk=data["id"])
        assert config.brand_name == "BD Ultra-Fine PRO"
        assert config.tenant == self.tenant_a

    def test_cashier_write_forbidden(self):
        """Cashier role is restricted to read-only access (HTTP 403 ERR_ROLE_UNAUTHORIZED)."""
        self.client.force_authenticate(user=self.user_cashier)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "BD Ultra-Fine",
            "needle_gauge": "31G",
            "needle_length_mm": 4.0,
            "is_sterile": True,
        }
        res = self.client.post("/api/v2/catalog/devices/", payload, format="json")
        assert res.status_code == 403
        assert res.json().get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_cross_tenant_isolation(self):
        """User from Tenant B cannot access or modify Tenant A's device configuration."""
        config_a = DeviceConsumableConfig.objects.create(
            product=self.product_needle,
            tenant=self.tenant_a,
            device_category=DeviceCategory.NEEDLE_OR_SYRINGE,
            brand_name="BD Ultra-Fine",
            needle_gauge=NeedleGaugeChoice.G31,
            needle_length_mm=Decimal("5.00"),
            is_sterile=True,
            is_single_use=True,
        )

        self.client.force_authenticate(user=self.user_tenant_b)
        # Attempt to retrieve Tenant A's config
        res_get = self.client.get(f"/api/v2/catalog/devices/{config_a.id}/")
        assert res_get.status_code == 403
        assert res_get.json().get("error_code") == "ERR_CROSS_TENANT_ACCESS_DENIED"

        # Attempt to patch Tenant A's config
        res_patch = self.client.patch(f"/api/v2/catalog/devices/{config_a.id}/", {"brand_name": "Hacked Brand"}, format="json")
        assert res_patch.status_code == 403
        assert res_patch.json().get("error_code") == "ERR_CROSS_TENANT_ACCESS_DENIED"

    def test_suspended_tenant_mutation_blocked(self):
        """Suspended tenant user attempting mutation gets blocked with HTTP 403."""
        self.client.force_authenticate(user=self.user_suspended)
        payload = {
            "product_id": self.product_needle.product_id,
            "device_category": "NEEDLE_OR_SYRINGE",
            "brand_name": "Suspended Attempt",
            "needle_gauge": "30G",
            "needle_length_mm": 8.0,
        }
        res = self.client.post("/api/v2/catalog/devices/", payload, format="json")
        assert res.status_code == 403
        assert res.json().get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        ]

    def test_retrieve_and_list_device_configs(self):
        """Retrieves and lists device configurations with accurate tenant filtering."""
        config = DeviceConsumableConfig.objects.create(
            product=self.product_strips,
            tenant=self.tenant_a,
            device_category=DeviceCategory.DIAGNOSTIC_TEST_STRIP,
            brand_name="Accu-Chek Instant",
            meter_compatibility="Accu-Chek Instant Meter",
            strip_count_per_pack=50,
            is_sterile=False,
            is_single_use=True,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        # 1. Retrieve by PK
        res = self.client.get(f"/api/v2/catalog/devices/{config.id}/")
        assert res.status_code == 200
        assert res.json()["brand_name"] == "Accu-Chek Instant"
        assert res.json()["strip_count_per_pack"] == 50

        # 2. List with filtering
        res_list = self.client.get("/api/v2/catalog/devices/?device_category=DIAGNOSTIC_TEST_STRIP")
        assert res_list.status_code == 200
        assert len(res_list.json()) >= 1
        assert res_list.json()[0]["id"] == str(config.id)

    def test_partial_update_device_config(self):
        """PATCH updates fields on an existing device configuration."""
        config = DeviceConsumableConfig.objects.create(
            product=self.product_needle,
            tenant=self.tenant_a,
            device_category=DeviceCategory.NEEDLE_OR_SYRINGE,
            brand_name="BD Ultra-Fine",
            needle_gauge=NeedleGaugeChoice.G31,
            needle_length_mm=Decimal("5.00"),
            is_sterile=True,
            is_single_use=True,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        update_payload = {
            "model_number": "UF-REV-2",
            "needle_length_mm": 6.0,
        }
        res = self.client.patch(f"/api/v2/catalog/devices/{config.id}/", update_payload, format="json")
        assert res.status_code == 200
        assert res.json()["model_number"] == "UF-REV-2"
        assert res.json()["needle_length_mm"] == 6.0

        config.refresh_from_db()
        assert config.model_number == "UF-REV-2"
        assert config.needle_length_mm == Decimal("6.00")

    def test_archive_device_config(self):
        """DELETE archives device configuration by setting is_active=False."""
        config = DeviceConsumableConfig.objects.create(
            product=self.product_needle,
            tenant=self.tenant_a,
            device_category=DeviceCategory.NEEDLE_OR_SYRINGE,
            brand_name="BD Ultra-Fine",
            needle_gauge=NeedleGaugeChoice.G31,
            needle_length_mm=Decimal("5.00"),
            is_sterile=True,
            is_single_use=True,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.delete(f"/api/v2/catalog/devices/{config.id}/")
        assert res.status_code == 200
        assert res.json()["status"] == "ARCHIVED"

        config.refresh_from_db()
        assert config.is_active is False
