"""
backend/apps/audit_logging/platform_audit_service.py

Architecture Decision: ADR-0078
Platform Admin Audit Search Service:
- Multi-dimensional query engine (actor, tenant, date range, entity, reason)
- Runtime redaction guaranteeing zero exposure of forbidden payloads
- Audit trail for forensic search accountability
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from django.db.models import Q

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.audit_logging.serializers import SanitizedAuditLogEntrySerializer

logger = logging.getLogger(__name__)


class PlatformAuditSearchService:
    """
    Central search service for platform administrators to inspect cross-tenant audit trails
    with statutory privacy redactions and access governance.
    """

    @classmethod
    def search_events(
        cls,
        filters: Dict[str, Any],
        requesting_user: Any,
        client_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes a targeted search against immutable AuditLogEntry records.
        Applies runtime redaction to all state snapshots before returning results.
        """
        qs = AuditLogEntry.objects.all()

        # 1. Filter by Actor (email, user_id, or role)
        actor = filters.get("actor", "").strip() if filters.get("actor") else ""
        if actor:
            qs = qs.filter(Q(actor_email__icontains=actor) | Q(user_id__iexact=actor) | Q(actor_role__iexact=actor))

        # 2. Filter by Tenant
        tenant = filters.get("tenant", "").strip() if filters.get("tenant") else ""
        if tenant:
            qs = qs.filter(tenant_id=tenant)

        # 3. Filter by Date Range
        date_from = filters.get("date_from")
        if date_from:
            qs = qs.filter(timestamp__gte=date_from)

        date_to = filters.get("date_to")
        if date_to:
            qs = qs.filter(timestamp__lte=date_to)

        # 4. Filter by Entity (Resource Type / Resource ID)
        entity_type = filters.get("entity_type", "").strip() if filters.get("entity_type") else ""
        if entity_type:
            qs = qs.filter(resource_type__iexact=entity_type)

        entity_id = filters.get("entity_id", "").strip() if filters.get("entity_id") else ""
        if entity_id:
            qs = qs.filter(resource_id=entity_id)

        # 5. Filter by Reason / Action Keyword
        reason = filters.get("reason", "").strip() if filters.get("reason") else ""
        if reason:
            qs = qs.filter(Q(action__icontains=reason))

        # 6. Filter by Status
        status_val = filters.get("status", "ALL")
        if status_val and status_val != "ALL":
            qs = qs.filter(status=status_val)

        total_count = qs.count()
        qs = qs.order_by("-timestamp")

        # 7. Pagination
        offset = filters.get("offset", 0)
        limit = filters.get("limit", 50)
        records = list(qs[offset : offset + limit])

        # 8. Runtime Redaction Serialization
        serializer = SanitizedAuditLogEntrySerializer(records, many=True)
        results = serializer.data

        # 9. Audit the search invocation itself for accountability (ADR-0078)
        try:
            user_id = str(getattr(requesting_user, "user_id", getattr(requesting_user, "pk", str(requesting_user))))
            AuditLogEntry.objects.create(
                action="PLATFORM_AUDIT_SEARCH_EXECUTED",
                resource_type="AUDIT_SEARCH",
                resource_id=f"srch-{uuid.uuid4().hex[:12]}",
                tenant_id="platform-system",
                user_id=user_id,
                actor_email=str(getattr(requesting_user, "email", "")),
                actor_role=str(getattr(requesting_user, "role", "SUPERADMIN")),
                status=AuditActionStatus.SUCCESS,
                ip_address=client_ip,
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                metadata_json={
                    "actor_filter": actor,
                    "tenant_filter": tenant,
                    "date_from": str(date_from) if date_from else None,
                    "date_to": str(date_to) if date_to else None,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "reason_filter": reason,
                    "status_filter": status_val,
                    "matches_found": total_count,
                    "results_returned": len(results),
                },
            )
        except Exception as e:
            logger.warning("Failed to record audit log for platform search execution: %s", e)

        return {
            "success": True,
            "count": total_count,
            "results": results,
            "query_filters": {
                "actor": actor,
                "tenant": tenant,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "reason": reason,
                "status": status_val,
                "limit": limit,
                "offset": offset,
            },
        }

    @classmethod
    def get_event_detail(
        cls,
        entry_id: str,
        requesting_user: Any,
        client_ip: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieves and sanitizes a single audit log entry."""
        entry = AuditLogEntry.objects.filter(entry_id=entry_id).first()
        if not entry:
            return None

        # Log inspection
        try:
            user_id = str(getattr(requesting_user, "user_id", getattr(requesting_user, "pk", str(requesting_user))))
            AuditLogEntry.objects.create(
                action="PLATFORM_AUDIT_ENTRY_INSPECTED",
                resource_type="AUDIT_ENTRY",
                resource_id=str(entry.entry_id),
                tenant_id="platform-system",
                user_id=user_id,
                actor_email=str(getattr(requesting_user, "email", "")),
                actor_role=str(getattr(requesting_user, "role", "SUPERADMIN")),
                status=AuditActionStatus.SUCCESS,
                ip_address=client_ip,
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                metadata_json={"target_tenant": entry.tenant_id, "target_action": entry.action},
            )
        except Exception as e:
            logger.warning("Failed to record audit log for entry inspection: %s", e)

        serializer = SanitizedAuditLogEntrySerializer(entry)
        return serializer.data
