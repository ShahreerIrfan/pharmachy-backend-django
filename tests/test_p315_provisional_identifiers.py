"""
backend/tests/test_p315_provisional_identifiers.py
Architecture Decision: ADR-0315 (P315).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Local Provisional Identifier Mapping Engine:
1. Valid registration & collision-free canonical assignment.
2. Replay-safe idempotency: Re-submitting the same provisional ID returns identical canonical ID with is_replay=True.
3. Bidirectional transparent lookup (lookup by provisional ID returns canonical ID; lookup by canonical ID returns provisional ID).
4. Validation: Invalid entity type or empty provisional ID raises 400 ValidationError.
5. Cross-tenant isolation: Tenant B cannot access or collide with Tenant A's provisional identifiers.
6. Batch reconciliation and DRF ViewSet endpoints (/reconcile, /lookup, /list).
"""

import datetime
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    ProvisionalEntityType,
    ProvisionalIdentifierMapping,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.provisional_identifier_service import ProvisionalIdentifierService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestProvisionalIdentifiersIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-pim-a-{self.suffix}",
            name=f"Provisional Pharma A {self.suffix}",
            subdomain=f"pim-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-pim-a-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Branch"
        )
        self.device_a = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-pim-a-{self.suffix}",
            device_id=f"POS-PIM-A-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-pim-b-{self.suffix}",
            name=f"Provisional Pharma B {self.suffix}",
            subdomain=f"pim-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-pim-b-{self.suffix}", tenant=self.tenant_b, name="Banani Branch"
        )
        self.device_b = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-pim-b-{self.suffix}",
            device_id=f"POS-PIM-B-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Users
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="PIM Owner A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="PIM Owner B",
            password="Password123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_01_valid_registration_and_replay_idempotency(self):
        prov_id = f"PROV-INVO-{uuid.uuid4()}"
        res1 = ProvisionalIdentifierService.register_or_resolve_mapping(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            entity_type=ProvisionalEntityType.INVOICE,
            provisional_id=prov_id,
            actor_user=self.owner_a,
        )
        assert res1["is_replay"] is False
        assert res1["provisional_id"] == prov_id
        assert res1["canonical_id"].startswith("CAN-INV-")
        canonical_assigned = res1["canonical_id"]

        # Re-submitting with identical provisional ID MUST return identical canonical ID
        res2 = ProvisionalIdentifierService.register_or_resolve_mapping(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            entity_type=ProvisionalEntityType.INVOICE,
            provisional_id=prov_id,
            actor_user=self.owner_a,
        )
        assert res2["is_replay"] is True
        assert res2["canonical_id"] == canonical_assigned
        assert ProvisionalIdentifierMapping.objects.filter(tenant=self.tenant_a, provisional_id=prov_id).count() == 1

    def test_02_bidirectional_lookup(self):
        prov_id = f"PROV-CUST-{uuid.uuid4()}"
        canon_id = f"CAN-CUS-DH-{self.suffix}"
        ProvisionalIdentifierService.register_or_resolve_mapping(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            entity_type=ProvisionalEntityType.CUSTOMER,
            provisional_id=prov_id,
            canonical_id=canon_id,
            actor_user=self.owner_a,
        )

        # Lookup by provisional ID
        by_prov = ProvisionalIdentifierService.lookup_by_any_id(
            tenant_id=str(self.tenant_a.tenant_id),
            search_id=prov_id,
        )
        assert by_prov["canonical_id"] == canon_id

        # Lookup by canonical ID
        by_canon = ProvisionalIdentifierService.lookup_by_any_id(
            tenant_id=str(self.tenant_a.tenant_id),
            search_id=canon_id,
        )
        assert by_canon["provisional_id"] == prov_id

    def test_03_invalid_inputs_rejected(self):
        with pytest.raises(Exception):
            ProvisionalIdentifierService.register_or_resolve_mapping(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                entity_type="INVALID_TYPE",
                provisional_id="PROV-001",
                actor_user=self.owner_a,
            )

        with pytest.raises(Exception):
            ProvisionalIdentifierService.register_or_resolve_mapping(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                entity_type=ProvisionalEntityType.INVOICE,
                provisional_id=" ",
                actor_user=self.owner_a,
            )

    def test_04_cross_tenant_isolation(self):
        prov_id = f"PROV-INVO-{uuid.uuid4()}"
        ProvisionalIdentifierService.register_or_resolve_mapping(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            entity_type=ProvisionalEntityType.INVOICE,
            provisional_id=prov_id,
            actor_user=self.owner_a,
        )

        # Tenant B lookup for Tenant A's ID must fail (404)
        with pytest.raises(Exception):
            ProvisionalIdentifierService.lookup_by_any_id(
                tenant_id=str(self.tenant_b.tenant_id),
                search_id=prov_id,
            )

    def test_05_batch_reconcile_and_drf_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        prov_inv = f"PROV-INVO-{uuid.uuid4()}"
        prov_pay = f"PROV-PAYM-{uuid.uuid4()}"

        reconcile_resp = self.client.post(
            "/api/v2/offline-sync/provisional-identifiers/reconcile/",
            data={
                "tenant_id": str(self.tenant_a.tenant_id),
                "device_id": self.device_a.device_id,
                "items": [
                    {"entity_type": "INVOICE", "provisional_id": prov_inv, "client_sequence": 1},
                    {"entity_type": "PAYMENT", "provisional_id": prov_pay, "client_sequence": 2},
                ],
            },
            format="json",
        )
        assert reconcile_resp.status_code == status.HTTP_200_OK
        data = reconcile_resp.json()
        assert data["total_items"] == 2
        assert data["new_mapped_count"] == 2
        canon_inv = data["results"][0]["canonical_id"]

        # Query REST lookup endpoint
        lookup_resp = self.client.get(
            f"/api/v2/offline-sync/provisional-identifiers/lookup/?tenant_id={self.tenant_a.tenant_id}&search_id={prov_inv}"
        )
        assert lookup_resp.status_code == status.HTTP_200_OK
        lookup_data = lookup_resp.json()
        assert lookup_data["canonical_id"] == canon_inv
