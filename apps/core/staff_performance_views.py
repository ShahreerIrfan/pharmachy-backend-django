"""
REST API ViewSet for Staff Performance Metrics & Drawer Reconciliation.
Architecture Decision: ADR-0115 (P115).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.staff_performance_service import StaffPerformanceService

logger = logging.getLogger("pharmacy.core.staff_performance.views")


class StaffPerformanceMetricsViewSet(viewsets.ViewSet):
    """
    REST API ViewSet providing authorized visibility into staff sales, refunds,
    reconciliation variances, and reporting freshness.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/analytics/staff-performance/"""
        branch_id = request.query_params.get("branch_id")
        staff_id = request.query_params.get("staff_id")
        metric_date = request.query_params.get("metric_date")
        tenant_id = request.query_params.get("tenant_id")

        try:
            results = StaffPerformanceService.list_metrics(
                actor_user=request.user,
                tenant_id=tenant_id,
                branch_id=branch_id,
                staff_id=staff_id,
                metric_date=metric_date,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/analytics/staff-performance/{staff_id}/"""
        try:
            results = StaffPerformanceService.list_metrics(
                actor_user=request.user,
                staff_id=pk,
            )
            if not results:
                return Response(
                    {"error_code": "METRICS_NOT_FOUND", "message_en": f"No metrics found for staff '{pk}'."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(results[0], status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

