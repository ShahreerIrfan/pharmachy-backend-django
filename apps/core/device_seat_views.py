"""
backend/apps/core/device_seat_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0082, ADR-0095 & ADR-0096
Phase 05: Basic/Standard/Pro Subscription & Billing (P096)
Requirement Trace: FR-004, FR-070, FR-071, FR-072 (Authoritative Device Seats & Lease Invalidation)

DRF ViewSet for Device Seats Management:
- Enrolls POS workstations subject to plan quota limits.
- Supports branch reassignment within tenant boundaries.
- Revokes device seats, automatically invalidating leases and releasing quota.
- Strictly enforces multi-tenant isolation and action-specific RBAC.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.device_seat_service import DeviceSeatManagementService
from apps.core.errors import CrossTenantViolationError
from apps.core.models import DeviceSeat
from apps.core.quota_guard_service import QuotaExceededError
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.device_seat")


class DeviceSeatViewSet(viewsets.ViewSet):
    """
    Manages enrolled POS workstations, seat quota consumption, and lease invalidation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_admin(self, user: Any) -> bool:
        if not user or not user.is_authenticated:
            return False
        return (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR]
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "PLATFORM_OPERATOR", "SUPER_ADMIN"]
        )

    def _is_owner_or_admin(self, user: Any) -> bool:
        if self._is_platform_admin(user):
            return True
        role = str(getattr(user, "role", "")).upper()
        return role in ["PHARMACY_OWNER", "OWNER", "ADMIN", "PHARMACY_MANAGER"]

    def _resolve_tenant_id(self, request: Any, target_tenant_id: str | None = None) -> str:
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access tenant '{clean_target}'."
                )
            return clean_target

        if not user_tid:
            raise ValidationError("Tenant context could not be resolved from authenticated user.")
        return user_tid

    def _check_device_tenant_boundary(self, user: Any, device_id: str) -> DeviceSeat:
        try:
            device = DeviceSeat.objects.select_related("tenant", "branch").get(device_id=device_id)
        except DeviceSeat.DoesNotExist as err:
            raise ValidationError(f"Device '{device_id}' does not exist.") from err

        if not self._is_platform_admin(user):
            user_tid = str(getattr(user, "tenant_id", "")).strip()
            if user_tid and user_tid != device.tenant_id:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access device '{device_id}' belonging to '{device.tenant_id}'."
                )
        return device

    def list(self, request: Any) -> Response:
        """
        GET /api/v2/billing/devices/
        Lists enrolled devices and quota capacity summary for tenant.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            result = DeviceSeatManagementService.list_device_seats(tenant_id)
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error listing device seats: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="enroll")
    def enroll(self, request: Any) -> Response:
        """
        POST /api/v2/billing/devices/enroll/
        Enrolls a new workstation and consumes an authoritative device seat.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can enroll new device seats.")

            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            branch_id = request.data.get("branch_id")
            device_id = request.data.get("device_id")
            device_name = request.data.get("device_name")
            device_type = request.data.get("device_type", "DESKTOP_POS")
            mac_address = request.data.get("mac_address", "")
            ip_address = request.data.get("ip_address", "")
            hardware_fingerprint = request.data.get("hardware_fingerprint", "")

            if not branch_id or not device_id or not device_name:
                raise ValidationError("branch_id, device_id, and device_name are required fields.")

            result = DeviceSeatManagementService.enroll_device(
                tenant_id=tenant_id,
                branch_id=branch_id,
                device_id=device_id,
                device_name=device_name,
                device_type=device_type,
                mac_address=mac_address,
                ip_address=ip_address,
                hardware_fingerprint=hardware_fingerprint,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_201_CREATED)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except QuotaExceededError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "ERR_QUOTA_EXCEEDED", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error enrolling device seat: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request: Any, pk: str | None = None) -> Response:
        """
        POST /api/v2/billing/devices/{device_id}/reassign/
        Reassigns an active device seat to another branch within the tenant.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can reassign device branches.")

            device = self._check_device_tenant_boundary(request.user, pk or "")
            new_branch_id = request.data.get("new_branch_id")
            if not new_branch_id:
                raise ValidationError("new_branch_id is required.")

            result = DeviceSeatManagementService.reassign_branch(
                device_id=device.device_id,
                new_branch_id=new_branch_id,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error reassigning device branch: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request: Any, pk: str | None = None) -> Response:
        """
        POST /api/v2/billing/devices/{device_id}/revoke/
        Revokes a device seat, invalidates active leases, and releases quota.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can revoke device seats.")

            device = self._check_device_tenant_boundary(request.user, pk or "")
            reason = request.data.get("reason", "")

            result = DeviceSeatManagementService.revoke_device_seat(
                device_id=device.device_id,
                reason=reason,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error revoking device seat: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="heartbeat")
    def heartbeat(self, request: Any, pk: str | None = None) -> Response:
        """
        POST /api/v2/billing/devices/{device_id}/heartbeat/
        Receives terminal heartbeat telemetry.
        """
        try:
            device = self._check_device_tenant_boundary(request.user, pk or "")
            ip = request.data.get("ip_address", "")
            mac = request.data.get("mac_address", "")

            result = DeviceSeatManagementService.record_heartbeat(
                device_id=device.device_id,
                ip_address=ip,
                mac_address=mac,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error recording device heartbeat: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

