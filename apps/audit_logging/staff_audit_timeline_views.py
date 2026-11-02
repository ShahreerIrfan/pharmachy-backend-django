"""
Staff Audit Timeline REST API Views.
Architecture Decision: ADR-0118 (P118).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.audit_logging.staff_audit_timeline_service import StaffAuditTimelineService

logger = logging.getLogger("pharmacy.audit_logging.timeline.views")


class StaffAuditTimelineViewSet(viewsets.ViewSet):
    """
    REST API ViewSet exposing chronological staff audit timelines
    with actor, branch, and device attribution.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/staff/audit-timeline/"""
        tenant_id = request.query_params.get("tenant_id")
        branch_id = request.query_params.get("branch_id")
        staff_id = request.query_params.get("staff_id")
        event_category = request.query_params.get("event_category")
        device_id = request.query_params.get("device_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        limit_param = request.query_params.get("limit", "50")

        try:
            limit = int(limit_param)
        except ValueError:
            limit = 50

        try:
            timeline_data = StaffAuditTimelineService.get_staff_timeline(
                actor_user=request.user,
                target_tenant_id=tenant_id,
                branch_id=branch_id,
                staff_id=staff_id,
                event_category=event_category,
                device_id=device_id,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
            return Response(timeline_data, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request) -> Response:
        """
        POST /api/v2/staff/audit-timeline/
        Allows recording high-value staff events (e.g. from POS offline sync or desktop/mobile clients).
        """
        data = request.data
        tenant_id = data.get("tenant_id") or getattr(request.user, "tenant_id", None)
        event_category = data.get("event_category")
        action = data.get("action")
        target_staff_id = data.get("target_staff_id")
        target_staff_name = data.get("target_staff_name")
        branch_id = data.get("branch_id") or getattr(request.user, "branch_id", None)
        device_id = data.get("device_id") or request.META.get("HTTP_X_DEVICE_ID")
        ip_address = request.META.get("REMOTE_ADDR")
        status_val = data.get("status", "SUCCESS")
        before_state = data.get("before_state", {})
        after_state = data.get("after_state", {})
        metadata = data.get("metadata", {})
        summary_en = data.get("summary_en")
        summary_bn = data.get("summary_bn")

        if not event_category or not action:
            return Response(
                {
                    "error_code": "ERR_INVALID_TIMELINE_INPUT",
                    "message_en": "event_category and action are required fields.",
                    "message_bn": "ইভেন্ট ক্যাটাগরি এবং অ্যাকশন আবশ্যক।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            entry = StaffAuditTimelineService.record_staff_audit_event(
                tenant_id=tenant_id,
                event_category=event_category,
                action=action,
                actor_id=str(request.user.user_id if hasattr(request.user, "user_id") else request.user.pk),
                actor_role=str(getattr(request.user, "role", "STAFF")),
                actor_email=getattr(request.user, "email", ""),
                branch_id=branch_id,
                device_id=device_id,
                ip_address=ip_address,
                target_staff_id=target_staff_id,
                target_staff_name=target_staff_name,
                status=status_val,
                before_state=before_state,
                after_state=after_state,
                metadata=metadata,
                summary_en=summary_en,
                summary_bn=summary_bn,
            )
            serialized = StaffAuditTimelineService._serialize_entry(entry)
            return Response(serialized, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
