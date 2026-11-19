"""
backend/apps/core/provisional_identifier_service.py
Architecture Decision: ADR-0315 (P315).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Authoritative Provisional Identifier Mapping Service:
- Maps client-generated provisional UUIDs (invoices, customers, payments, prescriptions, returns)
  to canonical cloud IDs without reference collisions across concurrent offline terminals.
- Guarantees exact idempotency via UNIQUE (tenant, entity_type, provisional_id).
- Supports bidirectional lookup: barcode scanners, returns, or cashiers can query using either
  the offline provisional receipt ID or the authoritative cloud ID transparently.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    ProvisionalEntityType,
    ProvisionalIdentifierMapping,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger(__name__)


class ProvisionalIdentifierService:

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            t = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' does not exist."})
        if t.status != TenantLifecycleStatus.ACTIVE:
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_SUSPENDED", "detail": f"Tenant '{tenant_id}' is suspended or inactive."}
            )
        return t

    @classmethod
    def _serialize_mapping(cls, m: ProvisionalIdentifierMapping, is_replay: bool = False) -> Dict[str, Any]:
        return {
            "mapping_id": m.mapping_id,
            "tenant_id": str(m.tenant.tenant_id),
            "branch_id": str(m.branch.branch_id),
            "device_id": m.device_registration.device_id,
            "entity_type": m.entity_type,
            "provisional_id": m.provisional_id,
            "canonical_id": m.canonical_id,
            "idempotency_key": m.idempotency_key,
            "client_sequence": m.client_sequence,
            "reconciliation_status": m.reconciliation_status,
            "mapped_at": m.mapped_at.isoformat(),
            "is_replay": is_replay,
        }

    @classmethod
    @transaction.atomic
    def register_or_resolve_mapping(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        entity_type: str,
        provisional_id: str,
        canonical_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        client_sequence: int = 1,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)

        device_reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(
            tenant=tenant, device_id=device_id
        ).first()
        if not device_reg:
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{device_id}' not found."})

        ent_type = str(entity_type or "").strip().upper()
        if ent_type not in ProvisionalEntityType.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_ENTITY_TYPE", "detail": f"Supported types: {ProvisionalEntityType.values}."}
            )

        prov_id = str(provisional_id or "").strip()
        if not prov_id or len(prov_id) < 3:
            raise ValidationError(
                {"error_code": "ERR_PROVISIONAL_ID_REQUIRED", "detail": "provisional_id cannot be blank or empty."}
            )

        # 1. Idempotent Lookup Check
        existing = ProvisionalIdentifierMapping.objects.select_related("tenant", "branch", "device_registration").filter(
            tenant=tenant, entity_type=ent_type, provisional_id=prov_id
        ).first()
        if existing:
            return cls._serialize_mapping(existing, is_replay=True)

        # 2. Assign canonical ID if not supplied
        assigned_canonical = str(canonical_id or "").strip()
        if not assigned_canonical:
            prefix = ent_type[:3]
            assigned_canonical = f"CAN-{prefix}-{uuid.uuid4().hex[:10].upper()}"

        idem_key = str(idempotency_key or f"idem-{uuid.uuid4().hex[:12]}").strip()
        mapping_id = f"pim-{uuid.uuid4().hex[:12]}"

        mapping = ProvisionalIdentifierMapping.objects.create(
            mapping_id=mapping_id,
            tenant=tenant,
            branch=device_reg.branch,
            device_registration=device_reg,
            entity_type=ent_type,
            provisional_id=prov_id,
            canonical_id=assigned_canonical,
            idempotency_key=idem_key,
            client_sequence=int(client_sequence),
            reconciliation_status="RECONCILED",
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PROVISIONAL_ID_MAPPED",
            resource_type="ProvisionalIdentifierMapping",
            resource_id=mapping.mapping_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "entity_type": ent_type,
                "provisional_id": prov_id,
                "canonical_id": assigned_canonical,
                "device_id": device_id,
            },
        )

        return cls._serialize_mapping(mapping, is_replay=False)

    @classmethod
    def lookup_by_any_id(
        cls,
        *,
        tenant_id: str,
        search_id: str,
        entity_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        s_id = str(search_id or "").strip()
        if not s_id:
            raise ValidationError({"error_code": "ERR_SEARCH_ID_REQUIRED", "detail": "search_id is required."})

        qs = ProvisionalIdentifierMapping.objects.select_related("tenant", "branch", "device_registration").filter(
            tenant=tenant
        )
        if entity_type:
            qs = qs.filter(entity_type=str(entity_type).strip().upper())

        mapping = qs.filter(provisional_id=s_id).first() or qs.filter(canonical_id=s_id).first()
        if not mapping:
            raise NotFound({"error_code": "ERR_IDENTIFIER_NOT_FOUND", "detail": f"Identifier '{s_id}' not found."})

        return cls._serialize_mapping(mapping, is_replay=False)

    @classmethod
    @transaction.atomic
    def batch_reconcile(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        items: List[Dict[str, Any]],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        if not items or not isinstance(items, list):
            raise ValidationError({"error_code": "ERR_EMPTY_BATCH", "detail": "items list cannot be empty."})

        results: List[Dict[str, Any]] = []
        new_count = 0
        replay_count = 0

        for it in items:
            res = cls.register_or_resolve_mapping(
                tenant_id=tenant_id,
                device_id=device_id,
                entity_type=it.get("entity_type", "INVOICE"),
                provisional_id=it.get("provisional_id", ""),
                canonical_id=it.get("canonical_id"),
                idempotency_key=it.get("idempotency_key"),
                client_sequence=it.get("client_sequence", 1),
                actor_user=actor_user,
            )
            if res.get("is_replay"):
                replay_count += 1
            else:
                new_count += 1
            results.append(res)

        return {
            "total_items": len(results),
            "new_mapped_count": new_count,
            "replay_count": replay_count,
            "results": results,
        }
