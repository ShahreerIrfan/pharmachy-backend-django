"""
REST API ViewSet for Staff Offline Authorization Leases.
Architecture Decision: ADR-0113 (P113).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import StaffOfflineAuthorization
from apps.core.staff_offline_auth_service import StaffOfflineAuthorizationService

logger = logging.getLogger("pharmacy.core.staff_offline_auth.views")


class StaffOfflineAuthorizationViewSet(viewsets.ViewSet):
    """
    API endpoints for issuing, listing, and revoking cryptographically signed
    offline authorizations for POS desktop devices and frontline pharmacy staff.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/staff/offline-authorizations/"""
        tenant_id = getattr(request.user, "tenant_id", None)
        qs = StaffOfflineAuthorization.objects.filter(tenant__tenant_id=tenant_id).select_related("staff_member", "branch")
        results = [
            {
                "authorization_id": item.authorization_id,
                "tenant_id": item.tenant.tenant_id,
                "branch_id": item.branch.branch_id if item.branch else None,
                "staff_id": item.staff_member.staff_id,
                "username": item.staff_member.username,
                "full_name": item.staff_member.full_name,
                "role": item.role_snapshot,
                "device_id": item.device_id,
                "credential_salt": item.credential_salt,
                "credential_hash": item.credential_hash,
                "permitted_actions": item.permitted_actions,
                "denied_actions": item.denied_actions,
                "risk_limits": item.risk_limits,
                "issued_at": item.issued_at.isoformat(),
                "expires_at": item.expires_at.isoformat(),
                "signature": item.signature,
                "is_revoked": item.is_revoked,
            }
            for item in qs
        ]
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """POST /api/v2/staff/offline-authorizations/"""
        data = request.data
        tenant_id = data.get("tenant_id") or getattr(request.user, "tenant_id", None)
        staff_id = data.get("staff_id")
        device_id = data.get("device_id")
        pin = data.get("pin_or_passcode")
        branch_id = data.get("branch_id")
        duration = data.get("duration_hours", 72)

        try:
            auth_payload = StaffOfflineAuthorizationService.issue_offline_authorization(
                actor_user=request.user,
                tenant_id=tenant_id,
                staff_id=staff_id,
                device_id=device_id,
                pin_or_passcode=pin,
                branch_id=branch_id,
                duration_hours=duration,
            )
            return Response(auth_payload, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None) -> Response:
        """POST /api/v2/staff/offline-authorizations/{id}/revoke/"""
        reason = request.data.get("reason", "MANUAL_REVOCATION")
        try:
            res = StaffOfflineAuthorizationService.revoke_offline_authorization(
                actor_user=request.user,
                authorization_id=pk,
                reason=reason,
            )
            return Response(res, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="verify-offline")
    def verify_offline(self, request) -> Response:
        """POST /api/v2/staff/offline-authorizations/verify-offline/"""
        payload = request.data.get("payload", {})
        pin = request.data.get("pin_or_passcode", "")
        device_id = request.data.get("device_id", "")
        try:
            result = StaffOfflineAuthorizationService.verify_offline_credentials(
                payload=payload,
                pin_or_passcode=pin,
                current_device_id=device_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

