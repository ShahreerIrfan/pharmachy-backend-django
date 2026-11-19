"""
backend/tests/test_p316_event_ordering_dependencies.py
Architecture Decision: ADR-0316 (P316).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Event Ordering & Transactional Dependency Barriers:
1. Topological ordering (Customer -> Sale, GRN -> Sale, Sale -> Return).
2. Cycle detection rejecting malformed causal graphs (ERR_CIRCULAR_DEPENDENCY_BARRIER).
3. In-batch dependency satisfaction and pre-committed upstream receipt satisfaction.
4. Blocked barrier status (BLOCKED_ON_DEPENDENCY) when upstream parent is missing.
5. Multi-tenant isolation: Tenant B cannot resolve or satisfy dependencies across Tenant A.
6. DRF ViewSet REST API endpoints (/evaluate and /list).
"""

import datetime
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.core.event_ordering_service import EventOrderingService
from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    EventBarrierStatus,
    EventDependencyRule,
    EventOrderingBarrier,
    OfflineOutboxCommandReceipt,
    ProvisionalEntityType,
    ProvisionalIdentifierMapping,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestEventOrderingDependenciesIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-eob-a-{self.suffix}",
            name=f"Ordering Pharma A {self.suffix}",
            subdomain=f"eob-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-eob-a-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Branch"
        )
        self.device_a = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-eob-a-{self.suffix}",
            device_id=f"POS-EOB-A-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-eob-b-{self.suffix}",
            name=f"Ordering Pharma B {self.suffix}",
            subdomain=f"eob-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-eob-b-{self.suffix}", tenant=self.tenant_b, name="Banani Branch"
        )
        self.device_b = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-eob-b-{self.suffix}",
            device_id=f"POS-EOB-B-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Users
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_eob_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="EOB Owner A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_eob_b_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="EOB Owner B",
            password="Password123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_01_topological_sort_kahn(self):
        # Unsorted list where sale depends on cust
        cmd_cust = {"command_id": "CMD-CUST-001", "depends_on_command_ids": []}
        cmd_sale = {"command_id": "CMD-SALE-001", "depends_on_command_ids": ["CMD-CUST-001"]}
        cmd_ret = {"command_id": "CMD-RET-001", "depends_on_command_ids": ["CMD-SALE-001"]}

        # Provide in reverse order
        reversed_batch = [cmd_ret, cmd_sale, cmd_cust]
        sorted_batch = EventOrderingService.topological_sort_commands(reversed_batch)

        assert len(sorted_batch) == 3
        assert sorted_batch[0]["command_id"] == "CMD-CUST-001"
        assert sorted_batch[1]["command_id"] == "CMD-SALE-001"
        assert sorted_batch[2]["command_id"] == "CMD-RET-001"

    def test_02_circular_dependency_detected(self):
        cmd_a = {"command_id": "CMD-A", "depends_on_command_ids": ["CMD-B"]}
        cmd_b = {"command_id": "CMD-B", "depends_on_command_ids": ["CMD-A"]}

        with pytest.raises(ValidationError) as exc_info:
            EventOrderingService.topological_sort_commands([cmd_a, cmd_b])

        assert "ERR_CIRCULAR_DEPENDENCY_BARRIER" in str(exc_info.value)

    def test_03_in_batch_satisfaction(self):
        cust_id = f"PROV-CUST-{uuid.uuid4().hex[:6]}"
        sale_id = f"CMD-SALE-{uuid.uuid4().hex[:6]}"

        commands = [
            {
                "command_id": sale_id,
                "command_type": "OFFLINE_POS_SALE",
                "depends_on_command_ids": [cust_id],
            },
            {
                "command_id": cust_id,
                "command_type": "CUSTOMER_CREATE",
                "depends_on_command_ids": [],
            },
        ]

        result = EventOrderingService.process_and_enforce_barriers(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            commands=commands,
            actor_user=self.owner_a,
        )

        assert result["all_dependencies_satisfied"] is True
        assert result["topological_execution_order"] == [cust_id, sale_id]
        assert len(result["barriers"]) == 2
        sale_barrier = next(b for b in result["barriers"] if b["command_id"] == sale_id)
        assert sale_barrier["status"] == EventBarrierStatus.SATISFIED
        assert sale_barrier["rule_code"] == EventDependencyRule.CUSTOMER_BEFORE_SALE

    def test_04_upstream_precommitted_satisfaction(self):
        # Upstream GRN already committed on server as OfflineOutboxCommandReceipt
        grn_id = f"CMD-GRN-{uuid.uuid4().hex[:6]}"
        OfflineOutboxCommandReceipt.objects.create(
            receipt_id=f"rec-{uuid.uuid4().hex[:8]}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_registration=self.device_a,
            command_id=grn_id,
            command_type="GOODS_RECEIVE_NOTE",
            idempotency_key=f"idem-{grn_id}",
            sync_status="ACCEPTED",
            client_sequence=1,
        )

        sale_id = f"CMD-SALE-{uuid.uuid4().hex[:6]}"
        commands = [
            {
                "command_id": sale_id,
                "command_type": "OFFLINE_POS_SALE",
                "depends_on_command_ids": [grn_id],
            }
        ]

        result = EventOrderingService.process_and_enforce_barriers(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            commands=commands,
            actor_user=self.owner_a,
        )

        assert result["all_dependencies_satisfied"] is True
        assert len(result["barriers"]) == 1
        assert result["barriers"][0]["status"] == EventBarrierStatus.SATISFIED
        assert result["barriers"][0]["rule_code"] == EventDependencyRule.GRN_BEFORE_SALE

    def test_05_blocked_on_missing_dependency(self):
        missing_parent = f"CMD-PARENT-MISSING-{uuid.uuid4().hex[:6]}"
        child_sale = f"CMD-SALE-BLOCKED-{uuid.uuid4().hex[:6]}"

        commands = [
            {
                "command_id": child_sale,
                "command_type": "OFFLINE_POS_SALE",
                "depends_on_command_ids": [missing_parent],
            }
        ]

        result = EventOrderingService.process_and_enforce_barriers(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            commands=commands,
            actor_user=self.owner_a,
        )

        assert result["all_dependencies_satisfied"] is False
        assert len(result["barriers"]) == 1
        b = result["barriers"][0]
        assert b["status"] == EventBarrierStatus.BLOCKED_ON_DEPENDENCY
        assert missing_parent in b["missing_command_ids"]

        # Stored in database
        db_b = EventOrderingBarrier.objects.get(command_id=child_sale, tenant=self.tenant_a)
        assert db_b.status == EventBarrierStatus.BLOCKED_ON_DEPENDENCY

    def test_06_cross_tenant_isolation(self):
        # Tenant A has committed receipt
        parent_id_a = f"CMD-PARENT-A-{uuid.uuid4().hex[:6]}"
        OfflineOutboxCommandReceipt.objects.create(
            receipt_id=f"rec-{uuid.uuid4().hex[:8]}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_registration=self.device_a,
            command_id=parent_id_a,
            command_type="GOODS_RECEIVE_NOTE",
            idempotency_key=f"idem-{parent_id_a}",
            sync_status="ACCEPTED",
            client_sequence=1,
        )

        # Tenant B tries to satisfy dependency using Tenant A's parent command
        child_sale_b = f"CMD-SALE-B-{uuid.uuid4().hex[:6]}"
        commands = [
            {
                "command_id": child_sale_b,
                "command_type": "OFFLINE_POS_SALE",
                "depends_on_command_ids": [parent_id_a],
            }
        ]

        result_b = EventOrderingService.process_and_enforce_barriers(
            tenant_id=str(self.tenant_b.tenant_id),
            device_id=self.device_b.device_id,
            commands=commands,
            actor_user=self.owner_b,
        )

        # For Tenant B, parent_id_a MUST be treated as missing
        assert result_b["all_dependencies_satisfied"] is False
        assert result_b["barriers"][0]["status"] == EventBarrierStatus.BLOCKED_ON_DEPENDENCY
        assert parent_id_a in result_b["barriers"][0]["missing_command_ids"]

    def test_07_drf_viewset_evaluate_and_list(self):
        self.client.force_authenticate(user=self.owner_a)

        c1 = f"CMD-C1-{uuid.uuid4().hex[:6]}"
        c2 = f"CMD-C2-{uuid.uuid4().hex[:6]}"

        payload = {
            "device_id": self.device_a.device_id,
            "commands": [
                {"command_id": c2, "command_type": "POS_RETURN", "depends_on_command_ids": [c1]},
                {"command_id": c1, "command_type": "OFFLINE_POS_SALE", "depends_on_command_ids": []},
            ],
        }

        # POST /api/v2/offline-sync/ordering-barriers/evaluate/
        res = self.client.post("/api/v2/offline-sync/ordering-barriers/evaluate/", data=payload, format="json")
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["all_dependencies_satisfied"] is True
        assert data["topological_execution_order"] == [c1, c2]

        # GET /api/v2/offline-sync/ordering-barriers/
        list_res = self.client.get("/api/v2/offline-sync/ordering-barriers/")
        assert list_res.status_code == status.HTTP_200_OK
        list_data = list_res.json()
        assert list_data["count"] >= 2
