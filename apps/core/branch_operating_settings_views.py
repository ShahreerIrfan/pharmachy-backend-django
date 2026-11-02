"""
REST API ViewSet for Branch Operating Settings & Precedence Governance.
Architecture Decision: ADR-0116 (P116).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.branch_operating_settings_service import BranchOperatingSettingsService

logger = logging.getLogger("pharmacy.core.branch_operating_settings.views")


class BranchOperatingSettingsViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for retrieving and updating branch operating settings, receipt templates,
    operating hours, zones, low stock alerts, and device policies.
    """

    permission_classes = [permissions.IsAuthenticated]

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/branches/operating-settings/{branch_id}/"""
        try:
            settings_dict = BranchOperatingSettingsService.get_branch_settings(
                actor_user=request.user,
                branch_id=pk,
            )
            return Response(settings_dict, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, pk=None) -> Response:
        """PUT /api/v2/branches/operating-settings/{branch_id}/"""
        try:
            updated = BranchOperatingSettingsService.update_branch_settings(
                actor_user=request.user,
                branch_id=pk,
                updates=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def partial_update(self, request, pk=None) -> Response:
        """PATCH /api/v2/branches/operating-settings/{branch_id}/"""
        return self.update(request, pk=pk)

