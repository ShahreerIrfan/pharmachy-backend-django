"""
REST API ViewSet for Active User Sessions and Instant Device Revocation.
Architecture Decision: ADR-0045, ADR-0112 (P112).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Enforces:
- Active session listing for Owners, Branch Managers, and Staff.
- Role-based scoping (Owner: tenant-wide, Manager: branch-wide, Staff: self-only).
- Cross-tenant and cross-branch violation guards.
- Immediate online revocation stopping access tokens without waiting for expiration.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.identity.active_session_service import ActiveSessionService

logger = logging.getLogger("pharmacy.identity.sessions.views")


class ActiveSessionViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for managing active device sessions, multi-user audit, and instant revocation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_current_session_id(self, request) -> str | None:
        token_session_id = getattr(request, "auth_session_id", None)
        if not token_session_id and hasattr(request, "auth") and isinstance(request.auth, dict):
            token_session_id = request.auth.get("session_id")
        if not token_session_id:
            token_session_id = request.META.get("HTTP_X_SESSION_ID")
        return str(token_session_id) if token_session_id else None

    def list(self, request) -> Response:
        """
        GET /api/v2/sessions/active/
        Lists active device sessions subject to caller's role authority.
        """
        branch_id = request.query_params.get("branch_id")
        target_user_id = request.query_params.get("user_id")
        active_param = request.query_params.get("is_active")
        is_active = None
        if active_param is not None:
            is_active = str(active_param).lower() in ["true", "1", "yes"]

        target_tenant = request.query_params.get("tenant_id")
        current_sess_id = self._get_current_session_id(request)

        try:
            sessions = ActiveSessionService.list_sessions(
                actor_user=request.user,
                tenant_id=target_tenant,
                branch_id=branch_id,
                target_user_id=target_user_id,
                is_active=is_active,
                current_session_id=current_sess_id,
            )
            return Response({"count": len(sessions), "results": sessions}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/sessions/active/{session_id}/
        Retrieves detail of a specific device session.
        """
        current_sess_id = self._get_current_session_id(request)
        try:
            sessions = ActiveSessionService.list_sessions(
                actor_user=request.user,
                current_session_id=current_sess_id,
            )
            target = next((s for s in sessions if s["session_id"] == str(pk)), None)
            if not target:
                return Response(
                    {"error_code": "SESSION_NOT_FOUND", "message": f"Session '{pk}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(target, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post", "delete"], url_path="revoke")
    def revoke(self, request, pk=None) -> Response:
        """
        POST /api/v2/sessions/active/{session_id}/revoke/
        Revokes a specific session, immediately blocking future online requests.
        """
        reason = request.data.get("reason") or "MANUAL_REVOCATION"
        try:
            revoked = ActiveSessionService.revoke_session(
                actor_user=request.user,
                session_id=str(pk),
                reason=reason,
            )
            return Response(
                {
                    "session_id": revoked["session_id"],
                    "status": "SUCCESS",
                    "revoked": True,
                    "revocation_reason": revoked["revocation_reason"],
                    "message_en": "Device session successfully revoked.",
                    "message_bn": "ডিভাইস সেশনটি সফলভাবে বাতিল করা হয়েছে।",
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as exc:
            code = exc.detail.get("error_code") if isinstance(exc.detail, dict) else ""
            if code == "SESSION_NOT_FOUND":
                return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

    def destroy(self, request, pk=None) -> Response:
        """DELETE alias for revoke."""
        return self.revoke(request, pk=pk)

    @action(detail=False, methods=["post"], url_path="revoke-all")
    def revoke_all(self, request) -> Response:
        """
        POST /api/v2/sessions/active/revoke-all/
        Terminates all active device sessions for a target user (or current user).
        """
        target_uid = request.data.get("target_user_id") or request.data.get("user_id")
        reason = request.data.get("reason") or "LOGOUT_ALL"

        try:
            result = ActiveSessionService.revoke_all_sessions(
                actor_user=request.user,
                target_user_id=target_uid,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)

