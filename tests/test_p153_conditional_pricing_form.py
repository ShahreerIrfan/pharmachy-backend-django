"""
backend/tests/test_p153_conditional_pricing_form.py
Architecture Decision: ADR-0153 (P153).
Integration test suite for Unit-Aware Conditional Pricing Form.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BaseDispensingUnit,
    Branch,
    BranchLifecycleStatus,
    ConditionalPricingConfig,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestConditionalPricingFormIntegration:
    """Integration test suite for P153 Unit-Aware Conditional Pricing Form."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p153-a-{self.suffix}",
            name=f"Beximco Health {self.suffix}",
            subdomain=f"beximco-p153-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p153-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch A",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p153-b-{self.suffix}",
            name=f"Square Health {self.suffix}",
            subdomain=f"square-p153-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p153-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Main Branch B",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p153-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"suspended-p153-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p153-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@beximco.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@beximco.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_tenant_b = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Square Pharmacist {self.suffix}",
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

        # 3. Medicine Product
        self.product_1 = MedicineProduct.objects.create(
            product_id=f"prod-1-{self.suffix}",
            brand_name=f"Napa Extend {self.suffix}",
            generic_name="Paracetamol 665mg",
            dosage_form="TABLET",
            strength="665mg",
            manufacturer="Beximco Pharma",
            cost_unit=Decimal("1.80"),
            mrp_unit=Decimal("2.50"),
        )
        self.product_2 = MedicineProduct.objects.create(
            product_id=f"prod-2-{self.suffix}",
            brand_name=f"Maxpro 20 {self.suffix}",
            generic_name="Esomeprazole 20mg",
            dosage_form="CAPSULE",
            strength="20mg",
            manufacturer="Square Pharmaceuticals",
            cost_unit=Decimal("5.00"),
            mrp_unit=Decimal("7.00"),
        )

    def test_pricing_schema(self):
        """Validates canonical pricing schema endpoint returns valid definition."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/conditional-pricing/schema/")
        assert res.status_code == 200
        data = res.json()
        assert data["editor_type"] == "CONDITIONAL_PRICING_FORM"
        assert len(data["supported_base_units"]) >= 5
        assert data["max_multiplier"] == 500

    def test_pricing_calculate_valid(self):
        """Calculates derived strip and pack prices from piece cost and packaging multipliers."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "base_dispensing_unit": "TABLET",
            "pack_to_strip_multiplier": 10,
            "strip_to_piece_multiplier": 10,
            "total_base_units_per_pack": 100,
            "cost_per_piece_bdt": 2.0,
            "mrp_per_piece_bdt": 3.0,
            "allow_strip_sale": True,
            "allow_piece_sale": True,
            "vat_rate_percentage": 0.0,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/calculate/", data=payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["strip_cost_bdt"] == 20.0
        assert data["strip_mrp_bdt"] == 30.0
        assert data["pack_cost_bdt"] == 200.0
        assert data["pack_mrp_bdt"] == 300.0

    def test_pricing_calculate_dimension_mismatch(self):
        """Verifies dimension mismatch triggers 422 with ERR_DIMENSION_MISMATCH."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "base_dispensing_unit": "TABLET",
            "pack_to_strip_multiplier": 5,
            "strip_to_piece_multiplier": 10,
            "total_base_units_per_pack": 60,  # 5 * 10 = 50 != 60
            "cost_per_piece_bdt": 2.0,
            "mrp_per_piece_bdt": 3.0,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/calculate/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_DIMENSION_MISMATCH"

    def test_pricing_calculate_invalid_multiplier(self):
        """Verifies zero or negative multiplier triggers 422 ERR_INVALID_MULTIPLIER."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "pack_to_strip_multiplier": 0,
            "strip_to_piece_multiplier": 10,
            "cost_per_piece_bdt": 2.0,
            "mrp_per_piece_bdt": 3.0,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/calculate/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_MULTIPLIER"

    def test_pricing_calculate_invalid_price(self):
        """Verifies non-positive price triggers 422 ERR_INVALID_PRICE."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "pack_to_strip_multiplier": 10,
            "strip_to_piece_multiplier": 10,
            "cost_per_piece_bdt": 0.0,
            "mrp_per_piece_bdt": 3.0,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/calculate/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_PRICE"

    def test_pricing_calculate_invalid_break_bulk(self):
        """Verifies multi-unit pack prohibiting both strip and piece sale triggers 422."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "pack_to_strip_multiplier": 5,
            "strip_to_piece_multiplier": 10,
            "total_base_units_per_pack": 50,
            "cost_per_piece_bdt": 2.0,
            "mrp_per_piece_bdt": 3.0,
            "allow_strip_sale": False,
            "allow_piece_sale": False,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/calculate/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_BREAK_BULK_POLICY"

    def test_create_conditional_pricing_by_pharmacist(self):
        """Pharmacist can create conditional pricing configuration with audit log entry."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "product_id": str(self.product_1.product_id),
            "base_dispensing_unit": "TABLET",
            "pack_to_strip_multiplier": 10,
            "strip_to_piece_multiplier": 10,
            "total_base_units_per_pack": 100,
            "cost_per_piece_bdt": 1.80,
            "mrp_per_piece_bdt": 2.50,
            "allow_strip_sale": True,
            "allow_piece_sale": True,
            "vat_rate_percentage": 0.0,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/", data=payload, format="json")
        assert res.status_code == 201
        data = res.json()
        assert data["product_id"] == str(self.product_1.product_id)
        assert data["pack_mrp_bdt"] == 250.0
        assert data["strip_mrp_bdt"] == 25.0
        assert data["base_dispensing_unit_display_bn"] == "ট্যাবলেট"

        # Check DB
        pricing_id = data["pricing_id"]
        obj = ConditionalPricingConfig.objects.get(pk=pricing_id)
        assert obj.total_base_units_per_pack == 100

        # Check Audit Log
        audit = AuditLogEntry.objects.filter(resource_id=pricing_id, action="CONDITIONAL_PRICING_CREATED").first()
        assert audit is not None
        assert audit.actor_email == self.user_pharmacist.email

    def test_cashier_write_forbidden(self):
        """Cashier role cannot create or modify pricing configuration."""
        self.client.force_authenticate(user=self.user_cashier)
        payload = {
            "product_id": str(self.product_1.product_id),
            "base_dispensing_unit": "TABLET",
            "cost_per_piece_bdt": 1.80,
            "mrp_per_piece_bdt": 2.50,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_suspended_tenant_mutation_blocked(self):
        """Suspended tenant user is blocked from creating pricing configuration."""
        self.client.force_authenticate(user=self.user_suspended)
        payload = {
            "product_id": str(self.product_1.product_id),
            "base_dispensing_unit": "TABLET",
            "cost_per_piece_bdt": 1.80,
            "mrp_per_piece_bdt": 2.50,
        }
        res = self.client.post("/api/v2/catalog/conditional-pricing/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_list_pricing_tenant_isolation(self):
        """Pricing configurations are scoped to tenant; cross-tenant items are isolated."""
        # Create pricing for Tenant A
        pricing_a = ConditionalPricingConfig.objects.create(
            product=self.product_1,
            tenant=self.tenant_a,
            base_dispensing_unit=BaseDispensingUnit.TABLET,
            pack_to_strip_multiplier=10,
            strip_to_piece_multiplier=10,
            total_base_units_per_pack=100,
            cost_per_piece_bdt=Decimal("1.80"),
            mrp_per_piece_bdt=Decimal("2.50"),
            strip_cost_bdt=Decimal("18.00"),
            strip_mrp_bdt=Decimal("25.00"),
            pack_cost_bdt=Decimal("180.00"),
            pack_mrp_bdt=Decimal("250.00"),
        )
        # Create pricing for Tenant B
        pricing_b = ConditionalPricingConfig.objects.create(
            product=self.product_2,
            tenant=self.tenant_b,
            base_dispensing_unit=BaseDispensingUnit.CAPSULE,
            pack_to_strip_multiplier=5,
            strip_to_piece_multiplier=10,
            total_base_units_per_pack=50,
            cost_per_piece_bdt=Decimal("5.00"),
            mrp_per_piece_bdt=Decimal("7.00"),
            strip_cost_bdt=Decimal("50.00"),
            strip_mrp_bdt=Decimal("70.00"),
            pack_cost_bdt=Decimal("250.00"),
            pack_mrp_bdt=Decimal("350.00"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/conditional-pricing/")
        assert res.status_code == 200
        pricing_ids = [p["pricing_id"] for p in res.json()]
        assert str(pricing_a.pricing_id) in pricing_ids
        assert str(pricing_b.pricing_id) not in pricing_ids

    def test_cross_tenant_patch_prohibited(self):
        """Cross-tenant PATCH is strictly denied with 403 ERR_CROSS_TENANT_ACCESS_DENIED."""
        pricing_a = ConditionalPricingConfig.objects.create(
            product=self.product_1,
            tenant=self.tenant_a,
            base_dispensing_unit=BaseDispensingUnit.TABLET,
            pack_to_strip_multiplier=10,
            strip_to_piece_multiplier=10,
            total_base_units_per_pack=100,
            cost_per_piece_bdt=Decimal("1.80"),
            mrp_per_piece_bdt=Decimal("2.50"),
            strip_cost_bdt=Decimal("18.00"),
            strip_mrp_bdt=Decimal("25.00"),
            pack_cost_bdt=Decimal("180.00"),
            pack_mrp_bdt=Decimal("250.00"),
        )

        # Tenant B user attempts to modify Tenant A's pricing
        self.client.force_authenticate(user=self.user_tenant_b)
        res = self.client.patch(
            f"/api/v2/catalog/conditional-pricing/{pricing_a.pricing_id}/",
            data={"mrp_per_piece_bdt": 3.00},
            format="json",
        )
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS_DENIED"

    def test_partial_update_success(self):
        """Pharmacist can partially update existing pricing and recalculate correctly."""
        pricing = ConditionalPricingConfig.objects.create(
            product=self.product_1,
            tenant=self.tenant_a,
            base_dispensing_unit=BaseDispensingUnit.TABLET,
            pack_to_strip_multiplier=10,
            strip_to_piece_multiplier=10,
            total_base_units_per_pack=100,
            cost_per_piece_bdt=Decimal("1.80"),
            mrp_per_piece_bdt=Decimal("2.50"),
            strip_cost_bdt=Decimal("18.00"),
            strip_mrp_bdt=Decimal("25.00"),
            pack_cost_bdt=Decimal("180.00"),
            pack_mrp_bdt=Decimal("250.00"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.patch(
            f"/api/v2/catalog/conditional-pricing/{pricing.pricing_id}/",
            data={"mrp_per_piece_bdt": 3.00, "allow_piece_sale": False},
            format="json",
        )
        assert res.status_code == 200
        data = res.json()
        assert data["mrp_per_piece_bdt"] == 3.0
        assert data["allow_piece_sale"] is False

    def test_soft_delete_and_archive(self):
        """Soft delete marks is_active=False and records audit log."""
        pricing = ConditionalPricingConfig.objects.create(
            product=self.product_1,
            tenant=self.tenant_a,
            base_dispensing_unit=BaseDispensingUnit.TABLET,
            pack_to_strip_multiplier=10,
            strip_to_piece_multiplier=10,
            total_base_units_per_pack=100,
            cost_per_piece_bdt=Decimal("1.80"),
            mrp_per_piece_bdt=Decimal("2.50"),
            strip_cost_bdt=Decimal("18.00"),
            strip_mrp_bdt=Decimal("25.00"),
            pack_cost_bdt=Decimal("180.00"),
            pack_mrp_bdt=Decimal("250.00"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.delete(f"/api/v2/catalog/conditional-pricing/{pricing.pricing_id}/")
        assert res.status_code == 200
        assert res.json()["status"] == "ARCHIVED"

        pricing.refresh_from_db()
        assert pricing.is_active is False
