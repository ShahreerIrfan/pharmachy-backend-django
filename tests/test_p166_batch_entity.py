"""
backend/tests/test_p166_batch_entity.py
Architecture Decision: ADR-0166 (P166).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Stable Medicine Batch Entity,
Expiry Lifecycle Constraints, FEFO Order, and Statutory Recall Links.
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    MedicineProduct,
    PlanTier,
    RecallScope,
    StorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestBatchEntityIntegration:
    """Integration test suite for P166 Batch Entity in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenant A & Branch A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p166-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p166-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p166-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Tenant B (for cross-tenant checks)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p166-b-{self.suffix}",
            name=f"Beximco Pharmacy {self.suffix}",
            subdomain=f"beximco-p166-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p166-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p166-susp-{self.suffix}",
            name="Suspended Tenant",
            subdomain=f"susp-p166-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p166-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Users
        self.pharmacist_user = PharmacyUser.objects.create_user(
            email=f"pharmacist-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacist User",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier User",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.user_b = PharmacyUser.objects.create_user(
            email=f"userb-{self.suffix}@pharmab.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="User B Manager",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        # 5. Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-p166-{self.suffix}",
            brand_name="Napa Extend 665mg",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="665mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.00"),
            mrp_unit=Decimal("2.50"),
        )

    def test_batch_creation_success(self):
        """Pharmacist creates valid batch; returns 201 and creates audit log."""
        self.client.force_authenticate(user=self.pharmacist_user)
        today = timezone.now().date()

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_number": f"BAT-OK-{self.suffix}",
            "quantity_base_units": 100,
            "cost_per_unit": "2.10",
            "manufacturing_date": (today - datetime.timedelta(days=120)).isoformat(),
            "expiry_date": (today + datetime.timedelta(days=365)).isoformat(),
            "storage_condition": StorageCondition.ROOM_TEMPERATURE,
            "lot_reference": "LOT-2026-A",
            "manufacturer_name": "Beximco Pharmaceuticals Ltd.",
        }

        resp = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data
        assert data["batch_number"] == f"BAT-OK-{self.suffix}"
        assert data["status"] == "ACTIVE"
        assert data["is_dispensable"] is True
        assert data["quantity_base_units"] == 100

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            tenant_id=self.tenant_a.tenant_id,
            action="BATCH_CREATED",
            resource_id=data["batch_id"],
        ).first()
        assert audit is not None
        assert audit.actor_role == UserRole.STAFF_PHARMACIST

    def test_batch_creation_expiry_before_manufacturing_blocked(self):
        """Rejects batch where expiry date is before or equal to manufacturing date."""
        self.client.force_authenticate(user=self.pharmacist_user)
        today = timezone.now().date()

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_number": f"BAT-INV-DATE-{self.suffix}",
            "quantity_base_units": 50,
            "cost_per_unit": "2.00",
            "manufacturing_date": today.isoformat(),
            "expiry_date": (today - datetime.timedelta(days=10)).isoformat(),
        }

        resp = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.data.get("error_code") == "ERR_EXPIRY_BEFORE_MANUFACTURING"

    def test_batch_creation_past_expiry_blocked_unless_quarantined(self):
        """Rejects active batch creation when expiry date is in the past."""
        self.client.force_authenticate(user=self.pharmacist_user)
        today = timezone.now().date()

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_number": f"BAT-PAST-{self.suffix}",
            "quantity_base_units": 50,
            "cost_per_unit": "2.00",
            "manufacturing_date": (today - datetime.timedelta(days=400)).isoformat(),
            "expiry_date": (today - datetime.timedelta(days=10)).isoformat(),
            "status": "ACTIVE",
            "is_quarantined": False,
        }

        resp = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.data.get("error_code") == "ERR_BATCH_EXPIRED_ON_CREATION"

    def test_batch_creation_duplicate_in_same_branch_blocked(self):
        """Blocks duplicate batch numbers within the same branch."""
        self.client.force_authenticate(user=self.pharmacist_user)
        today = timezone.now().date()

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_number": f"BAT-DUP-{self.suffix}",
            "quantity_base_units": 20,
            "cost_per_unit": "2.00",
            "expiry_date": (today + datetime.timedelta(days=200)).isoformat(),
        }

        resp1 = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp1.status_code == status.HTTP_201_CREATED

        resp2 = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp2.data.get("error_code") == "ERR_DUPLICATE_BATCH"

    def test_batch_creation_cashier_unauthorized(self):
        """Cashiers are strictly forbidden from creating batch inventory records."""
        self.client.force_authenticate(user=self.cashier_user)
        today = timezone.now().date()

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_number": f"BAT-CASHIER-{self.suffix}",
            "quantity_base_units": 20,
            "cost_per_unit": "2.00",
            "expiry_date": (today + datetime.timedelta(days=200)).isoformat(),
        }

        resp = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data.get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_batch_auto_detects_statutory_recall_notice(self):
        """Creating or retrieving a batch automatically links to active DGDA recall notice."""
        today = timezone.now().date()

        # Create active DGDA recall notice covering specific batch
        recalled_batch_num = f"BAT-RECALLED-{self.suffix}"
        notice = CatalogueLifecycleNotice.objects.create(
            notice_id=f"not-p166-{self.suffix}",
            notice_number=f"DGDA-REC-2026-{self.suffix}",
            product=self.product,
            lifecycle_status=CatalogueLifecycleStatus.RECALLED,
            recall_scope=RecallScope.SPECIFIC_BATCHES,
            affected_batch_numbers=[recalled_batch_num],
            regulatory_authority="Directorate General of Drug Administration (DGDA)",
            reason="Sub-standard dissolution profile identified during post-market surveillance.",
            effective_date=timezone.now(),
            is_active=True,
        )

        self.client.force_authenticate(user=self.pharmacist_user)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_number": recalled_batch_num,
            "quantity_base_units": 100,
            "cost_per_unit": "2.00",
            "expiry_date": (today + datetime.timedelta(days=300)).isoformat(),
        }

        resp = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.data
        assert data["status"] == "RECALLED"
        assert data["is_recalled"] is True
        assert data["is_dispensable"] is False
        assert data["recall_notice_id"] == notice.notice_id
        assert "Sub-standard dissolution" in data["recall_reason"]

    def test_validate_dispense_blocks_recalled_batch(self):
        """POS checkout pre-check endpoint blocks dispensing recalled batch."""
        today = timezone.now().date()
        batch = BatchStock.objects.create(
            batch_id=f"bat-rec-pos-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"REC-POS-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today,
            expiry_date=today + datetime.timedelta(days=180),
            status=BatchStatus.RECALLED,
            recall_reason="Statutory recall by manufacturer",
        )

        self.client.force_authenticate(user=self.cashier_user)
        resp = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/validate-dispense/",
            {"tenant_id": self.tenant_a.tenant_id, "requested_units": 5},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_dispensable"] is False
        assert data["error_code"] == "ERR_BATCH_RECALLED"

    def test_validate_dispense_blocks_expired_batch(self):
        """POS checkout pre-check endpoint blocks dispensing expired batch."""
        today = timezone.now().date()
        batch = BatchStock.objects.create(
            batch_id=f"bat-exp-pos-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"EXP-POS-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today - datetime.timedelta(days=200),
            expiry_date=today - datetime.timedelta(days=5),
            status=BatchStatus.EXPIRED,
        )

        self.client.force_authenticate(user=self.cashier_user)
        resp = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/validate-dispense/",
            {"tenant_id": self.tenant_a.tenant_id, "requested_units": 5},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_dispensable"] is False
        assert data["error_code"] == "ERR_BATCH_EXPIRED"

    def test_validate_dispense_blocks_quarantined_batch(self):
        """POS checkout pre-check endpoint blocks dispensing quarantined batch."""
        today = timezone.now().date()
        batch = BatchStock.objects.create(
            batch_id=f"bat-quar-pos-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"QUAR-POS-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today,
            expiry_date=today + datetime.timedelta(days=200),
            status=BatchStatus.QUARANTINED,
            is_quarantined=True,
        )

        self.client.force_authenticate(user=self.cashier_user)
        resp = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/validate-dispense/",
            {"tenant_id": self.tenant_a.tenant_id, "requested_units": 5},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_dispensable"] is False
        assert data["error_code"] == "ERR_BATCH_QUARANTINED"

    def test_validate_dispense_blocks_insufficient_stock(self):
        """POS checkout pre-check endpoint blocks dispensing when stock is insufficient."""
        today = timezone.now().date()
        batch = BatchStock.objects.create(
            batch_id=f"bat-low-pos-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"LOW-POS-{self.suffix}",
            quantity_base_units=5,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today,
            expiry_date=today + datetime.timedelta(days=200),
            status=BatchStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.cashier_user)
        resp = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/validate-dispense/",
            {"tenant_id": self.tenant_a.tenant_id, "requested_units": 20},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_dispensable"] is False
        assert data["error_code"] == "ERR_INSUFFICIENT_STOCK"

    def test_validate_dispense_allows_valid_batch(self):
        """POS checkout pre-check endpoint allows dispensing valid active batch."""
        today = timezone.now().date()
        batch = BatchStock.objects.create(
            batch_id=f"bat-ok-pos-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"OK-POS-{self.suffix}",
            quantity_base_units=100,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today,
            expiry_date=today + datetime.timedelta(days=200),
            status=BatchStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.cashier_user)
        resp = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/validate-dispense/",
            {"tenant_id": self.tenant_a.tenant_id, "requested_units": 10},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_dispensable"] is True
        assert data["error_code"] is None
        assert data["available_units"] == 100

    def test_status_transitions_and_audit(self):
        """Audited transitions between ACTIVE, QUARANTINED, and ARCHIVED."""
        self.client.force_authenticate(user=self.pharmacist_user)
        today = timezone.now().date()
        batch = BatchStock.objects.create(
            batch_id=f"bat-trans-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"TRANS-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today,
            expiry_date=today + datetime.timedelta(days=365),
            status=BatchStatus.ACTIVE,
        )

        # 1. ACTIVE -> QUARANTINED
        r1 = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/transition-status/",
            {"tenant_id": self.tenant_a.tenant_id, "target_status": "QUARANTINED", "reason": "Physical packaging check"},
            format="json",
        )
        assert r1.status_code == status.HTTP_200_OK
        assert r1.data["status"] == "QUARANTINED"
        assert r1.data["is_quarantined"] is True

        # 2. QUARANTINED -> ACTIVE
        r2 = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/transition-status/",
            {"tenant_id": self.tenant_a.tenant_id, "target_status": "ACTIVE", "reason": "Passed laboratory re-test"},
            format="json",
        )
        assert r2.status_code == status.HTTP_200_OK
        assert r2.data["status"] == "ACTIVE"
        assert r2.data["is_quarantined"] is False

        # 3. ACTIVE -> ARCHIVED
        r3 = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/transition-status/",
            {"tenant_id": self.tenant_a.tenant_id, "target_status": "ARCHIVED", "reason": "End of lifecycle"},
            format="json",
        )
        assert r3.status_code == status.HTTP_200_OK
        assert r3.data["status"] == "ARCHIVED"

        # 4. ARCHIVED -> ACTIVE (Forbidden)
        r4 = self.client.post(
            f"/api/v2/inventory/batch-entities/{batch.batch_id}/transition-status/",
            {"tenant_id": self.tenant_a.tenant_id, "target_status": "ACTIVE"},
            format="json",
        )
        assert r4.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert r4.data.get("error_code") == "ERR_BATCH_ALREADY_ARCHIVED"

    def test_cross_tenant_isolation(self):
        """Tenant B user cannot access or modify Tenant A batch records."""
        today = timezone.now().date()
        batch_a = BatchStock.objects.create(
            batch_id=f"bat-cross-a-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"CROSS-A-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today,
            expiry_date=today + datetime.timedelta(days=200),
            status=BatchStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.user_b)
        resp = self.client.get(
            f"/api/v2/inventory/batch-entities/{batch_a.batch_id}/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") == "ERR_TENANT_MISMATCH" or "tenant" in str(data).lower()

    def test_suspended_tenant_blocked(self):
        """Operations are blocked for suspended tenant accounts."""
        today = timezone.now().date()
        user_susp = PharmacyUser.objects.create_user(
            email=f"susp-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended User",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )
        self.client.force_authenticate(user=user_susp)

        payload = {
            "tenant_id": self.tenant_suspended.tenant_id,
            "branch_id": self.branch_suspended.branch_id,
            "product_id": self.product.product_id,
            "batch_number": f"BAT-SUSP-{self.suffix}",
            "quantity_base_units": 10,
            "cost_per_unit": "2.00",
            "expiry_date": (today + datetime.timedelta(days=200)).isoformat(),
        }

        resp = self.client.post("/api/v2/inventory/batch-entities/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(data).lower()

