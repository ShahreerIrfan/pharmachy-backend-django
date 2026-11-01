"""
backend/apps/core/offline_entitlement_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0082, ADR-0092 & ADR-0093
Phase 05: Basic/Standard/Pro Subscription & Billing (P093)
Requirement Trace: FR-071 (SaaS Plans, Billing & Tenant Lifecycle)

DRF ViewSet for Cryptographically Signed Offline Entitlement Leases:
- Issues time-bounded HMAC-SHA256 signed offline lease certificates.
- Verifies certificate integrity and expiration status.
- Allows owners to revoke lost/compromised terminal leases.
- Enforces strict multi-tenant boundary checks and role-based permissions.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import DeviceLeaseCertificate, OfflineLeaseStatus
from apps.core.offline_entitlement_service import OfflineEntitlementLeaseService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.offline_lease")


class OfflineEntitlementViewSet(viewsets.ViewSet):
    """
    Manages offline device entitlement lease issuance, verification, and revocation.
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
        return role in ["PHARMACY_OWNER", "OWNER", "ADMIN"]

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

    def list(self, request: Any) -> Response:
        """
        GET /api/v2/billing/offline-leases/
        Lists offline device lease certificates for tenant.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            qs = DeviceLeaseCertificate.objects.filter(tenant_id=tenant_id).order_by("-issued_at")
            branch_id = request.query_params.get("branch_id")
            if branch_id:
                qs = qs.filter(branch_id=str(branch_id).strip())

            device_id = request.query_params.get("device_id")
            if device_id:
                qs = qs.filter(device_id=str(device_id).strip())

            results = [OfflineEntitlementLeaseService._serialize_certificate(c) for c in qs[:50]]
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            logger.exception("Error listing offline leases: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="issue")
    def issue(self, request: Any) -> Response:
        """
        POST /api/v2/billing/offline-leases/issue/
        Issues a cryptographically signed offline lease.
        """
        try:
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            branch_id = request.data.get("branch_id", "")
            device_id = request.data.get("device_id", "")
            device_name = request.data.get("device_name", "")
            policy_version = request.data.get("policy_version", "v2.0")
            requested_duration_hours = request.data.get("requested_duration_hours")

            certificate = OfflineEntitlementLeaseService.issue_offline_lease(
                tenant_id=tenant_id,
                branch_id=branch_id,
                device_id=device_id,
                device_name=device_name,
                policy_version=policy_version,
                requested_duration_hours=requested_duration_hours,
                actor_user=request.user,
            )
            return Response(certificate, status=status.HTTP_201_CREATED)

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
            logger.exception("Error issuing offline lease: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="verify")
    def verify(self, request: Any) -> Response:
        """
        POST /api/v2/billing/offline-leases/verify/
        Verifies cryptographic signature, validity, and status of offline lease certificate.
        """
        try:
            lease_id = request.data.get("lease_id")
            raw_payload = request.data.get("payload")
            signature = request.data.get("signature")

            result = OfflineEntitlementLeaseService.verify_offline_lease(
                lease_id=lease_id,
                raw_payload=raw_payload,
                signature=signature,
            )
            return Response(result, status=status.HTTP_200_OK)

        except Exception as err:
            logger.exception("Error verifying offline lease: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="revoke")
    def revoke(self, request: Any) -> Response:
        """
        POST /api/v2/billing/offline-leases/revoke/
        Revokes a lost, stolen, or compromised device lease. Requires Owner/Admin privilege.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or administrators can revoke device leases.")

            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            lease_id = request.data.get("lease_id", "")
            reason = request.data.get("reason", "")

            # Ensure lease belongs to tenant
            try:
                cert = DeviceLeaseCertificate.objects.get(lease_id=lease_id)
            except DeviceLeaseCertificate.DoesNotExist:
                return Response(
                    {"error": "NOT_FOUND", "detail": f"Lease '{lease_id}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if not self._is_platform_admin(request.user) and cert.tenant_id != tenant_id:
                raise CrossTenantViolationError(
                    f"User from tenant '{tenant_id}' cannot revoke lease belonging to tenant '{cert.tenant_id}'."
                )

            result = OfflineEntitlementLeaseService.revoke_offline_lease(
                lease_id=lease_id,
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
            logger.exception("Error revoking offline lease: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"], url_path="active")
    def active_lease(self, request: Any) -> Response:
        """
        GET /api/v2/billing/offline-leases/active/
        Returns active lease for a specific device.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            device_id = request.query_params.get("device_id")
            if not device_id:
                return Response(
                    {"error": "VALIDATION_ERROR", "detail": "device_id query param is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cert = (
                DeviceLeaseCertificate.objects.filter(
                    tenant_id=tenant_id,
                    device_id=str(device_id).strip(),
                    status=OfflineLeaseStatus.ACTIVE,
                )
                .order_by("-issued_at")
                .first()
            )
            if not cert:
                return Response(
                    {"error": "NOT_FOUND", "detail": "No active lease found for device."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            serialized = OfflineEntitlementLeaseService._serialize_certificate(cert)
            return Response(serialized, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            logger.exception("Error retrieving active offline lease: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
