"""
backend/apps/core/desktop_shell_views.py
Architecture Decision: ADR-0321 (P321).
Phase 17: JavaFX macOS-Inspired App Shell & UI Components.

DRF ViewSet for Desktop App Shell & Workspace Preferences:
- Provides profile synchronization, theme/locale settings, and route entitlement checks for JavaFX POS client.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.desktop_shell_service import DesktopShellService

logger = logging.getLogger(__name__)


class DesktopShellProfileViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="resolve-profile")
    def resolve_profile(self, request):
        user = request.user
        tenant_id = (
            request.data.get("tenant_id")
            or getattr(user, "tenant_id", None)
            or getattr(getattr(user, "tenant", None), "tenant_id", None)
        )
        branch_id = request.data.get("branch_id") or getattr(user, "branch_id", None)
        device_id = request.data.get("device_id")

        if not tenant_id or not branch_id or not device_id:
            return Response(
                {
                    "error_code": "ERR_INVALID_PAYLOAD",
                    "detail": "tenant_id, branch_id, and device_id are required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = DesktopShellService.get_or_create_workspace_profile(
            tenant_id=str(tenant_id),
            branch_id=str(branch_id),
            user=user,
            device_id=str(device_id),
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["patch"], url_path="update-preferences")
    def update_preferences(self, request):
        user = request.user
        tenant_id = (
            request.data.get("tenant_id")
            or getattr(user, "tenant_id", None)
            or getattr(getattr(user, "tenant", None), "tenant_id", None)
        )
        branch_id = request.data.get("branch_id") or getattr(user, "branch_id", None)
        device_id = request.data.get("device_id")

        if not tenant_id or not branch_id or not device_id:
            return Response(
                {
                    "error_code": "ERR_INVALID_PAYLOAD",
                    "detail": "tenant_id, branch_id, and device_id are required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = DesktopShellService.update_workspace_preferences(
            tenant_id=str(tenant_id),
            branch_id=str(branch_id),
            user=user,
            device_id=str(device_id),
            theme=request.data.get("theme"),
            locale=request.data.get("locale"),
            sidebar_collapsed=request.data.get("sidebar_collapsed"),
            quick_shortcuts_enabled=request.data.get("quick_shortcuts_enabled"),
            requested_routes=request.data.get("enabled_routes"),
        )
        return Response(res, status=status.HTTP_200_OK)
