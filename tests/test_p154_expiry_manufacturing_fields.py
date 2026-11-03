"""
backend/tests/test_p154_expiry_manufacturing_fields.py
Architecture Decision: ADR-0154 (P154).
Integration test suite for Regulated Stock Entry, Expiry, Manufacturing Date, and Lot Controls.
"""

import random
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    RegulatedStockEntry,
    StorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestExpiryManufacturingFieldsIntegration:
    """Integration test suite for P154 Expiry and Manufacturing Fields."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p154-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p154-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p154-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch A",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p154-b-{self.suffix}",
            name=f"Beximco Health {self.suffix}",
            subdomain=f"beximco-p154-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p154-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Main Branch B",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p154-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"suspended-p154-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p154-susp-{self.suffix}",
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

        # 3. Medicine Product
        self.product_1 = MedicineProduct.objects.create(
            product_id=f"prod-1-{self.suffix}",
            brand_name=f"Seclo 20 {self.suffix}",
            generic_name="Omeprazole 20mg",
            dosage_form="CAPSULE",
            strength="20mg",
            manufacturer="Square Pharmaceuticals",
            cost_unit=Decimal("4.50"),
            mrp_unit=Decimal("6.00"),
        )
        self.product_2 = MedicineProduct.objects.create(
            product_id=f"prod-2-{self.suffix}",
            brand_name=f"Monas 10 {self.suffix}",
            generic_name="Montelukast 10mg",
            dosage_form="TABLET",
            strength="10mg",
            manufacturer="Acme Laboratories",
            cost_unit=Decimal("12.00"),
            mrp_unit=Decimal("16.00"),
        )

    def test_stock_entry_schema(self):
        """Validates canonical stock entry schema endpoint returns storage conditions and constraints."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/stock-entry/schema/")
        assert res.status_code == 200
        data = res.json()
        assert data["editor_type"] == "REGULATED_STOCK_ENTRY_FORM"
        assert len(data["supported_storage_conditions"]) >= 6
        assert data["default_quarantine_lead_days"] == 30

    def test_validate_intake_valid(self):
        """Validates standard compliant stock intake dry-run."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today - timedelta(days=90)).isoformat()
        exp = (self.today + timedelta(days=500)).isoformat()
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": f"SQ-LOT-{self.suffix[:4]}",
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 500,
            "unit_cost_bdt": 4.50,
            "storage_condition": "ROOM_TEMPERATURE_15_25C",
        }
        res = self.client.post("/api/v2/catalog/stock-entry/validate-intake/", data=payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["shelf_life_days"] == 590
        assert data["is_quarantined"] is False

    def test_future_manufacturing_date_rejected(self):
        """Future manufacturing date triggers 422 ERR_FUTURE_MANUFACTURING_DATE."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today + timedelta(days=5)).isoformat()
        exp = (self.today + timedelta(days=400)).isoformat()
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT-FUT-01",
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 100,
            "unit_cost_bdt": 4.50,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/validate-intake/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_FUTURE_MANUFACTURING_DATE"

    def test_expiry_before_or_equal_manufacturing_rejected(self):
        """Expiration date before manufacturing date triggers 422 ERR_EXPIRY_BEFORE_MANUFACTURING."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today - timedelta(days=30)).isoformat()
        exp = (self.today - timedelta(days=40)).isoformat()
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT-INV-01",
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 100,
            "unit_cost_bdt": 4.50,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/validate-intake/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_EXPIRY_BEFORE_MANUFACTURING"

    def test_expired_medicine_active_intake_rejected(self):
        """Expired stock intake without quarantine flag triggers 422 ERR_STOCK_ALREADY_EXPIRED."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today - timedelta(days=400)).isoformat()
        exp = (self.today - timedelta(days=10)).isoformat()
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT-EXP-01",
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 50,
            "unit_cost_bdt": 4.50,
            "is_quarantined": False,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/validate-intake/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_STOCK_ALREADY_EXPIRED"

    def test_invalid_lot_reference_format(self):
        """Invalid lot reference format or characters triggers 422 ERR_INVALID_LOT_FORMAT."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today - timedelta(days=30)).isoformat()
        exp = (self.today + timedelta(days=300)).isoformat()
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT#BAD@CHARS$",
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 100,
            "unit_cost_bdt": 4.50,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/validate-intake/", data=payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_LOT_FORMAT"

    def test_near_expiry_auto_quarantined(self):
        """Stock expiring within quarantine lead window (e.g. 15 days <= 30) is auto-quarantined."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today - timedelta(days=180)).isoformat()
        exp = (self.today + timedelta(days=15)).isoformat()
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT-NEAR-EXP",
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 100,
            "unit_cost_bdt": 4.50,
            "quarantine_lead_days": 30,
            "is_quarantined": False,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/validate-intake/", data=payload, format="json")
        assert res.status_code == 200
        assert res.json()["is_quarantined"] is True

    def test_create_stock_entry_by_pharmacist(self):
        """Pharmacist can record stock intake, synchronizing BatchStock and AuditLogEntry."""
        self.client.force_authenticate(user=self.user_pharmacist)
        mfg = (self.today - timedelta(days=60)).isoformat()
        exp = (self.today + timedelta(days=700)).isoformat()
        lot = f"LOT-CREATE-{self.suffix}"
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": lot,
            "manufacturing_date": mfg,
            "expiry_date": exp,
            "quantity_received": 250,
            "unit_cost_bdt": 4.50,
            "storage_condition": "ROOM_TEMPERATURE_15_25C",
            "supplier_reference": f"CHALLAN-{self.suffix[:4]}",
            "entry_notes": "Received in good condition",
        }
        res = self.client.post("/api/v2/catalog/stock-entry/", data=payload, format="json")
        assert res.status_code == 201
        data = res.json()
        assert data["lot_reference"] == lot
        assert data["quantity_received"] == 250
        assert data["batch_id"] is not None

        # Check DB RegulatedStockEntry
        entry_id = data["entry_id"]
        entry = RegulatedStockEntry.objects.get(pk=entry_id)
        assert entry.unit_cost_bdt == Decimal("4.5000")
        assert entry.shelf_life_days == 760

        # Check BatchStock synchronization
        batch = BatchStock.objects.get(tenant=self.tenant_a, batch_number=lot)
        assert batch.quantity_base_units == 250
        assert batch.lot_reference == lot
        assert batch.manufacturing_date == entry.manufacturing_date

        # Check Audit Log
        audit = AuditLogEntry.objects.filter(resource_id=entry_id, action="REGULATED_STOCK_ENTRY_RECORDED").first()
        assert audit is not None
        assert audit.actor_email == self.user_pharmacist.email

    def test_cashier_write_forbidden(self):
        """Cashier role is prohibited from creating stock intake records."""
        self.client.force_authenticate(user=self.user_cashier)
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT-CASHIER",
            "manufacturing_date": (self.today - timedelta(days=30)).isoformat(),
            "expiry_date": (self.today + timedelta(days=365)).isoformat(),
            "quantity_received": 100,
            "unit_cost_bdt": 4.50,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_suspended_tenant_mutation_blocked(self):
        """Suspended tenant user is blocked from creating stock intake records."""
        self.client.force_authenticate(user=self.user_suspended)
        payload = {
            "product_id": str(self.product_1.product_id),
            "lot_reference": "LOT-SUSP",
            "manufacturing_date": (self.today - timedelta(days=30)).isoformat(),
            "expiry_date": (self.today + timedelta(days=365)).isoformat(),
            "quantity_received": 100,
            "unit_cost_bdt": 4.50,
        }
        res = self.client.post("/api/v2/catalog/stock-entry/", data=payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_list_stock_entries_tenant_isolation(self):
        """Tenant A can only view their own stock entry records; Tenant B's records are isolated."""
        mfg = self.today - timedelta(days=30)
        exp = self.today + timedelta(days=365)
        # Entry for Tenant A
        entry_a = RegulatedStockEntry.objects.create(
            entry_id=f"stk-a-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_1,
            lot_reference=f"LOTA-{self.suffix}",
            manufacturing_date=mfg,
            expiry_date=exp,
            quantity_received=100,
            unit_cost_bdt=Decimal("4.5000"),
        )
        # Entry for Tenant B
        entry_b = RegulatedStockEntry.objects.create(
            entry_id=f"stk-b-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            product=self.product_2,
            lot_reference=f"LOTB-{self.suffix}",
            manufacturing_date=mfg,
            expiry_date=exp,
            quantity_received=200,
            unit_cost_bdt=Decimal("12.0000"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/stock-entry/")
        assert res.status_code == 200
        ids = [e["entry_id"] for e in res.json()]
        assert entry_a.entry_id in ids
        assert entry_b.entry_id not in ids

    def test_cross_tenant_patch_prohibited(self):
        """User from Tenant B cannot modify Tenant A's stock entry record."""
        entry_a = RegulatedStockEntry.objects.create(
            entry_id=f"stk-cross-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_1,
            lot_reference=f"LOTCROSS-{self.suffix}",
            manufacturing_date=self.today - timedelta(days=30),
            expiry_date=self.today + timedelta(days=365),
            quantity_received=100,
            unit_cost_bdt=Decimal("4.5000"),
        )

        self.client.force_authenticate(user=self.user_tenant_b)
        res = self.client.patch(
            f"/api/v2/catalog/stock-entry/{entry_a.entry_id}/",
            data={"entry_notes": "Attempted illegal cross tenant edit"},
            format="json",
        )
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS_DENIED"

    def test_partial_update_storage_condition_and_quarantine(self):
        """Pharmacist can update storage condition and quarantine flag, updating underlying batch."""
        batch = BatchStock.objects.create(
            batch_id=f"batch-upd-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_1,
            batch_number=f"LOTUPD-{self.suffix}",
            lot_reference=f"LOTUPD-{self.suffix}",
            quantity_base_units=100,
            expiry_date=self.today + timedelta(days=365),
            purchase_date=self.today,
            cost_per_unit=Decimal("4.50"),
            status=BatchStatus.ACTIVE,
        )
        entry = RegulatedStockEntry.objects.create(
            entry_id=f"stk-upd-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_1,
            batch=batch,
            lot_reference=batch.batch_number,
            manufacturing_date=self.today - timedelta(days=30),
            expiry_date=batch.expiry_date,
            quantity_received=100,
            unit_cost_bdt=Decimal("4.5000"),
            storage_condition=StorageCondition.ROOM_TEMPERATURE,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.patch(
            f"/api/v2/catalog/stock-entry/{entry.entry_id}/",
            data={"storage_condition": "COLD_CHAIN_2_8C", "is_quarantined": True},
            format="json",
        )
        assert res.status_code == 200
        data = res.json()
        assert data["storage_condition"] == "COLD_CHAIN_2_8C"
        assert data["is_quarantined"] is True

        batch.refresh_from_db()
        assert batch.storage_condition == "COLD_CHAIN_2_8C"
        assert batch.is_quarantined is True
        assert batch.status == BatchStatus.QUARANTINED

    def test_soft_delete_and_archive(self):
        """Soft deleting an entry sets is_active=False and records audit log."""
        entry = RegulatedStockEntry.objects.create(
            entry_id=f"stk-del-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_1,
            lot_reference=f"LOTDEL-{self.suffix}",
            manufacturing_date=self.today - timedelta(days=30),
            expiry_date=self.today + timedelta(days=365),
            quantity_received=100,
            unit_cost_bdt=Decimal("4.5000"),
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.delete(f"/api/v2/catalog/stock-entry/{entry.entry_id}/")
        assert res.status_code == 200
        assert res.json()["status"] == "ARCHIVED"

        entry.refresh_from_db()
        assert entry.is_active is False
