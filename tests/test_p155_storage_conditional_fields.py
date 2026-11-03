"""
backend/tests/test_p155_storage_conditional_fields.py
Architecture Decision: ADR-0155 (P155).
Integration test suite for Conditional Cold-Chain Storage Fields and Handling Controls.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    StorageConditionalConfig,
    StoragePlacementZone,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestStorageConditionalFieldsIntegration:
    """Integration test suite for P155 Storage Conditional Fields."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p155-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p155-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p155-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch A",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p155-b-{self.suffix}",
            name=f"Beximco Health {self.suffix}",
            subdomain=f"beximco-p155-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p155-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Main Branch B",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p155-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"suspended-p155-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p155-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_tenant_b = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@beximco.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Beximco Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
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
        self.product_insulin = MedicineProduct.objects.create(
            product_id=f"prod-ins-{self.suffix}",
            brand_name=f"Humulin 70/30 {self.suffix}",
            generic_name="Biphasic Isophane Insulin",
            dosage_form="INJECTION",
            strength="100 IU/ml",
            manufacturer="Eli Lilly",
            cost_unit=Decimal("450.00"),
            mrp_unit=Decimal("520.00"),
        )
        self.product_tablet = MedicineProduct.objects.create(
            product_id=f"prod-tab-{self.suffix}",
            brand_name=f"Ace Plus {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="TABLET",
            strength="500mg + 65mg",
            manufacturer="Square Pharmaceuticals",
            cost_unit=Decimal("2.00"),
            mrp_unit=Decimal("2.50"),
        )

    def test_storage_schema(self):
        """Validates canonical storage conditions schema endpoint returns placement zones and actions."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/storage-conditions/schema/")
        assert res.status_code == 200
        data = res.json()
        assert data["editor_type"] == "STORAGE_CONDITIONAL_FORM"
        assert len(data["supported_placement_zones"]) >= 5
        assert len(data["supported_excursion_actions"]) >= 4
        assert data["default_cold_chain_min"] == 2.0
        assert data["default_cold_chain_max"] == 8.0

    def test_validate_storage_cold_chain_valid(self):
        """Dry-run validation passes for valid cold-chain product parameters."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
            "min_temperature_celsius": 2.0,
            "max_temperature_celsius": 8.0,
            "refrigerator_placement": "SHELF_MIDDLE",
            "max_excursion_hours": 24,
            "excursion_handling_action": "QUARANTINE_IMMEDIATELY",
            "requires_ice_pack_transport": True,
            "protect_from_freezing": True,
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/validate-storage/", data=payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["is_cold_chain_required"] is True
        assert data["min_temperature_celsius"] == 2.0
        assert data["max_temperature_celsius"] == 8.0
        assert data["refrigerator_placement"] == "SHELF_MIDDLE"

    def test_validate_storage_ambient_clears_cold_chain(self):
        """Non-cold-chain product cleanly clears cold chain parameters."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_tablet.product_id),
            "is_cold_chain_required": False,
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/validate-storage/", data=payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["is_cold_chain_required"] is False
        assert data["requires_ice_pack_transport"] is False
        assert data["protect_from_freezing"] is False

    def test_invalid_temperature_range_rejected(self):
        """Min temperature >= max temperature triggers 422 ERR_INVALID_TEMPERATURE_RANGE."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
            "min_temperature_celsius": 8.0,
            "max_temperature_celsius": 2.0,
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/validate-storage/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_TEMPERATURE_RANGE"

    def test_refrigerator_door_placement_prohibited(self):
        """Refrigerator door placement triggers 422 ERR_PROHIBITED_DOOR_PLACEMENT."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
            "min_temperature_celsius": 2.0,
            "max_temperature_celsius": 8.0,
            "refrigerator_placement": "DOOR_NOT_PERMITTED",
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/validate-storage/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_PROHIBITED_DOOR_PLACEMENT"

    def test_invalid_excursion_hours_rejected(self):
        """Excursion hours > 72 triggers 422 ERR_INVALID_EXCURSION_HOURS."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
            "min_temperature_celsius": 2.0,
            "max_temperature_celsius": 8.0,
            "max_excursion_hours": 100,
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/validate-storage/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_EXCURSION_HOURS"

    def test_create_cold_chain_config_by_pharmacist(self):
        """Pharmacist can configure cold chain settings and synchronize MedicineProduct."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
            "min_temperature_celsius": 2.0,
            "max_temperature_celsius": 8.0,
            "refrigerator_placement": "SHELF_MIDDLE",
            "max_excursion_hours": 24,
            "excursion_handling_action": "QUARANTINE_IMMEDIATELY",
            "requires_ice_pack_transport": True,
            "protect_from_freezing": True,
            "handling_alert_notes": "Do not shake. Protect from freezing.",
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/", data=payload, format="json")
        assert res.status_code == 201
        data = res.json()
        assert data["is_cold_chain_required"] is True
        assert data["refrigerator_placement_bn"] == "মাঝের তাক (স্থির তাপমাত্রা)"

        # Verify DB entry
        config_id = data["config_id"]
        cfg = StorageConditionalConfig.objects.get(pk=config_id)
        assert cfg.protect_from_freezing is True

        # Verify MedicineProduct synchronized
        self.product_insulin.refresh_from_db()
        assert self.product_insulin.requires_cold_chain is True
        assert self.product_insulin.min_temp_celsius == Decimal("2.00")
        assert self.product_insulin.max_temp_celsius == Decimal("8.00")

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(resource_id=config_id, action="STORAGE_CONDITIONS_CREATED").first()
        assert audit is not None
        assert audit.actor_email == self.user_pharmacist.email

    def test_cashier_write_forbidden(self):
        """Cashier role is restricted from configuring storage conditions."""
        self.client.force_authenticate(user=self.user_cashier)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_suspended_tenant_mutation_blocked(self):
        """Suspended tenant user is blocked from modifying storage configurations."""
        self.client.force_authenticate(user=self.user_suspended)
        payload = {
            "product_id": str(self.product_insulin.product_id),
            "is_cold_chain_required": True,
        }
        res = self.client.post("/api/v2/catalog/storage-conditions/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_list_storage_configs_tenant_isolation(self):
        """Tenant A cannot see Tenant B's storage condition configs."""
        cfg_a = StorageConditionalConfig.objects.create(
            config_id=f"cfg-a-{self.suffix}",
            product=self.product_insulin,
            tenant=self.tenant_a,
            is_cold_chain_required=True,
            min_temperature_celsius=Decimal("2.00"),
            max_temperature_celsius=Decimal("8.00"),
        )
        cfg_b = StorageConditionalConfig.objects.create(
            config_id=f"cfg-b-{self.suffix}",
            product=self.product_tablet,
            tenant=self.tenant_b,
            is_cold_chain_required=False,
            min_temperature_celsius=Decimal("15.00"),
            max_temperature_celsius=Decimal("25.00"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/storage-conditions/")
        assert res.status_code == 200
        ids = [c["config_id"] for c in res.json()]
        assert cfg_a.config_id in ids
        assert cfg_b.config_id not in ids

    def test_cross_tenant_patch_prohibited(self):
        """Tenant B user cannot update Tenant A's storage configuration."""
        cfg_a = StorageConditionalConfig.objects.create(
            config_id=f"cfg-cross-{self.suffix}",
            product=self.product_insulin,
            tenant=self.tenant_a,
            is_cold_chain_required=True,
            min_temperature_celsius=Decimal("2.00"),
            max_temperature_celsius=Decimal("8.00"),
        )

        self.client.force_authenticate(user=self.user_tenant_b)
        res = self.client.patch(
            f"/api/v2/catalog/storage-conditions/{cfg_a.config_id}/",
            data={"max_excursion_hours": 12},
            format="json",
        )
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS_DENIED"

    def test_partial_update_storage_config(self):
        """Pharmacist can update placement and excursion hours."""
        cfg = StorageConditionalConfig.objects.create(
            config_id=f"cfg-upd-{self.suffix}",
            product=self.product_insulin,
            tenant=self.tenant_a,
            is_cold_chain_required=True,
            min_temperature_celsius=Decimal("2.00"),
            max_temperature_celsius=Decimal("8.00"),
            refrigerator_placement=StoragePlacementZone.SHELF_MIDDLE,
            max_excursion_hours=24,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.patch(
            f"/api/v2/catalog/storage-conditions/{cfg.config_id}/",
            data={"refrigerator_placement": "CHILLER_TRAY", "max_excursion_hours": 12},
            format="json",
        )
        assert res.status_code == 200
        data = res.json()
        assert data["refrigerator_placement"] == "CHILLER_TRAY"
        assert data["max_excursion_hours"] == 12

    def test_soft_delete_and_archive(self):
        """Archiving a storage config sets is_active=False and logs audit entry."""
        cfg = StorageConditionalConfig.objects.create(
            config_id=f"cfg-del-{self.suffix}",
            product=self.product_insulin,
            tenant=self.tenant_a,
            is_cold_chain_required=True,
            min_temperature_celsius=Decimal("2.00"),
            max_temperature_celsius=Decimal("8.00"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.delete(f"/api/v2/catalog/storage-conditions/{cfg.config_id}/")
        assert res.status_code == 200
        assert res.json()["status"] == "ARCHIVED"

        cfg.refresh_from_db()
        assert cfg.is_active is False
